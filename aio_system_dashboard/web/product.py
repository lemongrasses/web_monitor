"""Product UI (:8080): Overview, Navigation, Data. No login, read-only."""

from flask import abort, jsonify, render_template, request, send_file

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
        try:
            result = ctx.actions.run(action_id, process_control.GROUP)
            return jsonify(process_control.user_result(ctx.cfg, verb, result))
        except ActionError as e:
            return jsonify({"success": False, "summary": str(e)}), e.status

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
