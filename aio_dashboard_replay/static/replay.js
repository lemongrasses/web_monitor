/* Replay page (optional module): Live / Bag replay switch and the bag player.
   Nested inside maintPage(), so ask() / isRunning() / waitForRestart() come from there. */
(function () {
  "use strict";

  const post = async (url, body, method) => {
    const r = await fetch(url, {
      method: method || "POST",
      headers: { "Content-Type": "application/json", "X-Requested-With": "aio-dashboard" },
      body: JSON.stringify(body || {}),
    });
    let j = {};
    try { j = await r.json(); } catch (e) { j = { success: false, summary: "HTTP " + r.status }; }
    if (!r.ok && j.success === undefined) j.success = false;
    return j;
  };

  window.replayPage = function () {
    return {
      rp: { mode: {}, checks: [], player: {} },
      modePick: null, modeConfirm: false, modeBusy: false, modeMsg: "", modeMsgClass: "",
      bags: [], roots: [], bagsBusy: false,
      sel: null, schema: [], defaults: {}, opts: { topics: [] }, remapText: "",
      presets: {}, presetPick: "", presetName: "",
      cmdPreview: "", cmdError: "",
      playBusy: false, playMsg: "", playMsgClass: "",
      ctlBusy: false, ctlMsg: "", ctlMsgClass: "", rateNow: 1,

      async rpInit() {
        const o = await (await fetch("/api/replay/options")).json();
        this.schema = o.options; this.defaults = o.defaults;
        this.opts = this.copy(o.defaults);
        this.loadBags(false);
        this.loadPresets();
        const want = new URLSearchParams(location.search).get("bag");    // /replay?bag=<id>
        if (want) this.selectBag(want);
        AIO.poll("/api/replay/status", 1000, (d) => {
          this.rp = d;
          if (this.modePick === null && d.mode) this.modePick = d.mode.mode;
          if (d.player && d.player.status === "playing" && !this.ctlBusy && document.activeElement.id !== "rp-rate-now")
            this.rateNow = d.player.rate_now;
        });
      },

      copy(x) { return JSON.parse(JSON.stringify(x)); },
      get pl() { return this.rp.player || {}; },
      isPlaying() { return this.pl.status === "playing"; },
      playChip() {
        const m = { playing: ["healthy", this.pl.paused ? "Paused" : "Playing"], finished: ["unknown", "Finished"],
                    stopped: ["unknown", "Stopped"], failed: ["fault", "Failed"] };
        const e = m[this.pl.status] || ["unknown", "Idle"];
        return this.lvl(e[0], e[1]);
      },
      pctDone() {
        const d = this.pl.duration_s, p = this.pl.position_s;
        return d > 0 && AIO.isNum(p) ? Math.min(100, Math.max(0, 100 * p / d)) : 0;
      },
      fmtClock(s) {
        if (!AIO.isNum(s)) return "—";
        s = Math.max(0, Math.round(s));
        const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
        const two = (n) => String(n).padStart(2, "0");
        return (h ? h + ":" + two(m) : m) + ":" + two(x);
      },

      // ---------- mode
      async applyMode(mode) {
        this.modeConfirm = false; this.modeBusy = true;
        this.modeMsg = "Switching…"; this.modeMsgClass = "";
        try {
          const j = await post("/api/replay/mode", { mode });
          this.modeMsg = j.summary || ""; this.modeMsgClass = j.success ? "ok" : "bad";
          if (j.success && j.restarting) { this.waitForRestart(); return; }
        } catch (e) { this.modeMsg = "request failed: " + e.message; this.modeMsgClass = "bad"; }
        this.modeBusy = false;
      },

      // ---------- bags
      async loadBags(refresh) {
        this.bagsBusy = true;
        try {
          const j = await (await fetch("/api/replay/bags" + (refresh ? "?refresh=1" : ""))).json();
          this.bags = j.bags; this.roots = j.roots;
        } finally { this.bagsBusy = false; }
      },
      async selectBag(id, opts) {
        const r = await fetch("/api/replay/bag/" + encodeURIComponent(id));
        if (!r.ok) { this.playMsg = "bag not found"; this.playMsgClass = "bad"; return; }
        const b = await r.json();
        this.sel = b;
        const base = this.copy(this.defaults);
        this.opts = Object.assign(base, opts || b.last_options || {});
        const names = new Set(b.topics.map((t) => t.name));
        this.opts.topics = (this.opts.topics || []).filter((t) => names.has(t));
        this.remapText = (this.opts.remap || []).join("\n");
        this.playMsg = "";
        this.preview();
      },
      basicOpts() { return this.schema.filter((o) => o.basic && o.kind !== "topics"); },
      advancedOpts() { return this.schema.filter((o) => !o.basic); },
      advancedChanged() {
        return this.advancedOpts().some((o) => o.kind !== "remap" &&
          JSON.stringify(this.opts[o.key]) !== JSON.stringify(this.defaults[o.key]) &&
          !(this.opts[o.key] === "" && this.defaults[o.key] === null)) || !!this.remapText.trim();
      },
      resetOpts() {
        const topics = this.opts.topics;
        this.opts = Object.assign(this.copy(this.defaults), { topics });
        this.remapText = "";
        this.preview();
      },
      payload() {
        const o = this.copy(this.opts);
        o.remap = this.remapText.split("\n").map((l) => l.trim()).filter(Boolean);
        return { bag: this.sel && this.sel.id, options: o };
      },
      async preview() {
        if (!this.sel) return;
        const j = await post("/api/replay/preview", this.payload());
        this.cmdPreview = j.success ? j.command : "";
        this.cmdError = j.success ? "" : j.summary;
      },

      // ---------- play / stop / live control
      async play() {
        this.playBusy = true; this.playMsg = "Starting…"; this.playMsgClass = "";
        try {
          const j = await post("/api/replay/play", this.payload());
          this.playMsg = j.summary || ""; this.playMsgClass = j.success ? "ok" : "bad";
          if (j.success) document.getElementById("now-title").scrollIntoView({ behavior: "smooth" });
        } catch (e) { this.playMsg = "request failed: " + e.message; this.playMsgClass = "bad"; }
        this.playBusy = false;
      },
      async stopPlay() {
        this.ctlBusy = "stop"; this.ctlMsg = "Stopping…"; this.ctlMsgClass = "";
        const j = await post("/api/replay/stop");
        this.ctlMsg = j.summary || ""; this.ctlMsgClass = j.success ? "ok" : "bad";
        this.ctlBusy = false;
      },
      async control(action, rate) {
        this.ctlBusy = action; this.ctlMsg = "Asking the player…"; this.ctlMsgClass = "";
        const j = await post("/api/replay/control", { action, rate });
        this.ctlMsg = j.summary || ""; this.ctlMsgClass = j.success ? "ok" : "bad";
        this.ctlBusy = false;
      },

      // ---------- presets
      async loadPresets() { this.presets = await (await fetch("/api/replay/presets")).json(); },
      async savePreset() {
        const name = this.presetName.trim();
        const j = await post("/api/replay/presets", Object.assign({ name }, this.payload()));
        this.playMsg = j.summary || ""; this.playMsgClass = j.success ? "ok" : "bad";
        if (j.success) { await this.loadPresets(); this.presetPick = name; this.presetName = ""; }
      },
      async loadPreset() {
        const p = this.presets[this.presetPick];
        if (!p) return;
        if (!this.bags.some((b) => b.id === p.bag_id)) {
          this.playMsg = "the bag of this preset (" + p.bag_name + ") was not found"; this.playMsgClass = "bad";
          return;
        }
        await this.selectBag(p.bag_id, p.options);
        this.playMsg = "loaded preset '" + this.presetPick + "'"; this.playMsgClass = "ok";
      },
      async deletePreset() {
        const name = this.presetPick;
        const j = await post("/api/replay/presets", { name }, "DELETE");
        this.playMsg = j.summary || ""; this.playMsgClass = j.success ? "ok" : "bad";
        this.presetPick = ""; this.loadPresets();
      },
    };
  };
})();
