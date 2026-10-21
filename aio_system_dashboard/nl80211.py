"""Minimal nl80211: which Wi-Fi interface is an access point, and who is on it, without `iw`.

The same kernel requests `iw dev` and `iw dev X station dump` make (generic netlink, read-only, no
root needed), so the hotspot's device count and UDP copies work on a system without the iw package.
"""

import os
import socket
import struct
from typing import Dict, List, Optional, Set, Tuple

NETLINK_GENERIC = 16
NLM_F_REQUEST, NLM_F_DUMP = 0x1, 0x300
NLMSG_ERROR, NLMSG_DONE = 2, 3
GENL_ID_CTRL, CTRL_CMD_GETFAMILY = 0x10, 3
CTRL_ATTR_FAMILY_ID, CTRL_ATTR_FAMILY_NAME = 1, 2
CMD_GET_INTERFACE, CMD_GET_STATION = 5, 17
ATTR_IFINDEX, ATTR_IFNAME, ATTR_IFTYPE, ATTR_MAC = 3, 4, 5, 6
IFTYPE_AP = 3

_HDR = struct.Struct("=IHHII")                  # nlmsghdr: length, type, flags, sequence, port
_GENL = struct.Struct("=BBH")                   # genlmsghdr: command, version, reserved
_ATTR = struct.Struct("=HH")                    # nlattr: length, type


def attr(kind: int, data: bytes) -> bytes:
    size = _ATTR.size + len(data)
    return _ATTR.pack(size, kind) + data + b"\0" * (-size % 4)


def attrs(buf: bytes) -> Dict[int, bytes]:
    out, off = {}, 0
    while off + _ATTR.size <= len(buf):
        size, kind = _ATTR.unpack_from(buf, off)
        if size < _ATTR.size:
            break
        out[kind & 0x3FFF] = buf[off + _ATTR.size:off + size]      # without the nested / byte-order bits
        off += (size + 3) & ~3
    return out


def messages(data: bytes) -> List[Tuple[int, int, bytes]]:
    """(type, sequence, payload) of each netlink message in one datagram."""
    out, off = [], 0
    while off + _HDR.size <= len(data):
        size, kind, _, seq, _ = _HDR.unpack_from(data, off)
        if size < _HDR.size:
            raise OSError("malformed netlink message")
        out.append((kind, seq, data[off + _HDR.size:off + size]))
        off += (size + 3) & ~3
    return out


class Nl80211:
    """One generic netlink socket; a context manager. OSError when the kernel has no nl80211."""

    def __init__(self, timeout: float = 2.0, sock=None):
        self._sock = sock or socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_GENERIC)
        self._seq = 0
        try:
            if sock is None:
                self._sock.settimeout(timeout)
                self._sock.bind((0, 0))
            reply = self._request(GENL_ID_CTRL, CTRL_CMD_GETFAMILY, attr(CTRL_ATTR_FAMILY_NAME, b"nl80211\0"))
            family = reply[0].get(CTRL_ATTR_FAMILY_ID) if reply else None
            if family is None:
                raise OSError("no nl80211 in this kernel")
            self.family = struct.unpack("=H", family[:2])[0]
        except BaseException:
            self._sock.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._sock.close()

    def _request(self, family: int, cmd: int, payload: bytes = b"", dump: bool = False) -> List[Dict[int, bytes]]:
        self._seq += 1
        body = _GENL.pack(cmd, 1, 0) + payload
        flags = NLM_F_REQUEST | (NLM_F_DUMP if dump else 0)
        self._sock.send(_HDR.pack(_HDR.size + len(body), family, flags, self._seq, 0) + body)
        replies = []
        while True:
            for kind, seq, data in messages(self._sock.recv(65536)):
                if seq != self._seq:
                    continue
                if kind == NLMSG_DONE:
                    return replies
                if kind == NLMSG_ERROR:
                    err = struct.unpack_from("=i", data)[0]
                    if err:
                        raise OSError(-err, os.strerror(-err))
                    return replies
                replies.append(attrs(data[_GENL.size:]))
                if not dump:
                    return replies

    def interfaces(self) -> List[Dict]:
        """{"name", "ifindex", "type"} of each Wi-Fi interface (P2P devices have no name: left out)."""
        out = []
        for a in self._request(self.family, CMD_GET_INTERFACE, dump=True):
            if ATTR_IFNAME in a and ATTR_IFINDEX in a:
                out.append({"name": a[ATTR_IFNAME].rstrip(b"\0").decode(errors="replace"),
                            "ifindex": struct.unpack("=I", a[ATTR_IFINDEX][:4])[0],
                            "type": struct.unpack("=I", a[ATTR_IFTYPE][:4])[0] if ATTR_IFTYPE in a else None})
        return out

    def stations(self, ifindex: int) -> Set[str]:
        """The MACs (lower case) of the devices associated with the interface."""
        replies = self._request(self.family, CMD_GET_STATION, attr(ATTR_IFINDEX, struct.pack("=I", ifindex)), dump=True)
        return {a[ATTR_MAC][:6].hex(":") for a in replies if len(a.get(ATTR_MAC, b"")) >= 6}


def access_point() -> Optional[Tuple[str, Set[str]]]:
    """(interface, MACs of the devices on it) of the Wi-Fi interface in access-point mode, or None
    when none is. Raises OSError when nl80211 cannot be read."""
    with Nl80211() as nl:
        for i in nl.interfaces():
            if i["type"] == IFTYPE_AP:
                return i["name"], nl.stations(i["ifindex"])
    return None


def stations(ifname: str) -> Set[str]:
    """The MACs of the devices associated with this interface. Raises OSError."""
    with Nl80211() as nl:
        return nl.stations(socket.if_nametoindex(ifname))
