"""Shared Flask setup for the product and maintenance apps."""

import math
from pathlib import Path

from flask import Flask
from flask.json.provider import DefaultJSONProvider

PKG_DIR = Path(__file__).resolve().parent.parent


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
    app.extensions["dashboard"] = ctx

    @app.context_processor
    def _inject():
        return {"fake_mode": ctx.cfg.fake, "hostname": ctx.hostname,
                "devices": ctx.cfg["devices"]}

    @app.after_request
    def _no_cache_api(resp):
        if resp.mimetype == "application/json":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    return app
