"""Sends the readiness snapshot to the vehicle as the 0x05 status packet (nav/status_packet.py).

Called from the health step (about 5 Hz): a packet goes out every 1/rate_hz seconds and at once
when the state, requested action or inhibit mask changes, so a transition never waits for the
next tick. It keeps sending after READY: silence must mean "do not use".

Destination: status_udp.destination, or "auto" = output_udp of the active aio_nav.yaml, the
same address and port where the vehicle already reads 0x04. Any number of senders may send to
one UDP port, so the vehicle keeps one socket and one 55 AA frame parser and tells the packets
apart by the message ID byte (0x04: 156 bytes, 0x05: 42 bytes). An explicit "IPv4:port" sends
it elsewhere. The dashboard's own NAV listener (nav.udp_bind) is refused as a destination: it
would count every status packet as a bad NAV packet. A failed send is counted and shown, never
raised.
"""

import ipaddress
import logging
import socket
import time
from typing import Dict, Optional, Tuple

from ..config import parse_host_port
from ..nav import status_packet

logger = logging.getLogger(__name__)


class StatusUdpPublisher:
    def __init__(self, cfg, store, clock=time.monotonic):
        self.cfg, self.store, self._clock = cfg, store, clock
        s = cfg["status_udp"]
        self.enabled = bool(s["enabled"])
        self.message_id = int(s.get("message_id", status_packet.DEFAULT_MESSAGE_ID))
        self.period = 1.0 / max(float(s.get("rate_hz", 2.0)), 0.1)
        self.sequence = 0
        self.sent = 0
        self.errors = 0
        self.last_error = ""
        self._last_sent_at: Optional[float] = None
        self._last_key = None
        self._sock: Optional[socket.socket] = None
        self._published_at = 0.0
        self.relay = None                 # copies for the devices on the hotspot (hotspot_relay.py)

    # ------------------------------------------------------------ destination
    def destination(self) -> Tuple[Optional[Tuple[str, int]], str]:
        """((host, port), "") or (None, why not)."""
        want = str(self.cfg["status_udp"].get("destination") or "auto")
        if want == "auto":
            nav = parse_host_port(self.cfg.aio_nav.get("output_udp") or "")
            if nav is None:
                return None, "no destination: output_udp in aio_nav.yaml is empty"
            want = f"{nav[0]}:{nav[1]}"                # 0x05 goes where 0x04 goes
        addr = parse_host_port(want)
        if addr is None:
            return None, f"invalid destination {want!r} (host:port)"
        try:
            ipaddress.IPv4Address(addr[0])
        except ValueError:
            # A host name would make every send a DNS lookup on the health thread.
            return None, f"destination {want!r}: use an IPv4 address, not a host name"
        bind = parse_host_port(self.cfg["nav"]["udp_bind"])
        if bind and addr[1] == bind[1] and _is_local(addr[0]) and (_is_local(bind[0]) or bind[0] == "0.0.0.0"):
            return None, f"{want} is the dashboard's own NAV listener, not the vehicle"
        return addr, ""

    # ---------------------------------------------------------------- publish
    def publish(self, snap: Dict, now: Optional[float] = None) -> None:
        if not self.enabled:
            return
        now = self._clock() if now is None else now
        key = (snap.get("state"), snap.get("requested_action"), snap.get("inhibit_flags"), snap.get("session_id"))
        due = self._last_sent_at is None or now - self._last_sent_at >= self.period - 1e-6
        if not due and key == self._last_key:
            return
        addr, why = self.destination()
        if addr is None:
            self._note_error(why)
        else:
            self.sequence = (self.sequence + 1) & 0xFFFFFFFF
            pkt = status_packet.encode(status_packet.from_snapshot(snap, self.sequence), self.message_id)
            try:
                if self._sock is None:
                    self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    self._sock.setblocking(False)
                self._sock.sendto(pkt, addr)
                self.sent += 1
                self.last_error = ""
            except OSError as e:                         # no route, host unreachable, buffer full
                self._note_error(f"send to {addr[0]}:{addr[1]} failed: {e.strerror or e}")
            if self.relay is not None:
                self.relay(pkt)
        self._last_sent_at, self._last_key = now, key
        if now - self._published_at >= 1.0 or self.last_error:
            self._published_at = now
            self.store.set("status_udp", self.status())

    def _note_error(self, text: str) -> None:
        self.errors += 1
        if text != self.last_error:
            logger.warning("status packet: %s", text)
        self.last_error = text

    def status(self) -> Dict:
        addr, why = self.destination() if self.enabled else (None, "disabled")
        return {"enabled": self.enabled, "message_id": self.message_id,
                "destination": f"{addr[0]}:{addr[1]}" if addr else None, "destination_error": why,
                "sequence": self.sequence, "sent": self.sent, "errors": self.errors,
                "last_error": self.last_error, "rate_hz": round(1.0 / self.period, 2)}

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None


def _is_local(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
