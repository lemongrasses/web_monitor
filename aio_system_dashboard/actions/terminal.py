"""Shell sessions for the Maintenance terminal page.

Each session is an interactive shell of the service user on its own pseudo-terminal, so job
control, Ctrl+C, colors and full-screen programs (top, vim) work. It reads /etc/profile and then
~/.bashrc, like a desktop terminal (a login shell would skip ~/.bashrc when there is no
~/.profile, and with it the ROS setup). The browser gets the output as
a stream with byte offsets (it can reconnect and continue) and sends keystrokes and size changes.

* At most `max_sessions` at once; a session with no input for `idle_s` is closed.
* The shell gets a clean environment (not the dashboard's), so the user's own .bashrc decides ROS
  and Python settings.
* Shells are marked (AIO_DASHBOARD_TERMINAL=1) and recorded in state/terminals.json, so leftovers
  of a dashboard that died are closed at the next start.
"""

import fcntl
import json
import logging
import os
import pty
import pwd
import secrets
import signal
import struct
import termios
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

MARK = "AIO_DASHBOARD_TERMINAL"
RCFILE = """# Written by the AIO dashboard: start-up file of its terminal shells.
[ -r /etc/profile ] && . /etc/profile
[ -r "$HOME/.bashrc" ] && . "$HOME/.bashrc"
"""
KEEP_BYTES = 512 * 1024          # output kept per session for reconnects


class Session:
    def __init__(self, sid: str, pid: int, fd: int, ip: str):
        self.id, self.pid, self.fd, self.ip = sid, pid, fd, ip
        self.buf = bytearray()
        self.base = 0                     # stream offset of buf[0]
        self.alive = True
        self.exit_code: Optional[int] = None
        self.started = time.time()
        self.last_input = time.monotonic()
        self.cond = threading.Condition()

    @property
    def end(self) -> int:
        return self.base + len(self.buf)

    def append(self, data: bytes) -> None:
        with self.cond:
            self.buf += data
            if len(self.buf) > KEEP_BYTES:
                drop = len(self.buf) - KEEP_BYTES
                del self.buf[:drop]
                self.base += drop
            self.cond.notify_all()

    def read(self, since: int, wait_s: float) -> Tuple[bytes, int]:
        """Output after offset `since` (waits up to wait_s for some). Returns (data, new offset)."""
        with self.cond:
            if since >= self.end and self.alive:
                self.cond.wait(wait_s)
            since = max(since, self.base)
            return bytes(self.buf[since - self.base:]), self.end


