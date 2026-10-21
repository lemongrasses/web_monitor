"""Settings both UIs offer: Wi-Fi and "RTK required for Ready".

The same endpoints are mounted on the user app (no password, /api/...) and on the Maintenance
app (password, /api/maint/...). Both use the one WifiManager on ctx, so its lock serialises a
change made from either page. Wi-Fi may only be changed from a browser whose path to this device
is a cable (or the device itself), so a change can never cut off the page that made it.
Turning RTK off needs an explicit confirm, from either page. The hotspot (an installer option) is
switched on Maintenance only; the user page shows it and does not change Wi-Fi while it runs.
"""

from flask import jsonify, request

from ..actions import wifi as wifi_mod
from ..state.health import rtk_setting, set_rtk_setting


class _NoEvents:
    def add(self, *a, **k):
        pass


def _events(ctx):
    return getattr(ctx, "events", None) or _NoEvents()


def wifi_manager(ctx):
    """The one WifiManager of the process (both apps share it, and its lock)."""
    if getattr(ctx, "wifi", None) is None:
        ctx.wifi = wifi_mod.WifiManager(ctx.cfg, _events(ctx))
    return ctx.wifi


def register_settings(ctx, app, prefix: str, rtk_path: str, guard=None, where: str = "user page") -> None:
    wifi = wifi_manager(ctx)
    protect = guard.required if guard is not None else (lambda f: f)

    def bad_request():
        return request.headers.get("X-Requested-With") != "aio-dashboard" or not request.is_json

    def body():
        return request.get_json(silent=True) or {}

    def wifi_gate(dev):
        """(may this browser change Wi-Fi, why), and what a change could disturb right now."""
        ok, why = wifi_mod.client_path(request.remote_addr or "", dev)
        risks = []
        state = (ctx.store.get("readiness", {}) or {}).get("state")
        if state in ("READY", "DEGRADED"):
            risks.append(f"navigation is in use ({state})")
        net = ctx.store.get("network", {}) or {}
        for dest, r in (net.get("nav_routes") or {}).items():
            if dev and (r or {}).get("interface") == dev:
                risks.append(f"the vehicle output {dest} goes over Wi-Fi")
        if dev and (net.get("default_route") or {}).get("interface") == dev:
            risks.append("the internet route (RTK corrections) goes over Wi-Fi")
        return ok, why, risks

    def api_wifi():
        st = wifi.status()
        ok, why, risks = wifi_gate(st.get("device")) if st.get("available") else (False, st.get("reason", ""), [])
        relay = getattr(ctx, "hotspot_relay", None)
        if st.get("hotspot") is not None and relay is not None:
            st["hotspot"]["relay"] = relay.status()
        return jsonify(dict(st, may_change=ok, client=why, risks=risks))

    def api_wifi_scan():
        if bad_request():
            return jsonify({"success": False, "summary": "bad request"}), 400
        try:
            rescan = body().get("rescan", True)
            return jsonify({"success": True, "networks": wifi.scan("auto" if rescan == "auto" else bool(rescan))})
        except wifi_mod.WifiError as e:
            return jsonify({"success": False, "summary": str(e)}), e.status

    @protect
    def api_wifi_connect():
        if bad_request():
            return jsonify({"success": False, "summary": "bad request"}), 400
        b = body()
        st = wifi.status()
        if not st.get("available"):
            return jsonify({"success": False, "summary": st.get("reason", "no Wi-Fi")}), 404
        ok, why, risks = wifi_gate(st["device"])
        if not ok:
            return jsonify({"success": False, "summary": why}), 403
        if risks and b.get("confirm") is not True:
            return jsonify({"success": False, "needs_confirm": True, "risks": risks,
                            "summary": "Changing Wi-Fi now may interrupt: " + "; ".join(risks)}), 409
        ssid, password = b.get("ssid"), b.get("password") or ""
        if not isinstance(ssid, str) or not isinstance(password, str):
            return jsonify({"success": False, "summary": "ssid and password must be text"}), 400
        _events(ctx).add("info", "network", f"Wi-Fi change to {ssid} requested",
                       f"on the {where} from {request.remote_addr} ({why})")
        try:
            return jsonify(wifi.connect(ssid, password, bool(b.get("hidden"))))
        except wifi_mod.WifiError as e:
            return jsonify({"success": False, "summary": str(e)}), e.status

    @protect
    def api_wifi_hotspot():
        """Start or stop the hotspot (Maintenance only): {"action": "start", "ssid", "password"} or
        {"action": "stop"}; same cable rule as a Wi-Fi change, and a confirm when it disturbs."""
        if bad_request():
            return jsonify({"success": False, "summary": "bad request"}), 400
        if not wifi.hotspot_offered:
            return jsonify({"success": False, "summary": "the hotspot is not installed on this device"}), 404
        b = body()
        action = b.get("action")
        if action not in ("start", "stop"):
            return jsonify({"success": False, "summary": "action must be start or stop"}), 400
        st = wifi.status()
        if not st.get("available"):
            return jsonify({"success": False, "summary": st.get("reason", "no Wi-Fi")}), 404
        ok, why, risks = wifi_gate(st["device"])
        if not ok:
            return jsonify({"success": False, "summary": why}), 403
        hs = st.get("hotspot") or {}
        if action == "start" and st.get("connection"):
            risks.append(f"the Wi-Fi leaves {st['connection']}")
        if action == "stop" and hs.get("clients"):
            risks.append(f"{hs['clients']} device(s) on the hotspot lose the connection")
        if risks and b.get("confirm") is not True:
            return jsonify({"success": False, "needs_confirm": True, "risks": risks,
                            "summary": "Changing Wi-Fi now may interrupt: " + "; ".join(risks)}), 409
        _events(ctx).add("info", "network", f"Wi-Fi hotspot {action} requested",
                         f"on the {where} from {request.remote_addr} ({why})")
        try:
            if action == "stop":
                return jsonify(wifi.stop_hotspot())
            ssid, password = b.get("ssid"), b.get("password")
            if not isinstance(ssid, str) or not isinstance(password, str):
                return jsonify({"success": False, "summary": "ssid and password must be text"}), 400
            return jsonify(wifi.start_hotspot(ssid, password))
        except wifi_mod.WifiError as e:
            return jsonify({"success": False, "summary": str(e)}), e.status

    @protect
    def api_wifi_hotspot_relay():
        """Copies of the NAV and 0x05 packets for the devices on the hotspot: {"enabled": bool}.
        Changes no Wi-Fi setting, so no cable rule."""
        if bad_request():
            return jsonify({"success": False, "summary": "bad request"}), 400
        relay = getattr(ctx, "hotspot_relay", None)
        if relay is None or not wifi.hotspot_offered:
            return jsonify({"success": False, "summary": "the hotspot is not installed on this device"}), 404
        enabled = body().get("enabled")
        if not isinstance(enabled, bool):
            return jsonify({"success": False, "summary": "enabled must be true or false"}), 400
        relay.set_enabled(enabled, f"{where} {request.remote_addr or ''}".strip())
        return jsonify({"success": True, "relay": relay.status(),
                        "summary": (f"devices on the hotspot get the NAV and status packets on UDP {relay.port}"
                                    if enabled else "no packets to the devices on the hotspot")})

    @protect
    def api_rtk():
        """Require RTK for READY, or not. A safety setting: confirmed, logged, kept on disk."""
        if bad_request():
            return jsonify({"success": False, "summary": "bad request"}), 400
        b = body()
        required = b.get("required")
        if not isinstance(required, bool):
            return jsonify({"success": False, "summary": "required must be true or false"}), 400
        if not required and b.get("confirm") is not True:
            return jsonify({"success": False, "needs_confirm": True,
                            "summary": "Without RTK, Ready may be reported with a position that is only "
                                       "accurate to a few metres."}), 409
        try:
            set_rtk_setting(ctx.cfg, required, f"{where} {request.remote_addr or ''}".strip())
        except OSError as e:
            return jsonify({"success": False, "summary": f"cannot save: {e}"}), 500
        _events(ctx).add("warning", "readiness", "RTK " + ("required" if required else "no longer required") + " for Ready",
                       f"changed on the {where} from {request.remote_addr}")
        return jsonify({"success": True, "required": required,
                        "summary": "RTK is required for Ready" if required else "Ready no longer waits for RTK"})

    name = prefix.strip("/").replace("/", "_")
    app.add_url_rule(f"{prefix}/wifi", f"{name}_wifi", api_wifi)
    app.add_url_rule(f"{prefix}/wifi/scan", f"{name}_wifi_scan", api_wifi_scan, methods=["POST"])
    app.add_url_rule(f"{prefix}/wifi/connect", f"{name}_wifi_connect", api_wifi_connect, methods=["POST"])
    if guard is not None:                             # the hotspot is switched on Maintenance only
        app.add_url_rule(f"{prefix}/wifi/hotspot", f"{name}_wifi_hotspot", api_wifi_hotspot, methods=["POST"])
        app.add_url_rule(f"{prefix}/wifi/hotspot/relay", f"{name}_wifi_hotspot_relay", api_wifi_hotspot_relay,
                         methods=["POST"])
    app.add_url_rule(rtk_path, f"{name}_rtk", api_rtk, methods=["POST"])


def rtk_view(cfg) -> dict:
    """What a settings page shows about the RTK requirement."""
    required, source, data = rtk_setting(cfg)
    return {"required": required, "source": source, "changed_at": data.get("changed_at"),
            "changed_by": data.get("changed_by"),
            "min_quality": cfg["readiness"].get("gnss_min_quality") or "float"}
