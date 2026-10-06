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
    stale: ["warning", "Stale"], missing: ["fault", "Missing"],
  };

  window.maintPage = function () {
    let lastOk = 0;
    return {
      d: null, events: [], connected: true, fmt,
      confirm: null,        // {action, target, label}
      running: {},          // "action:target" -> true
      results: {},          // "action:target" -> result
      eventFilter: "",

      init() {
        AIO.poll("/api/maint/snapshot", 1000, (d) => { this.d = d; this.connected = true; lastOk = Date.now(); },
                 () => { if (Date.now() - lastOk > 4000) this.connected = false; });
        AIO.poll("/api/maint/events?limit=200", 3000, (e) => { this.events = e; });
      },

      // ---------- generic
      lvl(level, label) { return { level: level || "unknown", label: label || AIO.LEVEL_TEXT[level] || "Unknown" }; },
      get productState() { return this.connected && this.d && this.d.maint ? this.d.maint.product_state : "UNKNOWN"; },
      get state() { return this.productState; },
      get stateLabel() { return { READY: "Ready", INITIALIZING: "Initializing", FAULT: "Fault" }[this.productState] || "Unknown"; },
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
        if (!this.d) return "unknown";
        if (page === "overview") return this.productState === "READY" ? "healthy" : this.productState === "FAULT" ? "fault" : this.productState === "INITIALIZING" ? "warning" : "unknown";
        const rel = this.issues.filter((i) => page === "diagnostics" || i.link === page ||
          (page === "ros" && i.layer === "dataflow" && i.link) || (page === "network" && i.layer === "network"));
        return rel.reduce((acc, i) => (SEV[i.level] > SEV[acc] ? i.level : acc), "healthy");
      },
      productUrl() { return location.protocol + "//" + location.hostname + ":8080/"; },

      // ---------- system / services
      get sys() { return (this.d && this.d.system) || {}; },
      get services() { return (this.d && this.d.services) || {}; },
      serviceChip(key) {
        const s = this.services[key];
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
