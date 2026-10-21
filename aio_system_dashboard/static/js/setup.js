/* Product UI > Setup: the lever arms (web/setup.py). Axes: forward, right, down from the IMU. */
(function () {
  "use strict";
  const AXES = ["forward", "right", "down"];
  const ROWS = [
    { key: "gnss", label: "GNSS antenna", help: "to the centre of the antenna" },
    { key: "vehicle", label: "Rear axle centre", help: "to the middle of the rear axle (AIO NAV's vehicle reference point)" },
  ];
  // Side view: forward to the right, down downwards. Top view: forward to the right, right downwards.
  const VIEWS = [{ key: "side", title: "Side view", down: "down", axis: 2 }, { key: "top", title: "Top view", down: "right", axis: 1 }];
  const AREA = { x0: 48, x1: 278, y0: 52, y1: 150 };          // inside the 320 x 170 drawing
  // A typical car, in metres from the middle of its rear axle (x forward, y right, z down), drawn with
  // its rear axle at the vehicle reference point (which is the rear axle centre). It shows the directions;
  // its size is an example.
  const CAR = {
    side: [[-1.0, 0.05], [-1.0, -0.55], [-0.85, -0.62], [-0.5, -0.66], [0.1, -1.12], [0.25, -1.17], [1.75, -1.17],
           [2.35, -0.7], [3.45, -0.6], [3.7, -0.45], [3.7, 0.05]],
    sideGlass: [[[0.15, -1.08], [0.95, -1.08], [0.95, -0.72], [-0.25, -0.72]],
                [[1.05, -1.08], [1.7, -1.08], [2.22, -0.72], [1.05, -0.72]]],
    top: [[-0.85, -0.92], [3.3, -0.92], [3.6, -0.8], [3.7, -0.5], [3.7, 0.5], [3.6, 0.8], [3.3, 0.92],
          [-0.85, 0.92], [-1.0, 0.8], [-1.0, -0.8]],
    topGlass: [[[2.15, -0.8], [2.4, -0.84], [2.4, 0.84], [2.15, 0.8]], [[0.2, -0.8], [0.0, -0.84], [0.0, 0.84], [0.2, 0.8]]],
    wheelR: 0.33, wheelbase: 2.8, front: 3.7, rear: -1.0, halfWidth: 0.92,
  };
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

      // ---------- drawing: the points and the car, to scale, the same scale on both axes
      geom(view) {
        const ax = VIEWS.find((x) => x.key === view).axis;
        const at = (a) => [isNum(a[0]) ? a[0] : 0, isNum(a[ax]) ? a[ax] : 0];
        const ref = at(this.form.vehicle);
        const car = (q) => [ref[0] + q[0], ref[1] + q[1]];          // car metres -> metres from the IMU
        const pts = { imu: [0, 0], gnss: at(this.form.gnss), vehicle: ref };
        const outline = (view === "side" ? CAR.side.concat([[0, CAR.wheelR], [CAR.wheelbase, CAR.wheelR]]) : CAR.top).map(car);
        const all = Object.values(pts).concat(outline);
        const xs = all.map((q) => q[0]), ys = all.map((q) => q[1]);
        const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
        const s = Math.min((AREA.x1 - AREA.x0) / Math.max(maxX - minX, 0.5), (AREA.y1 - AREA.y0) / Math.max(maxY - minY, 0.5));
        const cx = (AREA.x0 + AREA.x1) / 2 - ((minX + maxX) / 2) * s, cy = (AREA.y0 + AREA.y1) / 2 - ((minY + maxY) / 2) * s;
        const S = (q) => ({ x: cx + q[0] * s, y: cy + q[1] * s });
        return { S, car, pts, s };
      },
      pt(view, which) { const g = this.geom(view); return g.S(g.pts[which]); },
      cross(c) { return `M${c.x - 6} ${c.y - 6} L${c.x + 6} ${c.y + 6} M${c.x + 6} ${c.y - 6} L${c.x - 6} ${c.y + 6}`; },
      poly(g, list) { return "M" + list.map((q) => { const p = g.S(g.car(q)); return p.x.toFixed(1) + " " + p.y.toFixed(1); }).join(" L") + " Z"; },
      carPath(view) { const g = this.geom(view); return this.poly(g, view === "side" ? CAR.side : CAR.top); },
      glassPath(view) { const g = this.geom(view); return (view === "side" ? CAR.sideGlass : CAR.topGlass).map((w) => this.poly(g, w)).join(" "); },
      wheelPath(view) {
        const g = this.geom(view), r = CAR.wheelR;
        if (view === "side") {
          return [0, CAR.wheelbase].map((x) => {
            const c = g.S(g.car([x, 0])), R = r * g.s;
            return `M${c.x - R} ${c.y} a${R} ${R} 0 1 0 ${2 * R} 0 a${R} ${R} 0 1 0 ${-2 * R} 0`;
          }).join(" ");
        }
        const w = CAR.halfWidth;
        return [0, CAR.wheelbase].flatMap((x) => [[w - 0.12, w + 0.1], [-w - 0.1, -w + 0.12]].map(([y0, y1]) =>
          this.poly(g, [[x - r, y0], [x + r, y0], [x + r, y1], [x - r, y1]]))).join(" ");
      },
      groundY() { const g = this.geom("side"); return g.S(g.car([0, CAR.wheelR])).y; },
      // FRONT / REAR beyond the ends of the car; LEFT / RIGHT SIDE above and below it (top view)
      carLabel(view, which) {
        const g = this.geom(view), mid = view === "side" ? -0.45 : 0;
        const p = { front: [CAR.front, mid], rear: [CAR.rear, mid], left: [1.35, -CAR.halfWidth - 0.1], right: [1.35, CAR.halfWidth + 0.1] }[which];
        const q = g.S(g.car(p));
        if (which === "front") return { x: q.x + 5, y: q.y + 3, a: "start" };
        if (which === "rear") return { x: q.x - 5, y: q.y + 3, a: "end" };
        return { x: q.x, y: which === "left" ? q.y - 5 : q.y + 12, a: "middle" };
      },
      // A point's name on the side away from the IMU, so the names do not overlap
      label(view, which) {
        const p = this.pt(view, which), imu = this.pt(view, "imu");
        if (which === "imu") return { x: p.x + 10, y: p.y + 4, a: "start" };
        if (which === "vehicle" && view === "top") return { x: p.x, y: p.y + 20, a: "middle" };   // REAR is beside it
        if (Math.abs(p.x - imu.x) < 4) return { x: p.x, y: p.y + (p.y < imu.y ? -10 : 18), a: "middle" };
        return p.x < imu.x ? { x: p.x - 10, y: p.y + 4, a: "end" } : { x: p.x + 10, y: p.y + 4, a: "start" };
      },
    };
  };
})();
