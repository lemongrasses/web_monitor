"""User UI (:8080): Overview, Navigation, Data, Setup, Settings. No login.

It may start / stop AIO NAV (nav.allow_control), set the lever arms (Setup), change Wi-Fi (only
from a browser connected by cable) and switch "RTK required for Ready" (with a confirm to turn it
off); everything else is read-only. The engineering detail stays on the Maintenance UI (:8081)."""

from flask import abort, g, jsonify, render_template, request, send_file

from ..actions import process_control
from ..actions.registry import ActionError
from ..data_access.files import DataAccessError
from . import i18n
from .common import create_base_app
from .settings_api import register_settings, rtk_view
from .setup import register_setup


def create_product_app(ctx):
    app = create_base_app("aio_product", ctx)

    # ------------------------------------------------------------------ language
    @app.before_request
    def _language():
        g.lang = i18n.pick_lang(request.cookies.get(i18n.COOKIE), request.accept_languages)

    @app.context_processor
    def _language_context():
        lang = g.get("lang", "en")
        return {"lang": lang, "langs": i18n.LANGS, "i18n_js": i18n.js_table(lang)}

    # Machine interfaces stay English: the readiness snapshot (vehicle integration), trajectories, files.
    untranslated_api = ("/api/readiness", "/api/trajectory", "/api/data/")

    @app.after_request
    def _translate_json(resp):
        lang = g.get("lang", "en")
        if (lang != "en" and resp.mimetype == "application/json" and request.path.startswith("/api/")
                and not request.path.startswith(untranslated_api)):
            data = resp.get_json(silent=True)
            if data is not None:
                resp.set_data(app.json.dumps(i18n.translate(data, lang)))
        return resp

    @app.route("/")
    def overview():
        return render_template("product/overview.html", page="overview")

    @app.route("/navigation")
    def navigation():
        return render_template("product/navigation.html", page="navigation")

    @app.route("/data")
    def data_page():
        return render_template("product/data.html", page="data")

    @app.route("/setup")
    def setup_page():
        return render_template("product/setup.html", page="setup")

    @app.route("/settings")
    def settings_page():
        return render_template("product/settings.html", page="settings")

    register_settings(ctx, app, "/api", "/api/settings/rtk", where="user page")

    @app.route("/api/settings")
    def api_settings():
        """What the Settings page shows besides Wi-Fi: Ready requirement, automatic start, vehicle."""
        auto = getattr(ctx, "autostart", None)
        tx = getattr(ctx, "status_udp", None)
        external = [d for d in ctx.cfg.nav_destinations() if d.get("kind") == "external"]
        return jsonify({
            "rtk": rtk_view(ctx.cfg),
            "autostart": auto.status() if auto is not None else {"enabled": False},
            "vehicle": {"nav": f'{external[0]["host"]}:{external[0]["port"]}' if external else None,
                        "status": tx.status() if tx is not None else {"enabled": False}},
            "control": bool(ctx.cfg["nav"]["allow_control"]),
        })

    @app.route("/api/state")
    def api_state():
        return jsonify(ctx.store.get("product", {}) or {})

    @app.route("/api/readiness")
    def api_readiness():
        """The readiness snapshot alone: the same data the 0x05 status packet carries."""
        snap = ctx.store.get("readiness")
        if not snap:
            return jsonify({"schema_version": 1, "state": "BOOTING", "reasons": ["System starting"]}), 503
        return jsonify(snap)

    @app.route("/api/nav/control", methods=["POST"])
    def api_nav_control():
        """Start / stop / restart AIO NAV (with the programs that go with it, like the AIO Nav app)."""
        if request.headers.get("X-Requested-With") != "aio-dashboard" or not request.is_json:
            return jsonify({"success": False, "summary": "bad request"}), 400
        if not ctx.cfg["nav"]["allow_control"]:
            return jsonify({"success": False, "summary": "control is disabled"}), 403
        b = request.get_json(silent=True)
        verb = b.get("action") if isinstance(b, dict) else None
        action_id = {"start": "start_process", "stop": "stop_process",
                     "restart": "restart_process"}.get(verb) if isinstance(verb, str) else None
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

    register_setup(ctx, app)
    return app