class TerminalManager:
    def __init__(self, state_file: Path, events=None, max_sessions: int = 3, idle_s: float = 1800.0):
        self.state_file = state_file
        self.events = events
        self.max_sessions = max_sessions
        self.idle_s = idle_s
        self._lock = threading.Lock()
        self.sessions: Dict[str, Session] = {}
        self._stop = threading.Event()
        self.rcfile = state_file.with_name("terminal_bashrc")
        threading.Thread(target=self._janitor, name="term-janitor", daemon=True).start()

    # ------------------------------------------------------------------ bookkeeping
    def _record(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps([s.pid for s in self.sessions.values() if s.alive]) + "\n")
        except OSError:
            pass

    def cleanup_orphans(self) -> int:
        """Close shells left by an earlier dashboard process (only ones carrying our mark)."""
        try:
            pids = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return 0
        closed = 0
        for pid in pids if isinstance(pids, list) else []:
            try:
                env = Path(f"/proc/{int(pid)}/environ").read_bytes().split(b"\0")
            except (OSError, ValueError):
                continue
            if f"{MARK}=1".encode() in env:
                try:
                    os.killpg(int(pid), signal.SIGHUP)
                    closed += 1
                except OSError:
                    pass
        self._record()
        return closed

    def _event(self, level: str, title: str, detail: str) -> None:
        if self.events is not None:
            self.events.add(level, "security", title, detail)

    # ------------------------------------------------------------------ sessions
    @staticmethod
    def _shell_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        pw = pwd.getpwuid(os.getuid())
        env = {"HOME": pw.pw_dir, "USER": pw.pw_name, "LOGNAME": pw.pw_name, "SHELL": "/bin/bash",
               "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
               "LANG": os.environ.get("LANG", "C.UTF-8"), "TERM": "xterm-256color", "COLORTERM": "truecolor",
               MARK: "1"}
        env.update(extra or {})
        return env

    def open(self, ip: str, cols: int = 120, rows: int = 32) -> Dict:
        with self._lock:
            live = [s for s in self.sessions.values() if s.alive]
            if len(live) >= self.max_sessions:
                return {"success": False, "summary": f"at most {self.max_sessions} terminals at once; close one first"}
            env = self._shell_env()
            try:
                self.rcfile.parent.mkdir(parents=True, exist_ok=True)
                self.rcfile.write_text(RCFILE)
                argv = ["bash", "--rcfile", str(self.rcfile), "-i"]
            except OSError:
                argv = ["bash", "-l"]
            pid, fd = pty.fork()
            if pid == 0:                                  # child: becomes the shell right away
                try:
                    os.chdir(env["HOME"])
                    os.execvpe("bash", argv, env)
                finally:
                    os._exit(127)
            sid = secrets.token_hex(8)
            s = Session(sid, pid, fd, ip)
            self.sessions[sid] = s
            self._record()
        self.resize(sid, cols, rows)
        threading.Thread(target=self._pump, args=(s,), name=f"term-{sid}", daemon=True).start()
        self._event("warning", "Terminal opened", f"from {ip} (pid {pid})")
        return {"success": True, "id": sid}

    def _pump(self, s: Session) -> None:
        while True:
            try:
                data = os.read(s.fd, 65536)
            except OSError:
                data = b""
            if not data:
                break
            s.append(data)
        try:
            _, status = os.waitpid(s.pid, 0)
            s.exit_code = os.waitstatus_to_exitcode(status)
        except ChildProcessError:
            pass
        try:
            os.close(s.fd)
        except OSError:
            pass
        with s.cond:
            s.alive = False
            s.cond.notify_all()
        with self._lock:
            self._record()
        self._event("info", "Terminal closed", f"pid {s.pid}, exit {s.exit_code}")

    def get(self, sid: str) -> Optional[Session]:
        return self.sessions.get(sid)

    def write(self, sid: str, data: str) -> bool:
        s = self.get(sid)
        if not s or not s.alive:
            return False
        s.last_input = time.monotonic()
        raw = data.encode("utf-8", errors="replace")
        while raw:
            n = os.write(s.fd, raw)
            raw = raw[n:]
        return True

    def resize(self, sid: str, cols: int, rows: int) -> bool:
        s = self.get(sid)
        if not s or not s.alive:
            return False
        cols, rows = max(20, min(int(cols), 500)), max(5, min(int(rows), 200))
        try:
            fcntl.ioctl(s.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            return False
        return True

    def close(self, sid: str) -> bool:
        s = self.get(sid)
        if not s:
            return False
        if s.alive:
            for sig in (signal.SIGHUP, signal.SIGKILL):
                try:
                    os.killpg(s.pid, sig)
                except OSError:
                    break
                for _ in range(20):
                    if not s.alive:
                        break
                    time.sleep(0.05)
                if not s.alive:
                    break
        with self._lock:
            self.sessions.pop(sid, None)
            self._record()
        return True

    def list(self) -> list:
        return [{"id": s.id, "pid": s.pid, "alive": s.alive, "started": s.started, "ip": s.ip,
                 "idle_s": round(time.monotonic() - s.last_input)} for s in self.sessions.values()]

    def _janitor(self) -> None:
        while not self._stop.wait(30.0):
            now = time.monotonic()
            for s in list(self.sessions.values()):
                if s.alive and now - s.last_input > self.idle_s:
                    self._event("info", "Terminal closed after inactivity", f"pid {s.pid}")
                    self.close(s.id)
                elif not s.alive and now - s.last_input > 600:
                    with self._lock:
                        self.sessions.pop(s.id, None)        # forget finished sessions after a while

    def shutdown(self) -> None:
        self._stop.set()
        for sid in list(self.sessions):
            self.close(sid)
