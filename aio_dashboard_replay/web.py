"""Routes of the replay module on the Maintenance app (:8081)."""

import os

from flask import Blueprint, jsonify, render_template, request

from aio_system_dashboard import modules
from aio_system_dashboard.actions import process_control
from aio_system_dashboard.actions.registry import ActionError
from aio_system_dashboard.config import resolve_path

from . import DEFAULTS, NAME, TITLE
from . import mode as ros_mode
from .bags import BagIndex
from .player import (OPTIONS, OptionError, Player, command_text, defaults, find_ros_setup, preconditions,
                     process_domain, validate)


def register(ctx, app) -> None:
    s = modules.settings(ctx.cfg.data, NAME, DEFAULTS)
    index = BagIndex(s["bag_roots"], int(s["scan_depth"]))
    player = Player(ctx.cfg.state_file("bag_play.json"), resolve_path(s["log_file"]),
                    ctx.cfg.state_file("bag_presets.json"), ctx.events)
    here = os.path.dirname(os.path.abspath(__file__))
    bp = Blueprint("replay", __name__, template_folder=os.path.join(here, "templates"),
                   static_folder=os.path.join(here, "static"), static_url_path="/replay-static")
    ctx.replay = player                     # for tests and diagnostics

    def setup() -> str:
        return find_ros_setup(s["ros_setup"], ctx.cfg.aio_nav.get("path", "")) or ""

    def env():
        e = process_control.child_env(ctx.cfg)
        e.pop("DISPLAY", None)
        return e

    def driver_domains(key):
        """Domains the driver's processes run in; None if none could be read."""
        found = {process_domain(p) for p in process_control.running_pids(ctx.cfg, key)}
        found.discard(None)
        return found or None

    def bad_request():
        return request.headers.get("X-Requested-With") != "aio-dashboard" or not request.is_json

    def bag_or_404(bid):
        bag = index.get(bid or "")
        if bag is None:
            return None, (jsonify({"success": False, "summary": "bag not found"}), 404)
        return bag, None

    @bp.route("/replay")
    def page():
        return render_template("replay/replay.html", page=NAME, **ctx.maint_page_context())

    @bp.route("/api/replay/bags")
    def api_bags():
        bags = index.all(refresh=request.args.get("refresh") == "1")
        return jsonify({"roots": [os.path.expanduser(str(r)) for r in s["bag_roots"]],
                        "bags": [{k: v for k, v in b.items() if k != "topics"} | {"topic_count": len(b["topics"])}
                                 for b in bags]})

    @bp.route("/api/replay/bag/<bid>")
    def api_bag(bid):
        bag, err = bag_or_404(bid)
        if err:
            return err
        last = player.presets()["last"].get(bid)
        return jsonify(dict(bag, last_options=last))

    @bp.route("/api/replay/status")
    def api_status():
        st = player.status()
        services = ctx.store.get("services", {}) or {}
        items, ok = preconditions(ctx.cfg, services, st.get("status") == "playing", driver_domains)
        nav = services.get(ctx.cfg["nav"]["service"]) or {}
        return jsonify({"player": st, "checks": items, "ready": ok and bool(setup()),
                        "ros_setup": setup(), "mode": ctx.cfg.ros_mode_info(),
                        "aio_nav_running": bool(nav.get("pids")), "log": player.log_tail(15)})

    @bp.route("/api/replay/options")
    def api_options():
        return jsonify({"options": OPTIONS, "defaults": defaults()})

    @bp.route("/api/replay/preview", methods=["POST"])
    def api_preview():
        body = request.get_json(silent=True) or {}
        bag, err = bag_or_404(body.get("bag"))
        if err:
            return err
        try:
            opts = validate(body.get("options") or {}, bag)
        except OptionError as e:
            return jsonify({"success": False, "summary": str(e)})
        info = ctx.cfg.ros_mode_info()
        bag_mode = next((m for m in info["modes"] if m["name"] == "bag"), {})
        e = {}
        if bag_mode.get("domain_id") is not None:
            e["ROS_DOMAIN_ID"] = str(bag_mode["domain_id"])
        if bag_mode.get("localhost_only") is not None:
            e["ROS_LOCALHOST_ONLY"] = "1" if bag_mode["localhost_only"] else "0"
        return jsonify({"success": True, "command": command_text(opts, bag["path"], e)})

    @bp.route("/api/replay/play", methods=["POST"])
    def api_play():
        if bad_request():
            return jsonify({"success": False, "summary": "bad request"}), 400
        body = request.get_json(silent=True) or {}
        bag, err = bag_or_404(body.get("bag"))
        if err:
            return err
        try:
            opts = validate(body.get("options") or {}, bag)
        except OptionError as e:
            return jsonify({"success": False, "summary": str(e)}), 400
        items, ok = preconditions(ctx.cfg, ctx.store.get("services", {}) or {}, player.running(),
                                  driver_domains)
        if not ok:
            return jsonify({"success": False, "summary": "not ready: " +
                            "; ".join(i["text"] for i in items if not i["ok"])}), 409
        if not setup():
            return jsonify({"success": False, "summary": "ROS setup.bash not found (modules.replay.ros_setup)"}), 500
        result = player.start(bag, opts, setup(), env(), os.path.dirname(bag["path"]))
        if result.get("success"):
            player.remember(bag, opts)
        return jsonify(result)

    @bp.route("/api/replay/stop", methods=["POST"])
    def api_stop():
        if bad_request():
            return jsonify({"success": False, "summary": "bad request"}), 400
        return jsonify(player.stop(float(s["stop_grace_s"])))

    @bp.route("/api/replay/control", methods=["POST"])
    def api_control():
        if bad_request():
            return jsonify({"success": False, "summary": "bad request"}), 400
        body = request.get_json(silent=True) or {}
        rate = body.get("rate")
        try:
            rate = float(rate) if rate is not None else None
        except (TypeError, ValueError):
            return jsonify({"success": False, "summary": "rate must be a number"}), 400
        return jsonify(player.control(str(body.get("action")), setup(), env(), rate))

    @bp.route("/api/replay/presets", methods=["GET", "POST", "DELETE"])
    def api_presets():
        if request.method == "GET":
            return jsonify(player.presets()["presets"])
        if bad_request():
            return jsonify({"success": False, "summary": "bad request"}), 400
        body = request.get_json(silent=True) or {}
        if request.method == "DELETE":
            return jsonify(player.delete_preset(str(body.get("name", ""))))
        bag, err = bag_or_404(body.get("bag"))
        if err:
            return err
        try:
            opts = validate(body.get("options") or {}, bag)
        except OptionError as e:
            return jsonify({"success": False, "summary": str(e)}), 400
        return jsonify(player.save_preset(str(body.get("name", "")), bag, opts))

    @bp.route("/api/replay/mode", methods=["POST"])
    def api_mode():
        """Live / Bag replay switch (moved here from the core: a product machine runs Live only)."""
        if bad_request():
            return jsonify({"success": False, "summary": "bad request"}), 400
        mode = (request.get_json(silent=True) or {}).get("mode")
        if not isinstance(mode, str):
            return jsonify({"success": False, "summary": "mode required"}), 400

        def stop_nav():
            if not process_control.members(ctx.cfg):
                return {"success": True}
            return ctx.actions.run("stop_process", process_control.GROUP)

        try:
            result = ros_mode.apply_mode(ctx.cfg, mode, stop_nav, ctx.request_restart, ctx.events)
        except ActionError as e:
            return jsonify({"success": False, "summary": str(e)}), e.status
        status = result.pop("status", 200)
        return jsonify(result), status

    app.register_blueprint(bp)
    ctx.maint_pages.append((NAME, TITLE, "/replay"))
