"""Product UI (:8080): Overview, Navigation, Data, Settings. No login.

It only reads, except AIO NAV Start / Stop and the two lever arms on Settings (each behind a
config switch).
"""

from flask import abort, jsonify, render_template, request, send_file

from ..actions import aionav_config as ac
from ..actions import process_control
from ..actions.registry import ActionError
from ..data_access.files import DataAccessError
from .common import create_base_app


def create_product_app(ctx):
    app = create_base_app("aio_product", ctx)

    @app.route("/")
    def overview():
        return render_template("product/overview.html", page="overview")

    @app.route("/navigation")
    def navigation():
        return render_template("product/navigation.html", page="navigation")

    @app.route("/data")
    def data_page():
        return render_template("product/data.html", page="data")

    @app.route("/settings")
    def settings_page():
        return render_template("product/settings.html", page="settings")

    @app.route("/api/state")
    def api_state():
        return jsonify(ctx.store.get("product", {}) or {})

    @app.route("/api/nav/control", methods=["POST"])
    def api_nav_control():
        """Start / stop / restart AIO NAV (with the programs that go with it, like the AIO Nav app)."""
        if request.headers.get("X-Requested-With") != "aio-dashboard" or not request.is_json:
            return jsonify({"success": False, "summary": "bad request"}), 400
        if not ctx.cfg["nav"]["allow_control"]:
            return jsonify({"success": False, "summary": "control is disabled"}), 403
        verb = (request.get_json(silent=True) or {}).get("action")
        action_id = {"start": "start_process", "stop": "stop_process",
                     "restart": "restart_process"}.get(verb)
        if action_id is None:
            return jsonify({"success": False, "summary": "action must be start, stop or restart"}), 400
        nav_svc = (ctx.store.get("services", {}) or {}).get(ctx.cfg["nav"]["service"]) or {}
        if verb == "restart" or (verb == "start" and not nav_svc.get("pids")):
            ctx.nav.clear(f"AIO NAV {verb} requested")   # a new run starts from the default state
        try:
            result = ctx.actions.run(action_id, process_control.GROUP)
            return jsonify(process_control.user_result(ctx.cfg, verb, result))
        except ActionError as e:
            return jsonify({"success": False, "summary": str(e)}), e.status

    # ------------------------------------------------------------------ lever arms (Settings page)
    def leverarm_file():
        """The config file of the mode in use, and its text."""
        mode = ctx.cfg.ros_mode()
        if not mode:
            raise ac.ConfigError("no AIO NAV settings file is in use on this device")
        f = ac.file_for(ctx.cfg, mode)
        with open(f["path"], encoding="utf-8") as fh:
            return f, fh.read()

    def nav_running():
        return bool(((ctx.store.get("services", {}) or {}).get(ctx.cfg["nav"]["service"]) or {}).get("pids"))

    @app.route("/api/settings/leverarm")
    def api_leverarm_get():
        if not ctx.cfg["nav"]["allow_leverarm_edit"]:
            return jsonify({"enabled": False})
        try:
            f, text = leverarm_file()
            values = ac.form_values(text)
        except Exception as e:                                   # missing or unreadable file
            return jsonify({"enabled": True, "error": str(e)})
        by = {".".join(x["path"]): x for x in ac.FIELDS}
        return jsonify({"enabled": True, "file": {"name": f["name"], "label": f["label"], "mtime_ns": f["mtime_ns"]},
                        "fields": [{"key": k, "label": by[k]["label"], "help": by[k]["help"], "value": values.get(k)}
                                   for k in ac.USER_FIELDS],
                        "running": nav_running()})

    @app.route("/api/settings/leverarm", methods=["POST"])
    def api_leverarm_save():
        if request.headers.get("X-Requested-With") != "aio-dashboard" or not request.is_json:
            return jsonify({"success": False, "summary": "bad request"}), 400
        if not ctx.cfg["nav"]["allow_leverarm_edit"]:
            return jsonify({"success": False, "summary": "changing the lever arms is turned off on this device"}), 403
        b = request.get_json(silent=True) or {}
        values = b.get("values")
        if not isinstance(values, dict) or not values or set(values) - set(ac.USER_FIELDS):
            return jsonify({"success": False, "summary": "only the lever arms can be changed here"}), 400
        try:
            f, text = leverarm_file()
            new_text = ac.apply_form(text, values)
            old, new = ac.form_values(text), ac.form_values(new_text)
            result = ac.save(ctx.cfg, f["mode"], new_text, b.get("base_mtime_ns"))
        except ac.ConfigError as e:
            return jsonify({"success": False, "summary": str(e)}), 409
        except OSError as e:
            return jsonify({"success": False, "summary": f"cannot write the settings file: {e.strerror or e}"}), 500
        if not result["changed"]:
            return jsonify({"success": True, "changed": False, "summary": "No changes."})
        detail = "; ".join(f"{k}: {old.get(k)} -> {new.get(k)}" for k in ac.USER_FIELDS if old.get(k) != new.get(k))
        ctx.events.add("warning", "action", f"Lever arms changed in {f['name']} (Settings page)",
                       f"{detail} from {request.remote_addr}; previous version {result['backup']}")
        summary = ("Saved. AIO NAV uses the new lever arms after it is restarted." if nav_running()
                   else "Saved. AIO NAV uses the new lever arms the next time it starts.")
        return jsonify({"success": True, "changed": True, "summary": summary})

    @app.route("/api/trajectory")
    def api_trajectory():
        session = request.args.get("session", type=int)
        cursor = request.args.get("cursor", default=0, type=int)
        return jsonify(ctx.nav.trajectory.snapshot(session, cursor))

    @app.route("/api/data/roots")
    def api_data_roots():
        return jsonify(ctx.data.list_roots())

    @app.route("/api/data/list")
    def api_data_list():
        try:
            return jsonify(ctx.data.listing(request.args.get("root", ""),
                                            request.args.get("path", "")))
        except DataAccessError as e:
            return jsonify({"error": str(e)}), e.status
        except OSError as e:
            return jsonify({"error": f"cannot read directory: {e.strerror}"}), 500

    @app.route("/data/download/<root_id>/<path:rel>")
    def data_download(root_id, rel):
        try:
            path = ctx.data.file_for_download(root_id, rel)
        except DataAccessError as e:
            abort(e.status)
        return send_file(path, as_attachment=True, download_name=path.name, max_age=0)

    return app
