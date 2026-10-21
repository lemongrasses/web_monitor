/* Product UI > Setup: the lever arms (web/setup.py). Axes: forward, right, down from the IMU. */
(function () {
  "use strict";
  const AXES = ["forward", "right", "down"];
  const ROWS = [
    { key: "gnss", label: "GNSS antenna", help: "to the centre of the antenna" },
    { key: "vehicle", label: "Vehicle reference point", help: "to the point AIO NAV uses for the vehicle's motion (usually the middle of the rear axle)" },
  ];
  // Side view: forward to the right, down downwards. Top view: forward to the right, right downwards.
  const VIEWS = [{ key: "side", title: "Side view", down: "down", axis: 2 }, { key: "top", title: "Top view", down: "right", axis: 1 }];
  const AREA = { x0: 70, x1: 250, y0: 30, y1: 150 };          // inside the 320 x 170 drawing
  const isNum = (v) => typeof v === "number" && isFinite(v);
  const copy = (a) => (Array.isArray(a) ? a.slice() : [null, null, null]);

  window.setupPage = function () {
    return {
      AXES, VIEWS, rows: ROWS,
      info: null, form: { vehicle: [null, null, null], gnss: [null, null, null] },
      busy: false, msg: "", msgClass: "", error: "", confirmRestart: false,

      async setupInit() {
        await this.load(true);
        setInterval(() => this.load(false), 3000);   // AIO NAV running? vehicle still? changed elsewhere?
      },
      async load(fillForm) {
        let j;
        try { j = await (await fetch("/api/setup/lever-arms", { cache: "no-store" })).json(); }
        catch (e) { if (fillForm) this.error = "Cannot reach the device: " + e.message; return; }
        if (!j.available) { this.error = j.summary || "Not available."; this.info = null; return; }
        this.error = "";
        const changedElsewhere = this.info && j.version !== this.info.version;
        const keep = !fillForm && this.dirty();
        this.info = j;
        if (fillForm || (changedElsewhere && !keep)) this.form = { vehicle: copy(j.vehicle), gnss: copy(j.gnss) };
        else if (changedElsewhere && keep) { this.msg = "The values were changed on another page meanwhile; saving now would be refused. Reload to see them."; this.msgClass = "bad"; }
      },

      // ---------- form
      bad(key, i) { const v = this.form[key][i]; return !isNum(v) || Math.abs(v) > (this.info ? this.info.max_m : 20); },
      valid() { return ROWS.every((r) => [0, 1, 2].every((i) => !this.bad(r.key, i))); },
      dirty() {
        if (!this.info) return false;
        return ROWS.some((r) => [0, 1, 2].some((i) => !this.info[r.key] || this.form[r.key][i] !== this.info[r.key][i]));
      },
      canRestart() { return !!(this.info && this.info.can_restart && this.info.active && this.info.nav_running); },
      applyNote() {
        const i = this.info;
        if (!i) return "";
        if (!i.active) return "The Live mode is not in use now: the values apply when it is.";
        if (!i.nav_running) return "They apply when AIO NAV starts.";
        if (i.still !== true) return "AIO NAV is running: Save alone applies them at its next start. Restarting it here needs the vehicle still.";
        return "AIO NAV is running: Save alone applies them at its next start.";
      },
      async save(restart) {
        this.busy = restart ? "restart" : "save"; this.msg = "";
        const body = { vehicle: this.form.vehicle, gnss: this.form.gnss, base_version: this.info.version, restart: !!restart };
        let j;
        try {
          const r = await fetch("/api/setup/lever-arms", {
            method: "POST", headers: { "Content-Type": "application/json", "X-Requested-With": "aio-dashboard" },
            body: JSON.stringify(body) });
          j = await r.json();
        } catch (e) { j = { success: false, summary: "request failed: " + e.message }; }
        this.busy = false; this.confirmRestart = false;
        this.msg = j.summary || ""; this.msgClass = j.success ? "ok" : "bad";
        if (j.saved || j.conflict) await this.load(true);
      },

      // ---------- drawing (to scale, same scale on both axes)
      pts(view) {
        const v = VIEWS.find((x) => x.key === view);
        const at = (a) => [isNum(a[0]) ? a[0] : 0, isNum(a[v.axis]) ? a[v.axis] : 0];
        return { imu: [0, 0], gnss: at(this.form.gnss), vehicle: at(this.form.vehicle) };
      },
      pt(view, which) {
        const p = this.pts(view), all = Object.values(p);
        const xs = all.map((q) => q[0]), ys = all.map((q) => q[1]);
        const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
        const W = AREA.x1 - AREA.x0, H = AREA.y1 - AREA.y0;
        const s = Math.min(W / Math.max(maxX - minX, 0.5), H / Math.max(maxY - minY, 0.5));
        const cx = (AREA.x0 + AREA.x1) / 2 - ((minX + maxX) / 2) * s, cy = (AREA.y0 + AREA.y1) / 2 - ((minY + maxY) / 2) * s;
        return { x: cx + p[which][0] * s, y: cy + p[which][1] * s };
      },
      cross(c) { return `M${c.x - 6} ${c.y - 6} L${c.x + 6} ${c.y + 6} M${c.x + 6} ${c.y - 6} L${c.x - 6} ${c.y + 6}`; },
    };
  };
})();
