/* Maintenance > Still detection: live still / moving, measuring the limits, applying them.
   Nested inside maintPage(), so toolFetch() (password prompt on 401) comes from there. */
(function () {
  "use strict";
  const KEYS = ["gyro_rad_s", "accel_std", "speed_m_s"];
  const NAMES = { gyro_rad_s: "Rotation", accel_std: "Shake", speed_m_s: "GNSS speed" };
  const UNITS = { gyro_rad_s: "rad/s", accel_std: "m/s²", speed_m_s: "m/s" };
  const NOW = { gyro_rad_s: "gyro_max", accel_std: "accel_std", speed_m_s: "speed" };   // keys of the judgement
  const isNum = (v) => typeof v === "number" && isFinite(v);

  window.stillPage = function () {
    return {
      keys: KEYS, NAMES, UNITS,
      s: null, form: {}, formFrom: "", srcKey: null, busy: false, msg: "", msgClass: "", mmsg: "",

      stillInit() {
        AIO.poll("/api/maint/still", 1000, (j) => { this.s = j; this.syncForm(); });
      },
      get cal() { return (this.s && this.s.calibration) || { state: "idle" }; },

      // ---------- the form: the proposal of a new measurement, else the limits in use
      syncForm() {
        const p = this.cal.proposal, src = p || this.s.limits;
        const key = JSON.stringify([!!p, KEYS.map((k) => src[k])]);
        if (key === this.srcKey) return;
        this.srcKey = key;
        const form = {};
        for (const k of KEYS) form[k] = src[k];
        this.form = form;
        this.formFrom = p ? "Proposed from the measurement; check them, then apply" : "In use";
      },
      formChanged() {
        return !!this.s && KEYS.some((k) => Number(this.form[k]) !== this.s.limits[k]);
      },
      range(k) { return (this.s && this.s.ranges[k]) || [0, 1]; },
      rangeText(k) { const r = this.range(k); return r[0] + "–" + r[1] + " " + UNITS[k]; },
      fieldNote(k) {
        const m = this.cal.measured;
        if (this.cal.proposal && m) return isNum(m[k]) ? "largest while still: " + this.num(m[k]) : "not measured (no GNSS speed)";
        return this.s ? "default " + this.num(this.s.defaults[k]) : "";
      },

      // ---------- now
      num(v) { return isNum(v) ? (Math.abs(v) < 1 ? v.toFixed(3) : v.toFixed(2)) : "—"; },
      limit(k) { return this.s ? this.s.limits[k] : null; },
      value(k) { return this.s && this.s.now ? this.s.now[NOW[k]] : null; },
      nowText(k) { const v = this.value(k); return isNum(v) ? this.num(v) + " " + UNITS[k] : "—"; },
      over(k) { const v = this.value(k); return isNum(v) && v >= this.limit(k); },
      meterPct(k) {   // the limit sits at two thirds of the bar
        const v = this.value(k), l = this.limit(k);
        return isNum(v) && l ? Math.min(100, (v / (l * 1.5)) * 100) : 0;
      },
      meterTitle(k) { return NAMES[k] + ": " + this.nowText(k) + ", limit " + this.num(this.limit(k)) + " " + UNITS[k]; },
      speedUsed() { return !!(this.s && this.s.now && this.s.now.speed_used); },
      speedNote() { return (this.s && this.s.now && this.s.now.speed_note) || ""; },
      vehicleLamp() {
        const n = this.s && this.s.now;
        if (!n || n.still === null || n.still === undefined) return { level: "unknown", label: (n && n.reason) || "Unknown" };
        return n.still ? { level: "healthy", label: "Still" } : { level: "warning", label: "Moving · " + n.reason.replace(/^moving: /, "") };
      },
      limitsSource() {
        const l = this.s && this.s.limits;
        if (!l) return "";
        if (l.how === "default") return "Default limits (from the settings)";
        return (l.how === "measured" ? "Measured " : "Set by hand ") + AIO.fmt.datetime(l.at);
      },

      // ---------- measuring
      running() { return this.cal.state === "running"; },
      progressPct() { return Math.min(100, (this.cal.elapsed_s / this.cal.duration_s) * 100); },
      lastText() {
        const l = this.cal.last;
        return l ? "Now: " + KEYS.map((k) => NAMES[k] + " " + (isNum(l[k]) ? this.num(l[k]) : "—")).join(", ") : "";
      },
      resultClass() {
        return { done: "check-ok", warning: "cfg-warn", failed: "check-fail" }[this.cal.state] || "muted";
      },
      async post(url, body) {
        return await this.toolFetch(url, { method: "POST", body: JSON.stringify(body || {}) });
      },
      async measure(kind) {
        this.busy = "measure"; this.mmsg = "";
        const j = await this.post("/api/maint/still/measure", { kind });
        this.busy = false;
        if (!j.success) this.mmsg = j.summary || "could not start the measurement";
        else this.s.calibration = j;
      },
      async cancel() { await this.post("/api/maint/still/cancel"); },

      // ---------- applying
      async apply() {
        const values = {};
        for (const k of KEYS) values[k] = Number(this.form[k]);
        this.busy = "apply"; this.msg = "";
        const j = await this.post("/api/maint/still/apply", { values });
        this.busy = false;
        this.msg = j.summary || (j.success ? "New limits in use." : "not applied");
        this.msgClass = j.success ? "ok" : "bad";
        if (j.success) { this.s.limits = j.limits; this.srcKey = null; this.syncForm(); }
      },
      async resetDefaults() {
        if (!confirm("Go back to the default limits from the settings?")) return;
        this.busy = "reset"; this.msg = "";
        const j = await this.post("/api/maint/still/reset");
        this.busy = false;
        this.msg = j.summary || ""; this.msgClass = j.success ? "ok" : "bad";
        if (j.success) { this.s.limits = j.limits; this.srcKey = null; this.syncForm(); }
      },
    };
  };
})();
