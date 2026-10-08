"""Password lock for the Maintenance tools that change the system (config editor, terminal).

* The password is set on the device with `aio-dashboard password`; only a salted PBKDF2 hash is
  stored (config/maint_password, readable by root and the service user). Without a password the
  tools stay locked: there is no default password.
* Unlocking gives a session (HttpOnly, SameSite=Strict cookie, bound to the client IP) that ends
  after `maintenance.unlock_minutes` without use, or with Lock.
* Repeated wrong passwords from one address are slowed down (a short lock-out).
"""

import functools
import hashlib
import hmac
import os
import secrets
import threading
import time
from typing import Dict, Optional, Tuple

from flask import jsonify, request

COOKIE = "aio_maint_session"
ITERATIONS = 200_000
MAX_FAILURES, FAILURE_WINDOW_S, LOCKOUT_S = 5, 300.0, 60.0


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = stored.strip().split("$")
        if algo != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(got.hex(), digest)
    except (ValueError, TypeError):
        return False


class Guard:
    def __init__(self, password_file: str, idle_s: float = 900.0, events=None, clock=time.monotonic):
        self.password_file = password_file
        self.idle_s = idle_s
        self.events = events
        self.clock = clock
        self._lock = threading.Lock()
        self._sessions: Dict[str, Tuple[str, float]] = {}       # token -> (client ip, expires)
        self._failures: Dict[str, list] = {}                     # ip -> failure times

    # ------------------------------------------------------------------ password
    def _stored(self) -> Optional[str]:
        try:
            with open(self.password_file, encoding="utf-8") as f:
                return f.read().strip() or None
        except OSError:
            return None

    def configured(self) -> bool:
        return self._stored() is not None

    # ------------------------------------------------------------------ sessions
    def _session(self) -> Optional[str]:
        token = request.cookies.get(COOKIE)
        if not token:
            return None
        now = self.clock()
        with self._lock:
            entry = self._sessions.get(token)
            if not entry or entry[0] != request.remote_addr or entry[1] < now:
                self._sessions.pop(token, None)
                return None
            return token

    def unlocked(self) -> bool:
        return self._session() is not None

    def touch(self) -> None:
        token = self._session()
        if token:
            with self._lock:
                self._sessions[token] = (request.remote_addr, self.clock() + self.idle_s)

    def expires_in(self) -> Optional[float]:
        token = self._session()
        with self._lock:
            return max(0.0, self._sessions[token][1] - self.clock()) if token in self._sessions else None

    def _event(self, level: str, title: str) -> None:
        if self.events is not None:
            self.events.add(level, "security", title, f"from {request.remote_addr}")

    def unlock(self, password: str):
        ip, now = request.remote_addr, self.clock()
        with self._lock:
            recent = [t for t in self._failures.get(ip, []) if now - t < FAILURE_WINDOW_S]
            self._failures[ip] = recent
            if len(recent) >= MAX_FAILURES and now - recent[-1] < LOCKOUT_S:
                wait = LOCKOUT_S - (now - recent[-1])
                return jsonify({"success": False, "summary": f"too many wrong passwords; try again in {wait:.0f} s"}), 429
        stored = self._stored()
        if stored is None:
            return jsonify({"success": False, "summary": "no password set on this device: run "
                                                         "'aio-dashboard password' on it first"}), 403
        if not isinstance(password, str) or not check_password(password, stored):
            with self._lock:
                self._failures.setdefault(ip, []).append(now)
            self._event("warning", "Maintenance sign-in failed")
            return jsonify({"success": False, "summary": "wrong password"}), 403
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._failures.pop(ip, None)
            self._sessions = {t: e for t, e in self._sessions.items() if e[1] > now}   # drop expired
            self._sessions[token] = (ip, now + self.idle_s)
        self._event("info", "Maintenance unlocked")
        resp = jsonify({"success": True, "summary": "unlocked", "expires_in_s": self.idle_s})
        resp.set_cookie(COOKIE, token, httponly=True, samesite="Strict", max_age=int(self.idle_s) + 60, path="/")
        return resp

    def lock(self):
        token = request.cookies.get(COOKIE)
        with self._lock:
            self._sessions.pop(token or "", None)
        resp = jsonify({"success": True, "summary": "locked"})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    def status(self) -> Dict:
        return {"configured": self.configured(), "unlocked": self.unlocked(),
                "expires_in_s": self.expires_in(), "idle_s": self.idle_s}

    def required(self, fn):
        """Decorator: the route needs an unlocked session (and extends it)."""
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            if not self.unlocked():
                return jsonify({"success": False, "locked": True,
                                "summary": "locked: unlock the maintenance tools with the password"}), 401
            self.touch()
            return fn(*a, **kw)
        return wrapper


def default_password_file(cfg) -> str:
    from ..config import resolve_path
    return str(resolve_path(cfg["maintenance"].get("password_file") or "config/maint_password"))


def write_password_file(path: str, password: str) -> None:
    """Used by `aio-dashboard password` (run as root): write the hash, readable by the service."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w") as f:
        f.write(hash_password(password) + "\n")
    os.replace(tmp, path)
