/* Product UI Alpine components. UI refresh (~5 Hz) is decoupled from the raw NAV rate. */
(function () {
  "use strict";
  const fmt = AIO.fmt;
  const STATE_LABEL = { READY: "Ready", INITIALIZING: "Initializing", STARTING: "Starting",
                        STOPPED: "Stopped", FAULT: "Fault", UNKNOWN: "Unknown" };

  window.productPage = function () {
    let navMap = null;            // Leaflet objects stay outside Alpine's reactive proxy
    const headingRot = AIO.rotator();
    let lastOk = 0;

    return {
      s: null,
      connected: true,
      hdgRot: 0,
      follow: true,
      ctlBusy: false,
      ctlConfirm: null,   // "stop" | "restart" while waiting for the second click
      ctlResult: null,
      fmt,

      init() {
        const el = document.querySelector("[data-navmap]");
        if (el) {
          navMap = new NavMap(el.id, { full: el.dataset.navmap === "full" });
          navMap.startTrajectory(2000);
          el.addEventListener("follow-changed", (e) => { this.follow = e.detail; });
        }
        AIO.poll("/api/state", 200, (d) => {
          lastOk = Date.now();
          this.connected = true;
          this.s = d;
          const sol = d.solution;
          if (sol) {
            const r = headingRot(sol.heading);
            if (r !== null) this.hdgRot = r;
            if (navMap) navMap.setPosition(sol.latitude, sol.longitude, sol.heading, !d.nav.fresh);
          } else if (navMap) {
            navMap.clearPosition();                 // AIO NAV stopped: nothing from the old run stays
          }
        }, () => {
          if (Date.now() - lastOk > 3000) this.connected = false;
        });
      },

      // ----- derived values
      get state() { return this.connected && this.s && this.s.health ? this.s.health.state : "UNKNOWN"; },
      get stateLabel() {
        if (!this.connected) return "Offline";
        return STATE_LABEL[this.state] || "Unknown";
      },
      // Two lines: what is happening, and what to do about it.
      get stateTitle() {
        if (!this.connected) return "Lost connection to the dashboard";
        const h = this.s && this.s.health;
        return (h && h.title) || (this.s ? "" : "Connecting…");
      },
      get stateDetail() {
        if (!this.connected) return "Values on this page are not live. Check the network connection to the device.";
        const h = this.s && this.s.health;
        return (h && h.detail) || "";
      },
      get fresh() { return !!(this.s && this.s.nav && this.s.nav.fresh); },
      v(key, digits) {
        const sol = this.s && this.s.solution;
        return sol ? fmt.num(sol[key], digits) : "—";
      },
      latlon(key) {
        const sol = this.s && this.s.solution;
        return sol ? fmt.deg(sol[key], 8) : "—";
      },
      hasHeading() { const sol = this.s && this.s.solution; return !!sol && AIO.isNum(sol.heading); },
      heading360() {
        const sol = this.s && this.s.solution;
        if (!sol || !AIO.isNum(sol.heading)) return "—";
        return (((sol.heading % 360) + 360) % 360).toFixed(1);
      },
      ind(key) {
        const i = this.s && this.s.indicators && this.s.indicators[key];
        return this.connected && i ? i : { level: "unknown", label: "Unknown" };
      },
      get ctl() {
        const c = this.connected && this.s && this.s.control;
        return c || { enabled: false, state: "unknown", installed: true, label: "AIO NAV" };
      },
      get ctlMsg() {
        if (this.ctlBusy) return "";
        if (this.ctlResult) return this.ctlResult.summary || "";
        return "";
      },
      get ctlMsgClass() {
        return this.ctlResult && !this.ctlBusy ? (this.ctlResult.success ? "ok" : "bad") : "";
      },
      askCtl(verb) {
        this.ctlResult = null;
        this.ctlConfirm = verb;
        setTimeout(() => { const b = document.getElementById("ctl-cancel"); if (b) b.focus(); }, 50);  // safe default; x-if renders async
        clearTimeout(this._ctlTimer);
        this._ctlTimer = setTimeout(() => { this.ctlConfirm = null; }, 8000);  // confirmation lapses
      },
      async runCtl(verb) {
        clearTimeout(this._ctlTimer);
        this.ctlConfirm = null;
        this.ctlBusy = true;
        this.ctlResult = null;
        try {
          const r = await fetch("/api/nav/control", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-Requested-With": "aio-dashboard" },
            body: JSON.stringify({ action: verb }),
          });
          this.ctlResult = await r.json();
        } catch (e) {
          this.ctlResult = { success: false, summary: "request failed: " + e.message };
        } finally {
          this.ctlBusy = false;
          setTimeout(() => { if (this.ctlResult && this.ctlResult.success) this.ctlResult = null; }, 6000);
        }
      },
      aid(key) {
        const on = this.connected && this.s && this.s.aiding && this.s.aiding[key];
        return on ? { level: "active", label: "Active" } : { level: "idle", label: "Idle" };
      },
      get udp() {
        return this.connected && this.s && this.s.udp ? this.s.udp
          : { level: "unknown", label: "Unknown", destinations: [] };
      },
      udpLevel() { return this.udp.level; },
      externalDest() {
        const d = (this.udp.destinations || []).find((x) => x.kind === "external");
        return d ? d.host + ":" + d.port : null;
      },
      localDest() {
        const d = (this.udp.destinations || []).find((x) => x.kind === "local");
        return d ? d.host + ":" + d.port : "127.0.0.1:9000";
      },
      outputText() {
        const u = this.udp, dest = this.externalDest();
        if (!this.connected) return "—";
        if (u.level === "idle") return "Nothing is sent while AIO NAV is stopped.";
        if (u.label === "Waiting") return "Nothing sent yet while AIO NAV starts.";
        const rate = AIO.isNum(u.rate_hz) ? fmt.hz(u.rate_hz) : "—";
        const to = dest ? " to " + dest : " (this device only)";
        if (u.label === "Lost") return AIO.isNum(u.age_s) ? "No output for " + Math.round(u.age_s) + " s." : "No output received.";
        return rate + to + (u.label === "Low rate" && AIO.isNum(u.expected_rate_hz) ? " (expected " + fmt.hz(u.expected_rate_hz) + ")" : "");
      },
      rateText() {
        const u = this.udp;
        if (!AIO.isNum(u.rate_hz)) return "—";
        return fmt.hz(u.rate_hz) + (AIO.isNum(u.expected_rate_hz) ? " / " + fmt.hz(u.expected_rate_hz) : "");
      },
      storageText() {
        const st = this.s && this.s.storage;
        return st ? fmt.bytes(st.free) + " free of " + fmt.bytes(st.total) : "—";
      },
      setFollow(on) { this.follow = on; if (navMap) navMap.setFollow(on); },
      fitAll() { if (navMap) navMap.fitTrajectory(); },
    };
  };

  window.dataPage = function () {
    return {
      roots: [], root: null, listing: null, error: null, loading: false, fmt,
      async init() {
        try {
          this.roots = await (await fetch("/api/data/roots")).json();
        } catch (e) { this.error = "Cannot load data locations"; return; }
        const params = new URLSearchParams(location.search);
        const rid = params.get("root") || (this.roots.find((r) => r.exists) || this.roots[0] || {}).id;
        if (rid) this.open(rid, params.get("path") || "");
      },
      async open(rootId, path) {
        this.loading = true; this.error = null; this.root = rootId;
        try {
          const r = await fetch("/api/data/list?root=" + encodeURIComponent(rootId) + "&path=" + encodeURIComponent(path || ""));
          const d = await r.json();
          if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
          this.listing = d;
          history.replaceState(null, "", "?root=" + encodeURIComponent(rootId) + (d.path ? "&path=" + encodeURIComponent(d.path) : ""));
        } catch (e) {
          this.listing = null; this.error = e.message;
        } finally { this.loading = false; }
      },
      downloadUrl(entry) {
        return "/data/download/" + encodeURIComponent(this.root) + "/" + entry.path.split("/").map(encodeURIComponent).join("/");
      },
    };
  };
})();
