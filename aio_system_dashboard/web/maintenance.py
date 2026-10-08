"""Maintenance UI (:8081): engineering pages and whitelisted actions."""

import logging

from urllib.parse import quote

from flask import Response, abort, jsonify, redirect, render_template, request

from ..actions import process_control
from ..actions.registry import ActionError
from ..media.tap import PreviewError, meta_header
from .common import create_base_app
from .tools import register_tools

logger = logging.getLogger(__name__)

PAGES = ("overview", "system", "ros", "camera", "lidar", "network", "diagnostics")
NAV = [("overview", "Overview", "/"), ("system", "System", "/system"), ("ros", "ROS 2", "/ros"),
       ("camera", "Camera", "/camera"), ("lidar", "LiDAR", "/lidar"),
       ("network", "Network", "/network"), ("diagnostics", "Diagnostics", "/diagnostics"),
       ("config", "AIO NAV config", "/config"), ("terminal", "Terminal", "/terminal")]


def create_maintenance_app(ctx):
    app = create_base_app("aio_maintenance", ctx)

    def page_context():
        return {
            "devices": ctx.cfg["devices"],
            "services_cfg": ctx.cfg["services"],
            "actions": ctx.actions.describe(),
            "previews": ctx.preview.describe(),
            "modules": [m.NAME for m in ctx.modules],
        }

    # Sidebar: the core pages, then one per optional module (each module adds its own).
    ctx.maint_pages = list(NAV)
    ctx.maint_page_context = page_context

    @app.context_processor
    def _nav_items():
        return {"nav_items": ctx.maint_pages}

    @app.route("/")
    def index():
        return render_template("maintenance/overview.html", page="overview", **page_context())

    @app.route("/<page>")
    def page(page):
        if page not in PAGES:
            abort(404)
        if page in ("camera", "lidar"):
            return render_template("maintenance/device.html", page=page, device_key=page,
                                   **page_context())
        return render_template(f"maintenance/{page}.html", page=page, **page_context())

    @app.route("/api/maint/snapshot")
    def api_snapshot():
        product = ctx.store.get("product", {}) or {}
        return jsonify({
            "maint": ctx.store.get("maint", {}),
            "system": ctx.store.get("system", {}),
            "services": ctx.store.get("services", {}),
            "network": ctx.store.get("network", {}),
            "ros": ctx.store.get("ros", {}),
            "watchdog": ctx.store.get("watchdog", {}),
            "gnss": ctx.store.get("gnss", {}),
            "product": {k: product.get(k) for k in ("health", "udp", "nav", "indicators", "aiding")},
            "config": {
                "aio_nav": ctx.cfg.aio_nav,
                "nav_bind": ctx.cfg["nav"]["udp_bind"],
                "nav_service": ctx.cfg["nav"]["service"],
                "nav_group": process_control.members(ctx.cfg),
                "fake": ctx.cfg.fake,
                "ros_mode": ctx.cfg.ros_mode_info(),
            },
            "modules": [m.NAME for m in ctx.modules],
        })

    @app.route("/api/maint/preview/<device>")
    def api_preview(device):
        """Latest camera image or down-sampled point cloud (view only)."""
        ros = ctx.store.get("ros", {}) or {}
        try:
            payload, ctype, meta = ctx.preview.frame(device, bool(ros.get("available")),
                                                     ros.get("error") or "")
        except PreviewError as e:
            return jsonify({"error": str(e)}), e.status
        resp = Response(payload, mimetype=ctype)
        resp.headers["X-Preview-Meta"] = meta_header(meta)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/api/maint/events")
    def api_events():
        limit = min(request.args.get("limit", default=100, type=int), 500)
        return jsonify(ctx.events.recent(limit))

    @app.route("/api/maint/actions")
    def api_actions():
        return jsonify(ctx.actions.describe())

    @app.route("/api/maint/actions/<action_id>", methods=["POST"])
    def api_run_action(action_id):
        # Requiring a JSON body plus a custom header blocks plain cross-site form posts.
        if request.headers.get("X-Requested-With") != "aio-dashboard" or not request.is_json:
            return jsonify({"success": False, "summary": "bad request"}), 400
        body = request.get_json(silent=True) or {}
        target = body.get("target")
        if not isinstance(target, str):
            return jsonify({"success": False, "summary": "target required"}), 400
        nav_svc = (ctx.store.get("services", {}) or {}).get(ctx.cfg["nav"]["service"]) or {}
        if target in (process_control.GROUP, ctx.cfg["nav"]["service"]) and (
                action_id == "restart_process" or (action_id == "start_process" and not nav_svc.get("pids"))):
            ctx.nav.clear("AIO NAV start requested")     # a new run starts from the default state
        try:
            return jsonify(ctx.actions.run(action_id, target))
        except ActionError as e:
            return jsonify({"success": False, "summary": str(e)}), e.status

    register_tools(ctx, app, page_context)        # config editor + terminal (password protected)
    guard = ctx.guard

    # maintenance.lock: "all" (default) = the whole Maintenance site needs the password;
    # "tools" = only the config editor and the terminal do.
    lock_all = str(ctx.cfg["maintenance"].get("lock", "all")) != "tools"

    @app.before_request
    def _require_unlock():
        if not lock_all:
            return None
        path = request.path
        if path == "/login" or path.startswith(("/api/maint/auth", "/static/")):
            return None
        if guard.unlocked():
            # Opening pages and acting counts as use; the pages' own background refresh does not,
            # so a page left open still locks after maintenance.unlock_minutes.
            if request.method != "GET" or not path.startswith("/api/"):
                guard.touch()
            return None
        if path.startswith("/api/"):
            return jsonify({"success": False, "locked": True,
                            "summary": "locked: sign in to the maintenance view"}), 401
        return redirect("/login?next=" + quote(request.full_path.rstrip("?"), safe="/?=&"))

    @app.route("/login")
    def login():
        nxt = request.args.get("next") or "/"
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"                                 # only back to a page of this site
        if guard.unlocked() or not lock_all:
            return redirect(nxt)
        return render_template("maintenance/login.html", next_url=nxt, configured=guard.configured(),
                               minutes=round(guard.idle_s / 60))

    for m in ctx.modules:
        try:
            m.register(ctx, app)
        except Exception:                 # a broken module must not take the Maintenance page down
            logger.exception("module %s could not register its pages", getattr(m, "NAME", m))

    return app
