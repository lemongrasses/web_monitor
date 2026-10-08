/* Maintenance tools: AIO NAV config editor and terminal.
   Nested inside maintPage(), so auth / needUnlock() / toolFetch() / services come from there. */
(function () {
  "use strict";
  const J = (x) => JSON.parse(JSON.stringify(x));
  const postJson = (url, body) => fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json", "X-Requested-With": "aio-dashboard" },
    body: JSON.stringify(body || {}) }).then((r) => r.json());

  // ======================================================================= config editor
  window.configPage = function () {
    return {
      files: [], fields: [], mode: null, file: null, text: "", raw: "", view: "form",
      orig: {}, form: {}, parseError: null, backups: [], preview: null,
      busy: false, msg: "", msgClass: "", loadError: "",

      async cfgInit() {
        try {
          const j = await (await fetch("/api/maint/aionav", { cache: "no-store" })).json();
          this.files = j.files; this.fields = j.fields;
          if (j.error) this.loadError = j.error;
          const pick = this.files.find((f) => f.active) || this.files[0];
          if (pick) await this.load(pick.mode);
          else if (!this.loadError) this.loadError = "No aio-nav-ros config file was found on this device.";
        } catch (e) { this.loadError = "Cannot load the config files: " + e.message; }
        window.addEventListener("beforeunload", (ev) => { if (this.dirty()) { ev.preventDefault(); ev.returnValue = ""; } });
      },
      key(f) { return f.path.join("."); },
      groups() { return [...new Set(this.fields.map((f) => f.group))]; },
      async load(mode) {
        const r = await fetch("/api/maint/aionav/" + encodeURIComponent(mode), { cache: "no-store" });
        const j = await r.json();
        if (!j.success) { this.loadError = j.summary; return; }
        this.loadError = "";
        this.mode = mode; this.file = j.file; this.text = j.text; this.raw = j.text;
        this.parseError = j.parse_error; this.backups = j.backups; this.preview = null;
        const vals = j.values || {};
        for (const f of this.fields) {
          const k = this.key(f);
          if (f.type === "list3" && vals[k] === undefined) vals[k] = null;
        }
        this.orig = J(vals);
        const form = J(vals);
        for (const f of this.fields) if (f.type === "list3" && form[this.key(f)] === null) form[this.key(f)] = [null, null, null];
        this.form = form;
        if (this.parseError) this.view = "raw";
        // the file list shows which file is in use and its time on disk
        this.files = this.files.map((f) => (f.mode === mode ? Object.assign({}, f, j.file) : f));
      },
      async pick(mode) {
        if (mode === this.mode) return;
        if (this.dirty() && !confirm("Discard the unsaved changes to " + this.file.name + "?")) return;
        this.msg = "";
        await this.load(mode);
      },
      changed(f) {
        const k = this.key(f);
        return this.orig[k] !== null && JSON.stringify(this.form[k]) !== JSON.stringify(this.orig[k]);
      },
      formChanges() {
        const out = {};
        for (const f of this.fields) if (this.changed(f)) out[this.key(f)] = this.form[this.key(f)];
        return out;
      },
      dirty() {
        return this.view === "raw" ? this.raw !== this.text : Object.keys(this.formChanges()).length > 0;
      },
      async setView(v) {
        if (v === this.view) return;
        if (v === "raw" && Object.keys(this.formChanges()).length) {
          // carry the form changes over into the text
          const j = await postJson("/api/maint/aionav/" + this.mode + "/preview", { form: this.formChanges() });
          if (!j.success && !j.text) { this.msg = j.summary || (j.errors || []).join("; "); this.msgClass = "bad"; return; }
          this.raw = j.text;
        }
        if (v === "form" && this.raw !== this.text) {
          this.msg = "Review and save (or discard) the changes in Edit file first."; this.msgClass = "bad";
          return;
        }
        if (v === "form") this.form = J(this.orig);
        this.msg = ""; this.preview = null; this.view = v;
      },
      payload() {
        const p = { base_mtime_ns: this.file.mtime_ns };
        if (this.view === "raw") p.text = this.raw; else p.form = this.formChanges();
        return p;
      },
      async review() {
        this.busy = "review"; this.msg = "";
        try {
          const j = await postJson("/api/maint/aionav/" + this.mode + "/preview", this.payload());
          if (j.errors === undefined) { this.preview = null; this.msg = j.summary; this.msgClass = "bad"; }
          else this.preview = j;
        } catch (e) { this.msg = "request failed: " + e.message; this.msgClass = "bad"; }
        this.busy = false;
        setTimeout(() => { const el = document.getElementById("rev-title"); if (el) el.scrollIntoView({ behavior: "smooth", block: "start" }); }, 50);
      },
      async save(restart) {
        this.busy = restart ? "restart" : "save"; this.msg = restart ? "Saving and restarting AIO NAV…" : "Saving…"; this.msgClass = "";
        const j = await this.toolFetch("/api/maint/aionav/" + this.mode + "/save",
          { method: "POST", body: JSON.stringify(Object.assign(this.payload(), { restart: !!restart })) });
        this.busy = false;
        this.msg = (j.summary || "") + ((j.warnings || []).length ? " — note: " + j.warnings.join("; ") : "");
        this.msgClass = j.success ? "ok" : "bad";
        if (j.success) { const keep = this.view; await this.load(this.mode); this.view = keep === "raw" ? "raw" : "form"; }
      },
      discard() {
        this.raw = this.text; this.form = J(this.orig);
        for (const f of this.fields) if (f.type === "list3" && this.form[this.key(f)] === null) this.form[this.key(f)] = [null, null, null];
        this.preview = null; this.msg = "";
      },
      async loadBackup(id) {
        if (this.dirty() && !confirm("Replace the unsaved changes with this earlier version?")) return;
        const j = await (await fetch("/api/maint/aionav/" + this.mode + "/backup/" + encodeURIComponent(id))).json();
        if (!j.success) { this.msg = j.summary; this.msgClass = "bad"; return; }
        this.raw = j.text; this.view = "raw"; this.preview = null;
        this.msg = "Earlier version " + id + " is in the editor. Review and save it to go back to it."; this.msgClass = "";
      },
      navRunning() {
        const k = ((this.d && this.d.config) || {}).nav_service;
        return !!(k && this.services[k] && this.services[k].state === "running");
      },
      saveHint() {
        if (!this.file) return "";
        if (!this.file.active) return "This file belongs to the " + this.file.label + " mode; it is used when that mode is.";
        return this.navRunning() ? "Save alone: AIO NAV keeps its current settings until it is restarted."
                                 : "It applies at the next Start.";
      },
      insertTab(ev) {
        const t = ev.target, s = t.selectionStart, e = t.selectionEnd;
        this.raw = this.raw.slice(0, s) + "  " + this.raw.slice(e);
        this.$nextTick(() => { t.selectionStart = t.selectionEnd = s + 2; });
      },
    };
  };

  // ======================================================================= terminal
  window.terminalPage = function () {
    // xterm objects stay outside Alpine's reactive proxy
    let term = null, fit = null, es = null, sid = null, queue = "", sending = Promise.resolve(), flushTimer = null;

    return {
      sessions: [], current: null, status: "", fontSize: 14, started: false,

      termInit() {
        const start = () => { if (!this.started && this.auth.unlocked) { this.started = true; this.setup(); } };
        this.$watch("auth.unlocked", (v) => { if (v) start(); });
        window.addEventListener("tools-locked", () => { this.detach(); this.started = false; this.sessions = []; this.current = null; if (term) { term.dispose(); term = null; } });
        this.loadAuth().then(start);
      },
      setup() {
        term = new Terminal({
          fontFamily: 'ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace', fontSize: this.fontSize,
          cursorBlink: true, scrollback: 5000, convertEol: false,
          theme: { background: "#0f1b2a", foreground: "#d9e2ec", cursor: "#9fb3c8", selectionBackground: "#334e68" },
        });
        fit = new FitAddon.FitAddon();
        term.loadAddon(fit);
        term.open(document.getElementById("term"));
        this.fitNow();
        term.onData((d) => this.send(d));
        window.addEventListener("resize", () => this.fitNow());
        this.refresh().then(() => { if (this.sessions.length) this.attach(this.sessions[this.sessions.length - 1].id); else this.openNew(); });
      },
      fitNow() {
        if (!term) return;
        try { fit.fit(); } catch (e) { return; }
        if (sid) postJson("/api/maint/term/" + sid + "/resize", { cols: term.cols, rows: term.rows });
      },
      async refresh() {
        const j = await this.toolFetch("/api/maint/term", { method: "GET", headers: {} });
        this.sessions = Array.isArray(j) ? j.filter((s) => s.alive) : [];
      },
      async openNew() {
        this.status = "Opening…";
        const j = await this.toolFetch("/api/maint/term", { method: "POST", body: JSON.stringify({ cols: term.cols, rows: term.rows }) });
        if (!j.success) { this.status = j.summary || "could not open a terminal"; return; }
        await this.refresh();
        this.attach(j.id);
      },
      detach() { if (es) { es.close(); es = null; } sid = null; },
      attach(id) {
        this.detach();
        sid = id; this.current = id;
        term.reset();
        es = new EventSource("/api/maint/term/" + id + "/stream?since=0");
        es.addEventListener("out", (e) => {
          const bin = atob(e.data), bytes = new Uint8Array(bin.length);
          for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
          term.write(bytes);
          this.status = "";
        });
        es.addEventListener("exit", (e) => {
          this.status = "The shell ended" + (e.data !== "" ? " (exit " + e.data + ")" : "") + ". Open a new terminal to continue.";
          es.close(); es = null; this.refresh();
        });
        es.onerror = () => { if (es && es.readyState !== EventSource.CLOSED) this.status = "Reconnecting…"; };
        this.fitNow();
        term.focus();
      },
      send(d) {
        if (!sid) return;
        queue += d;
        clearTimeout(flushTimer);
        flushTimer = setTimeout(() => {               // keystrokes in order, a few per request
          const data = queue, to = sid; queue = "";
          sending = sending.then(() => postJson("/api/maint/term/" + to + "/input", { data })
            .then((j) => { if (j.locked) this.needUnlock(); }).catch(() => { this.status = "Input not delivered"; }));
        }, 8);
      },
      async closeCurrent() {
        if (!this.current) return;
        if (!confirm("Close this terminal? Programs running in it are stopped.")) return;
        await this.toolFetch("/api/maint/term/" + this.current, { method: "DELETE", body: "{}" });
        this.detach();
        await this.refresh();
        if (this.sessions.length) this.attach(this.sessions[this.sessions.length - 1].id);
        else { this.current = null; term.reset(); this.status = "No terminal open."; }
      },
      rosEnv() {
        const m = ((this.d && this.d.config) || {}).ros_mode || {};
        if (m.domain_id === null || m.domain_id === undefined) return "";
        return "export ROS_DOMAIN_ID=" + m.domain_id + " ROS_LOCALHOST_ONLY=" + (m.localhost_only ? 1 : 0);
      },
      typeRosEnv() { const c = this.rosEnv(); if (c) { this.send(c + "\r"); term.focus(); } },
      zoom(step) {
        this.fontSize = Math.max(10, Math.min(22, this.fontSize + step));
        if (term) { term.options.fontSize = this.fontSize; this.fitNow(); }
      },
    };
  };
})();
