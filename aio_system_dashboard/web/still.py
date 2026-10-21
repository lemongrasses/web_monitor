"""Maintenance > Still detection: the "vehicle still" limits, live, and measuring them on the vehicle.

Reading is open like the other pages; measuring and changing the limits need the maintenance
password (web/auth.py). Changes are recorded in the event log (state/still.py).
"""

from flask import jsonify, render_template, request

from ..state import still
from .common import json_body


def register_still(ctx, app, page_context) -> None:
    guard, limits, cal = ctx.guard, ctx.still_limits, ctx.still_calibration

    def bad_request():
        return request.headers.get("X-Requested-With") != "aio-dashboard" or not request.is_json

    def refuse(summary: str, status: int = 400):
        return jsonify({"success": False, "summary": summary}), status

    @app.route("/still")
    def still_page():
        return render_template("maintenance/still.html", page="still", **page_context())

    @app.route("/api/maint/still")
    def api_still():
        current = limits.get()
        auto = ctx.cfg["autostart"]
        motion, speed, gnss = ctx.health.motion_inputs()
        return jsonify({
            "limits": current, "defaults": still.defaults(ctx.cfg),
            "now": still.judge(motion, speed, gnss, current),
            "speed": speed or {}, "gnss": {k: (gnss or {}).get(k) for k in ("quality", "label")},
            "speed_topic": ctx.cfg["readiness"].get("still_speed_topic") or "",
            "hold_s": float(auto["still_stable_s"]), "require_still": bool(auto["require_still"]),
            "ranges": still.RANGES, "margin": still.MARGIN, "durations": still.DURATION_S,
            "calibration": cal.status(),
        })

    @app.route("/api/maint/still/measure", methods=["POST"])
    @guard.required
    def api_still_measure():
        if bad_request():
            return refuse("bad request")
        kind = json_body().get("kind")
        try:
            return jsonify(dict(cal.start(kind if isinstance(kind, str) else ""), success=True))
        except ValueError as e:
            return refuse(str(e))
        except RuntimeError as e:
            return refuse(str(e), 409)

    @app.route("/api/maint/still/cancel", methods=["POST"])
    @guard.required
    def api_still_cancel():
        if bad_request():
            return refuse("bad request")
        return jsonify(dict(cal.cancel(), success=True))

    @app.route("/api/maint/still/apply", methods=["POST"])
    @guard.required
    def api_still_apply():
        if bad_request():
            return refuse("bad request")
        values = json_body().get("values")
        try:
            new = still.check_values(values)
        except ValueError as e:
            return refuse(str(e), 422)
        proposal = cal.proposal
        measured = proposal is not None and all(abs(new[k] - proposal[k]) < 1e-9 for k in still.KEYS)
        current = limits.apply(new, "measured" if measured else "typed", request.remote_addr or "",
                               cal.measured if measured else None)
        cal.clear_proposal()
        return jsonify({"success": True, "limits": current, "summary": "New limits in use."})

    @app.route("/api/maint/still/reset", methods=["POST"])
    @guard.required
    def api_still_reset():
        if bad_request():
            return refuse("bad request")
        cal.clear_proposal()
        return jsonify({"success": True, "limits": limits.reset(request.remote_addr or ""),
                        "summary": "Back to the defaults from the settings."})
