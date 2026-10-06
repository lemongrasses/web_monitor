"""Fake subsystem state for development without a Jetson, ROS or sensors.

Reads a JSON file (hot-reloaded on mtime change). Edit it while the dashboard
runs to toggle services, device reachability and ROS topic health.
"""

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)


class FakeState:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._mtime = None
        self._data: Dict[str, Any] = {}

    def get(self) -> Dict[str, Any]:
        with self._lock:
            try:
                mtime = self.path.stat().st_mtime
                if mtime != self._mtime:
                    self._data = json.loads(self.path.read_text(encoding="utf-8"))
                    self._mtime = mtime
            except (OSError, ValueError) as e:
                logger.warning("fake state %s unreadable: %s", self.path, e)
            return self._data

    def service_state(self, key: str) -> str:
        return self.get().get("services", {}).get(key, "running")

    def set_service(self, key: str, state: str) -> None:
        """Persist a simulated service state (used by start/stop in fake mode)."""
        with self._lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                data.setdefault("services", {})[key] = state
                self.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                self._mtime = None
            except (OSError, ValueError) as e:
                logger.warning("fake state %s not writable: %s", self.path, e)

    def reachable(self, device: str) -> bool:
        return bool(self.get().get("devices", {}).get(device, True))

    def topic(self, name: str) -> Dict[str, Any]:
        """Return {'state': healthy|low_rate|stale|missing, 'hz': float|None} for a ROS topic."""
        spec = self.get().get("ros", {}).get("topics", {}).get(name, "healthy")
        if isinstance(spec, str):
            spec = {"state": spec}
        hz = spec.get("hz")
        return {"state": spec.get("state", "healthy"), "hz": float(hz) if hz is not None else None}

    def node_present(self, name: str) -> bool:
        return bool(self.get().get("ros", {}).get("nodes", {}).get(name, True))
