"""Product UI > Setup: the lever arms of the Live config file, for the people who mount the system.

After the AIO system or the GNSS antenna is moved, its lever arms must follow, and they are easy to
measure with a tape. So they can be changed here, without the maintenance password, like Start / Stop
(the access list decides who reaches this site). Saving goes the config editor's way
(actions/aionav_config.py): checked, AIO NAV test-started on the new file, the previous version kept,
and the change recorded in the event log. product.edit_lever_arms: false turns it off.

Axes, from AIO NAV's own data (2026-10-21): the IMU's z axis points down (it reads -9.73 m/s2 at
rest), and the camera rotation Odometry.c_bs maps the camera's right / down / forward axes to the
body's y / z / x. So the body frame is x forward, y right, z down, and a lever arm is the position
of the point as seen from the IMU in that frame (the shipped values: the vehicle reference point
2.25 m behind and 1.52 m below the IMU, the antenna 0.49 m behind it).
"""

import math

from flask import jsonify, request

from ..actions import aionav_config as ac
from ..actions import process_control
from ..actions.registry import ActionError
from .common import json_body

ARMS = {"vehicle": "Motion.leverarm", "gnss": "GNSS.leverarm"}
LABELS = {"vehicle": "vehicle reference point", "gnss": "GNSS antenna"}
MODE = "live"                 # the mounting belongs to the vehicle, not to a recorded bag
MAX_M = 20.0                  # longer than any vehicle the system goes on


def _arm(value, name: str):
    """[forward, right, down] in metres, rounded to the millimetre, or ValueError."""
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{name}: three numbers (forward, right, down)")
    out = []
    for v in value:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"{name}: three numbers (forward, right, down)")
        if abs(v) > MAX_M:
            raise ValueError(f"{name}: each distance at most {MAX_M:g} m")
        out.append(round(float(v), 3) + 0.0)            # + 0.0: no "-0.0" in the file
    return out


def _show(arm) -> str:
    return "[" + ", ".join(f"{v:g}" for v in arm) + "]" if arm else "-"


def register_setup(ctx, app) -> None:
    def enabled() -> bool:
        return bool(ctx.cfg["product"].get("edit_lever_arms", True))

    def nav_running() -> bool:
        return bool(((ctx.store.get("services", {}) or {}).get(ctx.cfg["nav"]["service"]) or {}).get("pids"))

    def vehicle_still():
        return ((ctx.store.get("readiness", {}) or {}).get("motion") or {}).get("still")

    def load():
        f = ac.file_for(ctx.cfg, MODE)
        with open(f["path"], encoding="utf-8") as fh:
            return f, fh.read()

    @app.route("/api/setup/lever-arms")
    def api_lever_arms():
        if not enabled():
            return jsonify({"available": False, "summary": "changing the lever arms here is turned off"}), 403
        try:
            f, text = load()
        except (ac.ConfigError, OSError) as e:
            return jsonify({"available": False, "summary": f"no Live config file: {e}"})
        values = ac.form_values(text)
        return jsonify({"available": True, "version": f["version"], "file": f["name"], "active": f["active"],
                        "writable": f["writable"], "max_m": MAX_M,
                        "vehicle": values.get(ARMS["vehicle"]), "gnss": values.get(ARMS["gnss"]),
                        "nav_running": nav_running(), "still": vehicle_still(),
                        "can_restart": bool(ctx.cfg["nav"]["allow_control"])})

    @app.route("/api/setup/lever-arms", methods=["POST"])
    def api_lever_arms_save():
        if request.headers.get("X-Requested-With") != "aio-dashboard" or not request.is_json:
            return jsonify({"success": False, "summary": "bad request"}), 400
        if not enabled():
            return jsonify({"success": False, "summary": "changing the lever arms here is turned off"}), 403
        b = json_body()
        try:
            arms = {k: _arm(b.get(k), LABELS[k]) for k in ARMS}
        except ValueError as e:
            return jsonify({"success": False, "summary": str(e)}), 422
        if not isinstance(b.get("base_version"), str):
            return jsonify({"success": False, "summary": "this page is out of date; reload it"}), 409
        try:
            f, text = load()
        except (ac.ConfigError, OSError) as e:
            return jsonify({"success": False, "summary": f"no Live config file: {e}"}), 404
        restart = b.get("restart") is True and f["active"] and nav_running()
        if restart:
            # Checked before anything is written: the request is done whole or not at all.
            if not ctx.cfg["nav"]["allow_control"]:
                return jsonify({"success": False, "summary": "restarting AIO NAV is disabled here"}), 403
            if vehicle_still() is not True:
                return jsonify({"success": False, "summary": "not saved: the vehicle must stand still to restart "
                                                             "AIO NAV (it aligns again). Stop, or press Save alone."}), 409
        old = ac.form_values(text)
        try:
            new_text = ac.apply_form(text, {ARMS[k]: arms[k] for k in ARMS})
            result = ac.save(ctx.cfg, MODE, new_text, b["base_version"])
        except ac.ConflictError:
            return jsonify({"success": False, "conflict": True,
                            "summary": "the settings changed meanwhile (another page?); reload and enter them again"}), 409
        except ac.ConfigError as e:
            return jsonify({"success": False, "summary": f"not saved: {e}"}), 422
        except OSError as e:
            ctx.events.add("fault", "action", "Lever arms not saved", f"{e.__class__.__name__}: {e}")
            return jsonify({"success": False, "summary": f"not saved: cannot write {f['name']} ({e})"}), 500
        if not result["changed"]:
            return jsonify({"success": True, "saved": False, "summary": "No changes.", "version": result["version"]})
        detail = "; ".join(f"{LABELS[k]} {_show(old.get(ARMS[k]))} -> {_show(arms[k])}" for k in ARMS
                           if old.get(ARMS[k]) != arms[k])
        ctx.events.add("warning", "action", "Lever arms changed",
                       f"{detail} (Setup page, from {request.remote_addr}); previous version {result['backup']}")
        summary = "Saved."
        if restart:
            ctx.nav.clear("AIO NAV restart requested (lever arms changed)")
            try:
                r = ctx.actions.run("restart_process", process_control.GROUP)
                ok = bool(r.get("success"))
            except ActionError as e:
                ok, r = False, {"summary": str(e)}
            summary += (" AIO NAV restarted with them; it aligns again: keep the vehicle still." if ok else
                        " Saved, but AIO NAV did not restart (" + (r.get("summary") or "") + "): they apply at its next start.")
            return jsonify({"success": ok, "saved": True, "summary": summary, "version": result["version"]})
        if f["active"] and nav_running():
            summary += " They apply when AIO NAV starts again (Restart, or the next power-on)."
        elif not f["active"]:
            summary += " They apply when the Live mode is used again."
        else:
            summary += " They apply when AIO NAV starts."
        return jsonify({"success": True, "saved": True, "summary": summary, "version": result["version"]})
