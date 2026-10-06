"""Simple device diagnostics: reachability + driver service + ROS topics (spec §29–32)."""

from typing import Dict


def run_device_diagnostic(cfg, store, network_probe, device: str) -> Dict:
    dev = cfg["devices"][device]
    checks = []

    if dev.get("host"):  # optional: without an IP there is nothing to ping
        reach = network_probe(device)
        checks.append({"name": "Device reachable", "ok": bool(reach.get("reachable")),
                       "detail": f"{reach.get('host')}"
                                 + (f" rtt {reach['rtt_ms']:.1f} ms" if reach.get("rtt_ms") else "")
                                 + (f" ({reach['error']})" if reach.get("error") else "")})

    svc_key = dev.get("service")
    if svc_key:
        svc = (store.get("services", {}) or {}).get(svc_key, {})
        state = svc.get("state", "unknown")
        if state != "not_installed":  # optional: the driver may not run as a systemd unit
            checks.append({"name": "Driver service running", "ok": state == "running",
                           "detail": f"{svc.get('unit', svc_key)}: {state}"})

    ros = store.get("ros", {}) or {}
    group = dev.get("topic_group", device)
    topics = [t for t in ros.get("watched", []) if t.get("group") == group]
    if not ros.get("available"):
        checks.append({"name": "ROS topics", "ok": False,
                       "detail": ros.get("error") or "ROS monitor unavailable"})
    for t in topics:
        checks.append({"name": f"Topic {t['name']}", "ok": t["state"] == "healthy",
                       "detail": f"{t['state'].replace('_', ' ')}, {t['rate_hz']:.1f} Hz"})

    failed = [c["name"] for c in checks if not c["ok"]]
    return {
        "success": not failed,
        "checks": checks,
        "summary": "all checks passed" if not failed else "failed: " + ", ".join(failed),
    }
