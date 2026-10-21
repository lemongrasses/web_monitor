"""Shared Flask setup for the product and maintenance apps."""

import ipaddress
import logging
import math
import os
from pathlib import Path

from flask import Flask, abort, jsonify, request
from flask.json.provider import DefaultJSONProvider

from .access import client_allowed, hotspot_clients, parse_allowed_clients
from .i18n import gettext

PKG_DIR = Path(__file__).resolve().parent.parent
logger = logging.getLogger(__name__)


MAX_REQUEST_BYTES = 1024 * 1024


def json_body() -> dict:
    """The request's JSON object, or {} for no body, bad JSON, a list or a plain value."""
    b = request.get_json(silent=True)
    return b if isinstance(b, dict) else {}


def _sanitize(obj):
    """Replace NaN/inf (valid in NAV floats, invalid in JSON) with null."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


class SafeJSONProvider(DefaultJSONProvider):
    sort_keys = False

    def dumps(self, obj, **kwargs):
        return super().dumps(_sanitize(obj), **kwargs)


def create_base_app(name: str, ctx) -> Flask:
    app = Flask(name, template_folder=str(PKG_DIR / "templates"),
                static_folder=str(PKG_DIR / "static"))
    app.json = SafeJSONProvider(app)
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600
    # Request bodies are small (a config file is ~4 KB): a larger one is refused (413) before it is
    # read, so a client cannot make the dashboard hold an unlimited body in memory.
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES

    @app.errorhandler(500)
    def _internal_error(e):
        # A bug must not hand the page an HTML error: the API answers with a short JSON reason
        # (the traceback is in the log).
        if request.path.startswith("/api/"):
            return jsonify({"success": False, "summary": "internal error (see the dashboard log)"}), 500
        return e
    app.extensions["dashboard"] = ctx
    app.jinja_env.globals["_"] = gettext       # English unless the user app chose a language

    # Static files may be cached for an hour, so every link carries the file's modification time
    # (?v=...): after an update the browser fetches the new CSS / JS at once instead of mixing
    # new pages with old code.
    stamps = {}

    @app.url_defaults
    def _static_version(endpoint, values):
        if endpoint != "static" and not endpoint.endswith(".static"):
            return
        filename = values.get("filename")
        if not filename or "v" in values:
            return
        if endpoint == "static":
            folder = app.static_folder
        else:
            bp = app.blueprints.get(endpoint.rsplit(".", 1)[0])
            folder = bp.static_folder if bp is not None else None
        if not folder:
            return
        path = os.path.join(folder, filename)
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            return
        key = (path, mtime)
        if key not in stamps:
            stamps[key] = format(mtime // 1_000_000, "x")
        values["v"] = stamps[key]

    allowed = ctx.cfg["access"]["allowed_clients"]
    nets = parse_allowed_clients(allowed)
    if allowed and not nets:
        logger.error("access.allowed_clients has no valid entry; only loopback can connect")
        nets = [ipaddress.ip_network("127.0.0.0/8")]
    if nets:
        nets += hotspot_clients(ctx.cfg)

    @app.before_request
    def _restrict_clients():
        if not client_allowed(request.remote_addr, nets):
            abort(403)

    @app.context_processor
    def _inject():
        return {"fake_mode": ctx.cfg.fake,
                "devices": ctx.cfg["devices"]}

    @app.after_request
    def _no_cache_api(resp):
        if resp.mimetype == "application/json":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    return app
