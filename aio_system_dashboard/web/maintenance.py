"""Maintenance UI (:8081): engineering pages and whitelisted actions."""

from flask import Response, abort, jsonify, render_template, request

from ..actions.registry import ActionError
from ..media.tap import PreviewError, meta_header
from .common import create_base_app

PAGES = ("overview", "system", "ros", "camera", "lidar", "network", "diagnostics")


def create_maintenance_app(ctx):
    app = create_base_app("aio_maintenance", ctx)

    def page_context():
        return {
            "devices": ctx.cfg["devices"],
            "services_cfg": ctx.cfg["services"],
            "actions": ctx.actions.describe(),
            "previews": ctx.preview.describe(),
        }

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
            "product": {k: product.get(k) for k in ("health", "udp", "nav", "indicators", "aiding")},
            "config": {
                "aio_nav": ctx.cfg.aio_nav,
                "nav_bind": ctx.cfg["nav"]["udp_bind"],
                "fake": ctx.cfg.fake,
            },
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
        try:
            return jsonify(ctx.actions.run(action_id, target))
        except ActionError as e:
            return jsonify({"success": False, "summary": str(e)}), e.status

    return app
