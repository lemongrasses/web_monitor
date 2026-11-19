"""The vehicle output layer: everything the dashboard tells the vehicle, in one place.

The core (state/health.py) builds a readiness snapshot by name every health step and hands it to
its listeners. This layer is one of them:

* it adds the wire numbers (vehicle/protocol.py) and the packet's session, sequence and uptime,
  so /api/readiness shows exactly what the vehicle gets,
* sends the 0x05 status packet (vehicle/sender.py),
* and gives the user pages their "Vehicle link" start-up step.

Nothing in the core imports this package: without it the dashboard monitors, shows its pages
and starts AIO NAV as before, and the vehicle gets no 0x05.
"""

import secrets
import time
from typing import Callable, Dict, Optional

from ..state.health import HEALTHY, IDLE, WARNING
from . import protocol
from .sender import StatusUdpPublisher


class VehicleOutput:
    def __init__(self, cfg, store, clock: Callable[[], float] = time.monotonic,
                 relay: Optional[Callable[[bytes], None]] = None):
        self.cfg, self._clock = cfg, clock
        self.session_id = secrets.randbits(32) or 1     # new at every dashboard start
        self.started = clock()
        self._sequence = 0
        self.sender = StatusUdpPublisher(cfg, store, clock=clock)
        self.sender.relay = relay

    # --------------------------------------------------- health step listener
    def on_snapshot(self, snap: Dict, now: float) -> None:
        """Called by the health step with each readiness snapshot, before it is stored."""
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        snap.update(protocol.wire_fields(snap))
        snap.update({"session_id": self.session_id, "sequence": self._sequence,
                     "uptime_ms": int((now - self.started) * 1000)})
        self.sender.publish(snap, now)

    def status(self) -> Dict:
        return self.sender.status()

    def close(self) -> None:
        self.sender.close()

    # ------------------------------------------------------------ user pages
    def startup_step(self, fresh: bool) -> Dict:
        """The "Vehicle link" step of the user pages: what the vehicle is sent (UDP: sent, never
        confirmed received)."""
        tx = self.status()
        externals = [d for d in self.cfg.nav_destinations() if d.get("kind") == "external"]
        dest = f'{externals[0]["host"]}:{externals[0]["port"]}' if externals else None
        rows = [{"k": "Vehicle address", "v": dest or "Not set"},
                {"k": "Navigation data (0x04)", "v": ("Sending" if fresh else "Not sending") if dest else "Not set"},
                {"k": "Ready signal (0x05)", "v": ("Sending" if tx.get("sent") and not tx.get("destination_error") else
                                                   "Problem" if tx.get("destination_error") else "Starting")
                                                  if tx.get("enabled") else "Off"}]
        if not dest:
            level, label, hint = WARNING, "No vehicle address", "Set the vehicle address in Maintenance, AIO NAV config."
        elif not tx.get("enabled"):
            level, label, hint = IDLE, "Ready signal off", "The vehicle gets navigation data but no Ready signal."
        elif tx.get("destination_error") or tx.get("errors"):
            level, label, hint = WARNING, "Problem", tx.get("destination_error") or tx.get("last_error") or ""
        else:
            level, label, hint = HEALTHY, "Sending", "Sent over UDP: the vehicle does not reply, so delivery is not confirmed."
        return {"key": "vehicle", "name": "Vehicle link", "level": level, "label": label,
                "rows": rows, "hint": hint}
