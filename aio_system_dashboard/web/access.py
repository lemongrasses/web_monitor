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
