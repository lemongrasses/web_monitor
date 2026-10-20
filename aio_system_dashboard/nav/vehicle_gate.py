"""Reference receiver for the vehicle side: when may the 0x04 navigation data be used?

The vehicle's own software decides; this is the reference the vehicle team can test against
(tools/vehicle_gate.py runs it on live UDP). Rules:

1. Blocked by default. A valid 0x05 status packet must have arrived within status_timeout_s;
   a bad checksum, an unknown version or a new session_id clears the authorisation at once.
2. The status must say READY with inhibit_flags == 0 continuously for status_stable_s, in the
   same session.
3. The newest 0x04 must be at most nav_max_age_s old, carry the alignment, heading and fine
   alignment flags, have a time stamp that moves forward, and only finite values.
4. Sequence numbers are used to count lost and reordered status packets; one lost UDP packet
   alone never blocks, only the time-outs do. A packet more than REORDER_WINDOW behind the newest
   one in the same session means two senders (or a forged packet that jumped ahead): the
   authorisation is cleared and the newest packet starts again. Without this, one forged packet
   far ahead made the real ones look old: forged READY was trusted, the real stream was ignored.

No authentication: anything that can send to the vehicle's port can still send a READY packet.
Keep the link private (direct cable or a closed VLAN) and pin the sender's address (allowed_from).

Pure: feed packets with their arrival time, ask allowed(now).
"""

import math
from typing import Dict, Optional, Tuple

from . import status_packet
from .decoder import NAV_MSG_ID_SOLUTION, parse_nav_packet

READY_CODE = 6
REORDER_WINDOW = 5         # status packets (2.5 s at 2 Hz) that may arrive late and be ignored


class VehicleGate:
    def __init__(self, status_timeout_s: float = 3.0, status_stable_s: float = 2.0,
                 nav_max_age_s: float = 0.5, status_message_id: int = status_packet.DEFAULT_MESSAGE_ID,
                 allowed_from: Optional[str] = None):
        self.status_timeout_s, self.status_stable_s = status_timeout_s, status_stable_s
        self.nav_max_age_s, self.status_id = nav_max_age_s, status_message_id
        self.status: Optional[Dict] = None
        self.status_at: Optional[float] = None
        self.ready_since: Optional[float] = None
        self.session: Optional[int] = None
        self.nav: Optional[Dict] = None
        self.nav_at: Optional[float] = None
        self.nav_time_backwards = False
        self.allowed_from = allowed_from         # sender IP to accept (None: any)
        self.lost = self.reordered = self.bad = self.conflicts = self.foreign = 0

    def feed(self, data: bytes, now: float, sender: Optional[str] = None) -> Optional[str]:
        """Take one datagram. Returns "status", "nav" or None (not a valid packet)."""
        if self.allowed_from and sender is not None and sender != self.allowed_from:
            self.foreign += 1
            return None
        if len(data) >= 3 and data[:2] == status_packet.SYNC and data[2] == self.status_id:
            s = status_packet.decode(data, self.status_id)
            if s is None:
                self.bad += 1
                self._clear()                 # a broken or unknown status packet: stop trusting
                return None
            if self.session is not None and s["session_id"] != self.session:
                self._clear()                 # the dashboard restarted: start over
            elif self.status is not None:
                gap = (s["sequence"] - self.status["sequence"]) & 0xFFFFFFFF
                if gap == 0 or gap > 0x7FFFFFFF:
                    if (self.status["sequence"] - s["sequence"]) & 0xFFFFFFFF <= REORDER_WINDOW:
                        self.reordered += 1
                        return "status"       # late or duplicate: ignore its content
                    self.conflicts += 1       # far behind: two senders in one session
                    self._clear()
                else:
                    self.lost += gap - 1
            self.session, self.status, self.status_at = s["session_id"], s, now
            if s["state"] == READY_CODE and s["inhibit_flags"] == 0:
                if self.ready_since is None:
                    self.ready_since = now
            else:
                self.ready_since = None
            return "status"
        if len(data) >= 3 and data[:2] == b"\x55\xAA" and data[2] == NAV_MSG_ID_SOLUTION:
            n = parse_nav_packet(data)
            if n is None:
                self.bad += 1
                return None
            self.nav_time_backwards = self.nav is not None and n["time_s"] <= self.nav["time_s"]
            self.nav, self.nav_at = n, now
            return "nav"
        self.bad += 1
        return None

    def _clear(self) -> None:
        self.status = self.status_at = self.ready_since = self.session = None

    def allowed(self, now: float) -> Tuple[bool, str]:
        if self.status is None or now - self.status_at > self.status_timeout_s:
            return False, "no current status packet"
        if self.ready_since is None:
            return False, f"status not READY (state {self.status['state']}, inhibit 0x{self.status['inhibit_flags']:x})"
        if now - self.ready_since < self.status_stable_s:
            return False, "READY not yet stable"
        if self.nav is None or now - self.nav_at > self.nav_max_age_s:
            return False, "navigation data too old"
        if self.nav_time_backwards:
            return False, "navigation time did not move forward"
        n = self.nav
        if not (n["alignment"] and n["heading_valid"] and n["fine_alignment"]):
            return False, "navigation flags not aligned"
        values = [v for k, v in n.items() if isinstance(v, float)]
        if not all(math.isfinite(v) for v in values):
            return False, "navigation data not finite"
        return True, "ok"
