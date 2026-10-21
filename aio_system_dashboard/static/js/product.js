/* Product UI Alpine components. UI refresh (~5 Hz) is decoupled from the raw NAV rate. */
(function () {
  "use strict";
  const fmt = AIO.fmt, tr = AIO.tr, trf = AIO.trf;
  const SP = window.AIO_LANG && window.AIO_LANG !== "en" ? "" : " ";   // between two sentences
  const STATE_LABEL = { READY: "Ready", INITIALIZING: "Initializing", STARTING: "Starting",
                        STOPPED: "Stopped", FAULT: "Fault", DEGRADED: "Degraded", UNKNOWN: "Unknown" };
  // What the operator does now (readiness requested_action, the same as in the 0x05 packet):
  // an icon and a short verb in the status area. Maintenance hold is said by the state itself.
  const ACTION = { HOLD_STILL: ["still", "Keep still"], DRIVE_STRAIGHT: ["straight", "Drive straight"],
                   KEEP_DRIVING: ["drive", "Keep driving"], SAFE_STOP: ["stop", "Stop using navigation"],
                   SERVICE_REQUIRED: ["service", "Maintenance needed"] };
  const short = (sec) => {                  // 0:12, 4:05, 1:02:03
    if (!AIO.isNum(sec)) return "";
    sec = Math.floor(sec);
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), x = String(sec % 60).padStart(2, "0");
    return h ? h + ":" + String(m).padStart(2, "0") + ":" + x : m + ":" + x;
  };

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
      openStep: null,     // the start-up step whose details are open
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
        if (!this.connected) return tr("Offline");
        return tr(STATE_LABEL[this.state] || "Unknown");
      },
      // Two lines: what is happening, and what to do about it.
      get stateTitle() {
        if (!this.connected) return tr("Lost connection to the dashboard");
        const h = this.s && this.s.health;
        return (h && h.title) || (this.s ? "" : tr("Connecting…"));
      },
      get stateDetail() {
        if (!this.connected) return tr("Values on this page are not live. Check the network connection to the device.");
        const h = this.s && this.s.health;
        return (h && h.detail) || "";
      },
      get held() { return !!(this.s && this.s.readiness && this.s.readiness.hold); },
      action() { return this.connected && this.s && this.s.health ? ACTION[this.s.health.action] : null; },
      actionIcon() { const a = this.action(); return a ? a[0] : ""; },
      actionText() { const a = this.action(); return a ? tr(a[1]) : ""; },
      sinceText() {
        const h = this.connected && this.s && this.s.health;
        return h && AIO.isNum(h.since_s) ? trf("for {0}", short(h.since_s)) : "";
      },
      // The instrument: one tone per state, then three lines of text from big to small.
      tone() {
        if (!this.connected) return "idle";
        return { READY: "go", INITIALIZING: "work", STARTING: "info", DEGRADED: "stop", FAULT: "stop" }[this.state] || "idle";
      },
      headline() {
        if (this.actionText()) return this.actionText();
        if (this.state === "READY") return tr("Ready to use");
        return this.stateTitle;
      },
      whatLine() {
        if (this.actionText() || this.state === "READY") return this.stateTitle;
        return this.stateDetail;
      },
      whyLine() { return this.actionText() ? this.stateDetail : ""; },
      // Ring segment i = start-up step i: its level, or idle while there is none.
      arc(i) {
        const c = this.connected && this.s && this.s.checks && this.s.checks[i];
        return c ? c.level : "idle";
      },
      // The step to look at: the first one that is not done yet.
      currentStep() {
        if (this.state === "READY") return null;      // nothing to look at: no pulsing step
        const c = (this.s && this.s.checks) || [];
        const x = c.find((i) => i.level !== "healthy");
        return x ? x.key : null;
      },
      confirmText() {
        if (this.ctlConfirm === "restart") return tr("Restart AIO NAV? Alignment starts over.");
        const auto = this.s && this.s.readiness && this.s.readiness.autostart && this.s.readiness.autostart.enabled;
        return auto ? tr("Stop AIO NAV? Navigation output ends. It stays stopped until you press Resume or switch the system off and on.")
                    : tr("Stop AIO NAV? Navigation output ends.");
      },
      // For comparing with the vehicle's log: the 0x05 sequence, session and time of the snapshot.
      linkText() {
        const r = this.s && this.s.readiness, tx = this.s && this.s.status_udp;
        if (!this.connected || !r || !tx || !tx.enabled) return "";
        const at = r.observed_at ? new Date(r.observed_at).toLocaleTimeString([], { hour12: false }) : "—";
        return trf("Ready signal #{0}, session {1}, updated {2}", tx.sequence, r.session_id, at);
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
          this.ctlResult = { success: false, summary: tr("The device did not answer. Check the connection.") };
        } finally {
          this.ctlBusy = false;
          setTimeout(() => { if (this.ctlResult && this.ctlResult.success) this.ctlResult = null; }, 6000);
        }
      },
      aid(key) {
        const on = this.connected && this.s && this.s.aiding && this.s.aiding[key];
        return on ? { level: "active", label: tr("Active") } : { level: "idle", label: tr("Idle") };
      },
      get udp() {
        return this.connected && this.s && this.s.udp ? this.s.udp
          : { level: "unknown", label: tr("Unknown"), destinations: [] };
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
        if (u.level === "idle") return tr("Nothing is sent while AIO NAV is stopped.");
        if (u.level === "unknown") return tr("Nothing sent yet while AIO NAV starts.");
        const rate = AIO.isNum(u.rate_hz) ? fmt.hz(u.rate_hz) : "—";
        if (u.level === "fault") return AIO.isNum(u.age_s) ? trf("No output for {0} s.", Math.round(u.age_s)) : tr("No output received.");
        const out = dest ? trf("{0} to {1}", rate, dest) : trf("{0} (this device only)", rate);
        return out + (u.level === "warning" && AIO.isNum(u.expected_rate_hz) && AIO.isNum(u.rate_hz) && u.rate_hz < 0.9 * u.expected_rate_hz ? " " + trf("(expected {0})", fmt.hz(u.expected_rate_hz)) : "");
      },
      rateText() {
        const u = this.udp;
        if (!AIO.isNum(u.rate_hz)) return "—";
        return fmt.hz(u.rate_hz) + (AIO.isNum(u.expected_rate_hz) ? " / " + fmt.hz(u.expected_rate_hz) : "");
      },
      storageText() {
        const st = this.s && this.s.storage;
        return st ? trf("{0} free of {1}", fmt.bytes(st.free), fmt.bytes(st.total)) : "—";
      },
      setFollow(on) { this.follow = on; if (navMap) navMap.setFollow(on); },
      fitAll() { if (navMap) navMap.fitTrajectory(); },
    };
  };

  // ======================================================================= Settings page
  const postJson = async (url, body) => {
    try {
      const r = await fetch(url, { method: "POST", body: JSON.stringify(body || {}),
        headers: { "Content-Type": "application/json", "X-Requested-With": "aio-dashboard" } });
      return { status: r.status, j: await r.json() };
    } catch (e) {
      return { status: 0, j: { success: false, summary: tr("The device did not answer. Check the connection.") } };
    }
  };
  // Why a Wi-Fi change failed, in plain words (actions/wifi.py failure codes).
  const WIFI_FAIL = {
    wrong_password: "The password was not accepted.",
    not_found: "The network was not found. Move closer or check the name.",
    no_ip: "Joined the network, but it gave this device no address.",
    timeout: "The network did not answer in time.",
    denied: "This device is not set up to change Wi-Fi. Ask maintenance to run the installer with --with-wifi.",
  };

  window.settingsPage = function () {
    return {
      wifi: null, cfg: null, networks: [], scanning: false,
      choice: "", ssid: "", password: "", showPw: false,
      busy: "", result: "", resultClass: "", confirmRisks: [],
      rtkBusy: false, rtkAsk: false, rtkMsg: "", rtkClass: "",

      async init() {
        await Promise.all([this.loadWifi(), this.loadCfg()]);
        if (this.wifi && this.wifi.available && this.wifi.may_change) this.scan("auto");   // no forced scan on open
        setInterval(() => { if (!document.hidden) this.loadCfg(); }, 3000);
      },
      async loadWifi() {
        try { this.wifi = await (await fetch("/api/wifi", { cache: "no-store" })).json(); }
        catch (e) { this.wifi = { available: false }; }
      },
      async loadCfg() {
        try { this.cfg = await (await fetch("/api/settings", { cache: "no-store" })).json(); } catch (e) { /* keep the last */ }
      },

      // ----- Wi-Fi
      wifiNow() {
        const w = this.wifi;
        if (!w || !w.available) return "";
        return w.connection ? trf("Connected to {0}", w.connection) : tr("Not connected");
      },
      async scan(rescan) {
        this.scanning = true;
        const { j } = await postJson("/api/wifi/scan", { rescan: rescan === undefined ? true : rescan });
        this.scanning = false;
        if (j.success) this.networks = j.networks;
        else { this.result = j.summary || tr("Could not search for networks."); this.resultClass = "bad"; }
      },
      netText(n) {
        const strength = n.signal >= 67 ? tr("strong") : n.signal >= 34 ? tr("medium") : tr("weak");
        return n.ssid + " · " + strength + (n.security ? "" : " · " + tr("no password")) +
               (n.in_use ? " · " + tr("connected") : "");
      },
      picked() { this.password = ""; this.result = ""; this.confirmRisks = []; if (this.choice !== "__other__") this.ssid = ""; },
      target() { return this.choice === "__other__" ? this.ssid.trim() : this.choice; },
      needsPassword() {
        if (!this.target()) return false;
        if (this.choice === "__other__") return true;
        const n = this.networks.find((x) => x.ssid === this.choice);
        return !n || !!n.security;
      },
      pwOk() {
        const p = this.password.length;
        if (this.choice === "__other__") return p === 0 || p >= 8;   // a hidden network may be open
        return !this.needsPassword() || p >= 8;
      },
      async connect(confirm) {
        const name = this.target();
        this.busy = "connect"; this.result = ""; this.confirmRisks = [];
        const { status, j } = await postJson("/api/wifi/connect",
          { ssid: name, password: this.password, hidden: this.choice === "__other__", confirm: !!confirm });
        this.busy = "";
        if (status === 409 && j.needs_confirm) { this.confirmRisks = j.risks && j.risks.length ? j.risks : [j.summary]; return; }
        if (j.success) {
          this.result = trf("Connected to {0}.", name); this.resultClass = "ok";
          this.password = ""; this.choice = "";
        } else {
          const why = j.code ? tr(WIFI_FAIL[j.code] || "Could not connect.") : (j.summary || tr("Could not connect."));
          const back = j.code ? SP + (!j.restored ? tr("Wi-Fi is not connected now.") :
                                      j.kept ? trf("Still on {0}.", j.restored) : trf("Back on {0}.", j.restored)) : "";
          this.result = why + back; this.resultClass = "bad";
        }
        await this.loadWifi();
        this.scan("auto");
      },

      // ----- RTK requirement
      rtkExplain() {
        if (!this.cfg) return "";
        if (!this.cfg.rtk.required) return tr("Ready does not wait for RTK. The position may be accurate to only a few metres.");
        return this.cfg.rtk.min_quality === "fix" || this.cfg.rtk.min_quality === "fixed"
          ? tr("Ready waits for an RTK fix: a position accurate to a few centimetres.")
          : tr("Ready waits for RTK (float or fix): a position accurate to centimetres or decimetres.");
      },
      toggleRtk() {
        if (!this.cfg || this.rtkBusy) return;
        if (this.cfg.rtk.required) { this.rtkAsk = true; return; }
        this.setRtk(true, false);
      },
      async setRtk(required, confirm) {
        this.rtkBusy = true; this.rtkAsk = false;
        const { j } = await postJson("/api/settings/rtk", { required, confirm: !!confirm });
        this.rtkBusy = false;
        this.rtkMsg = j.summary || ""; this.rtkClass = j.success ? "ok" : "bad";
        await this.loadCfg();
      },

      // ----- automatic start and vehicle link
      autoChip() {
        const a = this.cfg && this.cfg.autostart;
        if (!a || !a.enabled) return { level: "idle", label: tr("Off") };
        if (a.hold) return { level: "warning", label: tr("Paused") };
        if (["gave_up", "prereq_timeout"].includes(a.state)) return { level: "fault", label: tr("Problem") };
        if (["done", "ready_this_boot", "running"].includes(a.state)) return { level: "healthy", label: tr("On") };
        return { level: "warning", label: tr("Starting") };
      },
      autoExplain() {
        const a = this.cfg && this.cfg.autostart;
        if (!a || !a.enabled) return tr("AIO NAV does not start by itself. Press Start on the Overview page.");
        if (a.hold) return tr("Stopped by an operator. It stays stopped until you press Resume or switch the system off and on.");
        const base = tr("AIO NAV starts by itself after power-on, as soon as the sensors are ready.");
        return a.detail && !["done", "running", "ready_this_boot"].includes(a.state) ? base + SP + a.detail : base;
      },
      vehicleChip() {
        const v = this.cfg && this.cfg.vehicle;
        if (!v || !v.nav) return { level: "warning", label: tr("Not set") };
        const st = v.status || {};
        if (!st.enabled) return { level: "idle", label: tr("Ready signal off") };
        if (st.destination_error || st.errors) return { level: "warning", label: tr("Problem") };
        return { level: "healthy", label: tr("Sending") };
      },
    };
  };

  window.dataPage = function () {
    return {
      roots: [], root: null, listing: null, error: null, loading: false, fmt,
      async init() {
        try {
          this.roots = await (await fetch("/api/data/roots")).json();
        } catch (e) { this.error = tr("Cannot load data locations"); return; }
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
