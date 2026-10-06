/* Shared helpers for the AIO dashboard pages (no build step). */
(function () {
  "use strict";

  const isNum = (v) => typeof v === "number" && isFinite(v);

  const fmt = {
    num(v, d = 2) { return isNum(v) ? v.toFixed(d) : "—"; },
    deg(v, d = 8) { return isNum(v) ? v.toFixed(d) : "—"; },
    hz(v) { return isNum(v) ? (v >= 10 ? v.toFixed(0) : v.toFixed(1)) + " Hz" : "—"; },
    age(s) {
      if (!isNum(s)) return "—";
      if (s < 1) return Math.round(s * 1000) + " ms";
      if (s < 120) return s.toFixed(1) + " s";
      return fmt.duration(s) + " ago";
    },
    duration(s) {
      if (!isNum(s)) return "—";
      s = Math.floor(s);
      const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600),
            m = Math.floor((s % 3600) / 60), sec = s % 60;
      const pad = (n) => String(n).padStart(2, "0");
      return (d ? d + "d " : "") + pad(h) + ":" + pad(m) + ":" + pad(sec);
    },
    bytes(b) {
      if (!isNum(b)) return "—";
      const u = ["B", "KB", "MB", "GB", "TB"];
      let i = 0;
      while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
      return (i ? b.toFixed(1) : b) + " " + u[i];
    },
    time(ts) {
      if (!isNum(ts)) return "—";
      return new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
    },
    datetime(ts) {
      if (!isNum(ts)) return "—";
      const d = new Date(ts * 1000);
      return d.toLocaleDateString() + " " + d.toLocaleTimeString([], { hour12: false });
    },
  };

  /* Poll a JSON endpoint without overlapping requests. cb(data) or onErr(err). */
  function poll(url, intervalMs, cb, onErr) {
    let busy = false, stopped = false;
    async function tick() {
      if (busy || stopped || document.hidden) return;
      busy = true;
      try {
        const r = await fetch(typeof url === "function" ? url() : url, { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        cb(await r.json());
      } catch (e) {
        if (onErr) onErr(e);
      } finally {
        busy = false;
      }
    }
    tick();
    const id = setInterval(tick, intervalMs);
    return () => { stopped = true; clearInterval(id); };
  }

  /* Shortest-angle rotation: keeps an accumulated angle so 359° -> 0° turns +1°, not -359°. */
  function rotator() {
    let acc = null;
    return function (deg) {
      if (!isNum(deg)) return acc;
      if (acc === null) { acc = deg; return acc; }
      const cur = ((acc % 360) + 360) % 360;
      const delta = ((deg - cur + 540) % 360) - 180;
      acc += delta;
      return acc;
    };
  }

  const LEVEL_TEXT = { healthy: "Normal", warning: "Warning", fault: "Fault", unknown: "Unknown" };

  window.AIO = { fmt, poll, rotator, isNum, LEVEL_TEXT };
})();
