"""Maintenance tools that change the system: the AIO NAV config editor and the terminal.

Both need the maintenance password (web/auth.py). Changes are recorded in the event log.
"""

import base64
import logging

from flask import Response, jsonify, render_template, request, stream_with_context

from ..actions import aionav_config as ac
from ..actions import process_control
from ..actions.terminal import TerminalManager
from .auth import Guard, default_password_file

logger = logging.getLogger(__name__)


def register_tools(ctx, app, page_context) -> None:
    mcfg = ctx.cfg["maintenance"]
    guard = Guard(default_password_file(ctx.cfg), idle_s=float(mcfg.get("unlock_minutes", 15)) * 60,
                  events=ctx.events)
    terms = TerminalManager(ctx.cfg.state_file("terminals.json"), ctx.events)
    closed = terms.cleanup_orphans()
    if closed:
        logger.info("closed %d terminal(s) left by an earlier run", closed)
    ctx.guard, ctx.terminals = guard, terms

    def bad_request():
        return request.headers.get("X-Requested-With") != "aio-dashboard" or not request.is_json

    def body():
        return request.get_json(silent=True) or {}

    # ------------------------------------------------------------------ password lock
    @app.route("/api/maint/auth")
    def api_auth():
        scope = "tools" if str(mcfg.get("lock", "all")) == "tools" else "all"
        return jsonify(dict(guard.status(), scope=scope))

    @app.route("/api/maint/auth/unlock", methods=["POST"])
    def api_unlock():
        if bad_request():
            return jsonify({"success": False, "summary": "bad request"}), 400
        return guard.unlock(body().get("password"))

    @app.route("/api/maint/auth/lock", methods=["POST"])
    def api_lock():
        if bad_request():
            return jsonify({"success": False, "summary": "bad request"}), 400
        return guard.lock()

    # ------------------------------------------------------------------ AIO NAV config editor
    @app.route("/config")
    def config_page():
        return render_template("maintenance/config.html", page="config", **page_context())

    @app.route("/api/maint/aionav")
    def api_aionav_files():
        try:
            files = ac.files(ctx.cfg)
        except OSError as e:
            return jsonify({"files": [], "fields": ac.FIELDS, "error": str(e)})
        return jsonify({"files": [{k: v for k, v in f.items()} for f in files], "fields": ac.FIELDS})

    def load(mode):
        f = ac.file_for(ctx.cfg, mode)
        with open(f["path"], encoding="utf-8") as fh:
            return f, fh.read()

    def new_text(text, b):
        """The text a request wants to save: raw text as typed, or form changes applied to the file."""
        if isinstance(b.get("text"), str):
            return b["text"]
        return ac.apply_form(text, b.get("form") or {})

    @app.route("/api/maint/aionav/<mode>")
    def api_aionav_get(mode):
        try:
            f, text = load(mode)
        except (ac.ConfigError, OSError) as e:
            return jsonify({"success": False, "summary": str(e)}), 404
        try:
            values = ac.form_values(text)
        except Exception as e:                                   # a broken file still opens in the raw editor
            values, err = {}, str(e)
        else:
            err = None
        errors, warnings = ac.validate(text)
        return jsonify({"success": True, "file": f, "text": text, "values": values, "parse_error": err,
                        "errors": errors, "warnings": warnings, "backups": ac.backups(ctx.cfg, f["name"])})

    @app.route("/api/maint/aionav/<mode>/preview", methods=["POST"])
    def api_aionav_preview(mode):
        if bad_request():
            return jsonify({"success": False, "summary": "bad request"}), 400
        try:
            f, text = load(mode)
            new = new_text(text, body())
        except (ac.ConfigError, OSError) as e:
            return jsonify({"success": False, "summary": str(e)})
        errors, warnings = ac.validate(new)
        return jsonify({"success": not errors, "diff": ac.diff(text, new, f["name"]), "text": new,
                        "errors": errors, "warnings": warnings, "changed": new != text})

    @app.route("/api/maint/aionav/<mode>/save", methods=["POST"])
    @guard.required
    def api_aionav_save(mode):
        if bad_request():
            return jsonify({"success": False, "summary": "bad request"}), 400
        b = body()
        try:
            f, text = load(mode)
        except (ac.ConfigError, OSError) as e:
            return jsonify({"success": False, "summary": str(e)}), 404
        try:
            result = ac.save(ctx.cfg, mode, new_text(text, b), b.get("base_mtime_ns"))
        except ac.ConfigError as e:
            return jsonify({"success": False, "summary": str(e)}), 409
        except OSError as e:
            return jsonify({"success": False, "summary": f"cannot write {f['name']}: {e}"}), 500
        if not result["changed"]:
            return jsonify({"success": True, "summary": "no changes", **result})
        lines = sum(1 for l in result["diff"].splitlines() if l[:1] == "+" and l[:3] != "+++")
        ctx.events.add("warning", "action", f"AIO NAV config {f['name']} saved",
                       f"{lines} line(s) changed from {request.remote_addr}; previous version {result['backup']}")
        summary = f"saved {f['name']} (previous version kept as {result['backup']})"
        nav = (ctx.store.get("services", {}) or {}).get(ctx.cfg["nav"]["service"]) or {}
        if b.get("restart") and f["active"] and nav.get("pids"):
            r = ctx.actions.run("restart_process", process_control.GROUP)
            summary += "; AIO NAV " + ("restarted" if r.get("success") else "restart failed: " + r.get("summary", ""))
        elif f["active"] and nav.get("pids"):
            summary += "; it applies when AIO NAV is restarted"
        elif not f["active"]:
            summary += "; it applies when this mode is used"
        return jsonify({"success": True, "summary": summary, **result})

    @app.route("/api/maint/aionav/<mode>/backup/<backup_id>")
    def api_aionav_backup(mode, backup_id):
        try:
            f = ac.file_for(ctx.cfg, mode)
            return jsonify({"success": True, "text": ac.read_backup(ctx.cfg, f["name"], backup_id)})
        except ac.ConfigError as e:
            return jsonify({"success": False, "summary": str(e)}), 404

    # ------------------------------------------------------------------ terminal
    @app.route("/terminal")
    def terminal_page():
        return render_template("maintenance/terminal.html", page="terminal", **page_context())

    def own(sid):
        s = terms.get(sid)
        return s if s is not None and s.ip == request.remote_addr else None   # only the browser that opened it

    @app.route("/api/maint/term", methods=["GET", "POST"])
    @guard.required
    def api_term():
        if request.method == "GET":
            return jsonify([t for t in terms.list() if t["ip"] == request.remote_addr])
        if bad_request():
            return jsonify({"success": False, "summary": "bad request"}), 400
        b = body()
        return jsonify(terms.open(request.remote_addr, int(b.get("cols") or 120), int(b.get("rows") or 32)))

    @app.route("/api/maint/term/<sid>/stream")
    @guard.required
    def api_term_stream(sid):
        s = own(sid)
        if s is None:
            return jsonify({"success": False, "summary": "no such terminal"}), 404
        try:
            since = int(request.headers.get("Last-Event-ID") or request.args.get("since") or 0)
        except ValueError:
            since = 0

        def events():
            pos = since
            yield "retry: 1000\n\n"
            while True:
                data, pos = s.read(pos, 15.0)
                if data:
                    yield f"id: {pos}\nevent: out\ndata: {base64.b64encode(data).decode()}\n\n"
                elif not s.alive:
                    yield f"event: exit\ndata: {s.exit_code if s.exit_code is not None else ''}\n\n"
                    return
                else:
                    yield ": keep-alive\n\n"

        return Response(stream_with_context(events()), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.route("/api/maint/term/<sid>/input", methods=["POST"])
    @guard.required
    def api_term_input(sid):
        if bad_request():
            return jsonify({"success": False, "summary": "bad request"}), 400
        s = own(sid)
        data = body().get("data")
        if s is None or not isinstance(data, str) or len(data) > 65536:
            return jsonify({"success": False, "summary": "no such terminal"}), 404
        return jsonify({"success": terms.write(sid, data)})

    @app.route("/api/maint/term/<sid>/resize", methods=["POST"])
    @guard.required
    def api_term_resize(sid):
        if bad_request():
            return jsonify({"success": False, "summary": "bad request"}), 400
        if own(sid) is None:
            return jsonify({"success": False, "summary": "no such terminal"}), 404
        b = body()
        return jsonify({"success": terms.resize(sid, int(b.get("cols") or 80), int(b.get("rows") or 24))})

    @app.route("/api/maint/term/<sid>", methods=["DELETE"])
    @guard.required
    def api_term_close(sid):
        if request.headers.get("X-Requested-With") != "aio-dashboard":
            return jsonify({"success": False, "summary": "bad request"}), 400
        if own(sid) is None:
            return jsonify({"success": False, "summary": "no such terminal"}), 404
        return jsonify({"success": terms.close(sid)})
