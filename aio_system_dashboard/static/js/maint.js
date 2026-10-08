/* Maintenance UI Alpine component (shared by all maintenance pages). */
(function () {
  "use strict";
  const fmt = AIO.fmt;
  const SEV = { unknown: 0, healthy: 1, warning: 2, fault: 3 };

  const SERVICE = {
    running: ["healthy", "Running"], stopped: ["fault", "Stopped"], failed: ["fault", "Failed"],
    not_installed: ["unknown", "Not installed"], unknown: ["unknown", "Unknown"],
  };
  const TOPIC = {
    healthy: ["healthy", "Active"], low_rate: ["warning", "Low rate"],
    stale: ["warning", "Stale"], missing: ["fault", "Missing"], not_found: ["unknown", "Not found"],
  };

  window.maintPage = function () {
    let lastOk = 0;
    return {
      d: null, events: [], connected: true, fmt,
      confirm: null,        // {action, target, label}
      running: {},          // "action:target" -> true
      results: {},          // "action:target" -> result
      eventFilter: "",
      // password lock of the config editor and the terminal
      auth: { configured: true, unlocked: false, expires_in_s: null, idle_s: 900 },
      unlockOpen: false, unlockPw: "", unlockMsg: "", unlockBusy: false, _afterUnlock: null, _onCancel: null,

      init() {
        AIO.poll("/api/maint/snapshot", 1000, (d) => { this.d = d; this.connected = true; lastOk = Date.now(); },
                 () => { if (Date.now() - lastOk > 4000) this.connected = false; });
        AIO.poll("/api/maint/events?limit=200", 3000, (e) => { this.events = e; });
        AIO.poll("/api/maint/auth", 15000, (a) => { this.auth = a; });
      },

      // ---------- password lock
      async loadAuth() {
        try { this.auth = await (await fetch("/api/maint/auth", { cache: "no-store" })).json(); } catch (e) { /* keep */ }
        return this.auth;
      },
      /* Run cb now if the tools are unlocked, else ask for the password first. */
      needUnlock(cb, onCancel) {
        if (this.auth.unlocked) { if (cb) cb(); return; }
        this._afterUnlock = cb || null; this._onCancel = onCancel || null;
        this.unlockPw = ""; this.unlockMsg = this.auth.configured ? "" :
          "No password is set on this device yet. Run 'aio-dashboard password' on it first.";
        this.unlockOpen = true;
        setTimeout(() => { const el = document.getElementById("unlock-pw"); if (el) el.focus(); }, 50);
      },
      async unlock() {
        this.unlockBusy = true; this.unlockMsg = "";
        try {
          const r = await fetch("/api/maint/auth/unlock", {
            method: "POST", headers: { "Content-Type": "application/json", "X-Requested-With": "aio-dashboard" },
            body: JSON.stringify({ password: this.unlockPw }) });
          const j = await r.json();
          this.unlockPw = "";
          if (!j.success) { this.unlockMsg = j.summary || "wrong password"; return; }
          await this.loadAuth();
          this.unlockOpen = false;
          const cb = this._afterUnlock; this._afterUnlock = null; this._onCancel = null;
          if (cb) cb();
        } catch (e) { this.unlockMsg = "request failed: " + e.message; }
        finally { this.unlockBusy = false; }
      },
      cancelUnlock() {
        this.unlockOpen = false; this.unlockPw = "";
        const c = this._onCancel; this._onCancel = null; this._afterUnlock = null;
        if (c) c();
      },
      async lockTools() {
        await fetch("/api/maint/auth/lock", { method: "POST",
          headers: { "Content-Type": "application/json", "X-Requested-With": "aio-dashboard" }, body: "{}" });
        await this.loadAuth();
        window.dispatchEvent(new CustomEvent("tools-locked"));
      },
      /* fetch + JSON for the protected tools: a 401 opens the password dialog and retries after it. */
      async toolFetch(url, opts) {
        const r = await fetch(url, Object.assign({ headers: { "Content-Type": "application/json", "X-Requested-With": "aio-dashboard" } }, opts || {}));
        let j = {};
        try { j = await r.json(); } catch (e) { j = { success: false, summary: "HTTP " + r.status }; }
        if (r.status === 401 && j.locked) {
          this.auth.unlocked = false;
          return await new Promise((resolve) => this.needUnlock(
            async () => resolve(await this.toolFetch(url, opts)),
            () => resolve({ success: false, locked: true, summary: "locked: not saved" })));
        }
        return j;
      },

      // ---------- generic
      lvl(level, label) { return { level: level || "unknown", label: label || AIO.LEVEL_TEXT[level] || "Unknown" }; },
      get productState() { return this.connected && this.d && this.d.maint ? this.d.maint.product_state : "UNKNOWN"; },
      get state() { return this.productState; },
      get stateLabel() { return { READY: "Ready", INITIALIZING: "Initializing", FAULT: "Fault", STARTING: "Starting", STOPPED: "Stopped" }[this.productState] || "Unknown"; },
      get stateReasons() {
        if (!this.connected) return ["Lost connection to the dashboard. Values on this page are not live."];
        const h = this.d && this.d.product && this.d.product.health;
        if (this.productState === "READY") return ["AIO NAV is available"];
        return (h && h.reasons && h.reasons.length) ? h.reasons : ["Waiting for NAV data"];
      },
      get issues() { return (this.d && this.d.maint && this.d.maint.issues) || []; },
      layer(name) {
        const l = this.d && this.d.maint && this.d.maint.layers ? this.d.maint.layers[name] : null;
        return this.lvl(l, { healthy: "OK", warning: "Warning", fault: "Fault" }[l]);
      },
      pageLevel(page) {
        if (["config", "terminal", "replay"].includes(page)) return "idle";   // tools, not health
        if (!this.d) return "unknown";
        if (page === "overview") return { READY: "healthy", FAULT: "fault", INITIALIZING: "warning" }[this.productState] || "unknown";
        const rel = this.issues.filter((i) => page === "diagnostics" || i.link === page ||
          (page === "ros" && i.layer === "dataflow" && i.link) || (page === "network" && i.layer === "network"));
        return rel.reduce((acc, i) => (SEV[i.level] > SEV[acc] ? i.level : acc), "healthy");
      },
      productUrl() { return location.protocol + "//" + location.hostname + ":8080/"; },

      // ---------- system / services
      get sys() { return (this.d && this.d.system) || {}; },
      get services() { return (this.d && this.d.services) || {}; },
      // Sensor drivers (a systemd user unit): deliberately stopped is not an alarm, so grey, not red.
      driverChip(key) {
        const s = this.services[key];
        if (!s) return this.lvl("unknown", "Unknown");
        if (s.unit_state && s.unit_state.active === "activating") return this.lvl("warning", "Starting");
        if (s.state === "running") return this.lvl("healthy", "Running");
        if (s.state === "failed") return this.lvl("fault", "Failed");
        return this.lvl("unknown", "Stopped");
      },
      driverNote(key) {
        const s = this.services[key];
        if (!s) return "";
        const running = s.state === "running";
        if (this.rosMode.mode === "live" && !running)
          return "Live is selected but the sensor drivers are stopped. Start them to get camera and IMU data.";
        return "";
      },
      serviceChip(key) {
        const s = this.services[key];
        // AIO NAV (and what starts and stops with it, DSO) stopped on purpose is normal: grey, not red.
        const c = (this.d && this.d.config) || {};
        const group = (c.nav_group || []).concat(c.nav_service ? [c.nav_service] : []);
        if (group.includes(key) && this.productState === "STOPPED" && s && s.state === "stopped") return this.lvl("idle", "Stopped");
        const m = SERVICE[(s && s.state) || "unknown"] || SERVICE.unknown;
        return this.lvl(m[0], m[1]);
      },
      pct(v) { return AIO.isNum(v) ? v.toFixed(0) + "%" : "—"; },
      memText() { const s = this.sys; return AIO.isNum(s.mem_used) ? fmt.bytes(s.mem_used) + " / " + fmt.bytes(s.mem_total) : "—"; },
      rootDisk() { return (this.sys.disks || [])[0] || null; },
      levelFor(v, warn, fault) { return !AIO.isNum(v) ? "unknown" : v >= fault ? "fault" : v >= warn ? "warning" : "healthy"; },

      // ---------- data flow / ROS
      get dataflow() { return (this.d && this.d.maint && this.d.maint.dataflow) || []; },
      get ros() { return (this.d && this.d.ros) || {}; },
      get rosMode() {
        return ((this.d && this.d.config) || {}).ros_mode || { mode: null, label: "", domain_id: null, localhost_only: null, modes: [] };
      },
      waitForRestart() {
        // The dashboard stops itself and systemd starts it again; reload once it answers.
        setTimeout(() => {
          const tick = async () => {
            try {
              const r = await fetch("/api/maint/snapshot", { cache: "no-store" });
              if (r.ok) { location.reload(); return; }
            } catch (e) { /* still down */ }
            setTimeout(tick, 1000);
          };
          tick();
        }, 2500);
      },
      get dso() { return ((this.d && this.d.watchdog) || {}).dso || {}; },
      dsoChip() {
        const m = { ok: ["healthy", "OK"], settling: ["warning", "Settling"], restarting: ["warning", "Restarting"],
                    bad: ["warning", "NaN"], retrying: ["warning", "Retrying"], idle: ["unknown", "DSO not running"],
                    waiting: ["unknown", "Waiting"], disabled: ["unknown", "Disabled"] };
        const e = m[this.dso.state] || ["unknown", "Unknown"];
        return this.lvl(e[0], e[1]);
      },
      topicChip(t) { const m = TOPIC[t.state] || ["unknown", "Unknown"]; return this.lvl(m[0], m[1]); },
      topicsFor(group) { return (this.ros.watched || []).filter((t) => t.group === group); },
      rateText(r, exp) { return AIO.isNum(r) ? fmt.hz(r) + (AIO.isNum(exp) ? " / " + fmt.hz(exp) : "") : "—"; },

      // ---------- network / devices
      get net() { return (this.d && this.d.network) || {}; },
      device(key) { return (this.net.devices || {})[key] || {}; },
      reachChip(key) {
        const r = this.device(key).reachable;
        return r === true ? this.lvl("healthy", "Reachable") : r === false ? this.lvl("fault", "Unreachable") : this.lvl("unknown", "Unknown");
      },
      linkChip() {
        const up = this.net.sensor_link_up;
        return up === true ? this.lvl("healthy", "Up") : up === false ? this.lvl("fault", "Down") : this.lvl("unknown", "Unknown");
      },
      ifaceChip(i) { return i.up && i.carrier !== false ? this.lvl("healthy", "Up") : this.lvl("fault", i.up ? "No carrier" : "Down"); },
      get product() { return (this.d && this.d.product) || {}; },
      udpChip() { const u = this.product.udp; return u ? this.lvl(u.level, u.label) : this.lvl("unknown"); },

      // ---------- actions
      key(action, target) { return action + ":" + target; },
      ask(action, target, label, needsConfirm) {
        if (needsConfirm) { this.confirm = { action, target, label }; } else { this.run(action, target); }
      },
      async run(action, target) {
        this.confirm = null;
        const k = this.key(action, target);
        this.running[k] = true;
        this.results[k] = null;
        try {
          const r = await fetch("/api/maint/actions/" + encodeURIComponent(action), {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-Requested-With": "aio-dashboard" },
            body: JSON.stringify({ target }),
          });
          this.results[k] = await r.json();
        } catch (e) {
          this.results[k] = { success: false, summary: "request failed: " + e.message };
        } finally {
          this.running[k] = false;
        }
      },
      result(action, target) { return this.results[this.key(action, target)]; },
      isRunning(action, target) { return !!this.running[this.key(action, target)]; },

      // ---------- events
      filteredEvents() {
        return this.eventFilter ? this.events.filter((e) => e.category === this.eventFilter) : this.events;
      },
      actionEvents() { return this.events.filter((e) => e.category === "action" || e.category === "diagnostic"); },
    };
  };
})();
