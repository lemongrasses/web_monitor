"""Client allow-list for the web apps (no Flask dependency)."""

import ipaddress
import logging

logger = logging.getLogger(__name__)


def parse_allowed_clients(values):
    """Turn config entries (IP or CIDR) into networks; bad entries are logged and skipped."""
    nets = []
    for v in values or []:
        try:
            nets.append(ipaddress.ip_network(str(v).strip(), strict=False))
        except ValueError:
            logger.error("access.allowed_clients: ignoring invalid entry %r", v)
    return nets


def client_allowed(addr, nets) -> bool:
    if not nets:
        return True
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    return any(ip in n for n in nets)


def hotspot_clients(cfg) -> list:
    """The hotspot's own network, when the hotspot is installed and its phones may open the pages
    (wifi.hotspot.allow_clients): a configured access list would otherwise shut them out."""
    from ..actions.wifi import hotspot_enabled
    try:
        h = (cfg["wifi"] or {}).get("hotspot") or {}
    except KeyError:
        return []
    if h.get("allow_clients", True) is False or not hotspot_enabled(cfg):
        return []
    try:
        return [ipaddress.ip_interface(str(h.get("address") or "10.42.0.1/24")).network]
    except ValueError:
        logger.error("wifi.hotspot.address: not an address with a prefix (like 10.42.0.1/24): %r", h.get("address"))
        return []
