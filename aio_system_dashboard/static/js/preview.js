/* View-only live previews for the maintenance Camera / LiDAR pages.
 * Frames are pulled one at a time (~2 Hz); the server only subscribes to the
 * ROS topic while a page keeps asking. */
(function () {
  "use strict";

  const INTERVAL_MS = 500;
  const RETRY_MS = 2000;

  function parseMeta(resp) {
    try { return JSON.parse(resp.headers.get("X-Preview-Meta") || "{}"); } catch (e) { return {}; }
  }

  /* Repeatedly fetch url; onFrame(resp) must consume the body. Pauses while the tab is hidden. */
  function pullLoop(url, onFrame, onStatus) {
    let stopped = false;
    async function step() {
      if (stopped) return;
      if (document.hidden) { setTimeout(step, INTERVAL_MS); return; }
      let wait = INTERVAL_MS;
      try {
        const r = await fetch(url, { cache: "no-store" });
        if (r.ok) {
          await onFrame(r, parseMeta(r));
        } else {
          let msg = "HTTP " + r.status;
          try { msg = (await r.json()).error || msg; } catch (e) { /* not JSON */ }
          onStatus(msg);
          wait = RETRY_MS;
        }
      } catch (e) {
        onStatus("connection lost");
        wait = RETRY_MS;
      }
      setTimeout(step, wait);
    }
    step();
    return () => { stopped = true; };
  }

  function setInfo(el, parts) {
    el.replaceChildren(...parts.filter(Boolean).map((t) => {
      const span = document.createElement("span");
      span.textContent = t;
      return span;
    }));
  }

  /* ---------------- camera ---------------- */
  function startImage(root) {
    const img = root.querySelector("img");
    const status = root.querySelector("[data-status]");
    const info = root.querySelector("[data-info]");
    let lastUrl = null, frames = 0, t0 = performance.now();
    pullLoop(root.dataset.url, async (r, meta) => {
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      img.src = url;
      if (lastUrl) URL.revokeObjectURL(lastUrl);
      lastUrl = url;
      status.hidden = true;
      frames++;
      const fps = frames / ((performance.now() - t0) / 1000);
      setInfo(info, [meta.topic, meta.size, meta.encoding || meta.source, meta.frame && "frame " + meta.frame,
        "age " + AIO.fmt.age(meta.age_s), fps.toFixed(1) + " frames/s shown"]);
    }, (msg) => { status.hidden = false; status.textContent = msg; });
  }

  /* ---------------- point cloud ---------------- */
  // Compact "turbo"-like ramp, t in [0,1].
  const RAMP = [[48, 18, 59], [70, 107, 227], [41, 187, 236], [49, 242, 153], [163, 253, 61],
                [237, 208, 58], [251, 128, 34], [208, 47, 5], [122, 4, 3]];
  function ramp(t) {
    t = Math.min(1, Math.max(0, t)) * (RAMP.length - 1);
    const i = Math.floor(t), f = t - i, a = RAMP[i], b = RAMP[Math.min(i + 1, RAMP.length - 1)];
    return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f];
  }

  class CloudView {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.pts = new Float32Array(0);
      this.yaw = 0;          // rad, rotation about z
      this.pitch = Math.PI / 2; // PI/2 = top view (looking down), 0 = side view
      this.scale = 8;        // px per metre
      this.colorBy = "height";
      this._bindInput();
      new ResizeObserver(() => this.resize()).observe(canvas);
      this.resize();
    }

    resize() {
      const r = this.canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      this.canvas.width = Math.max(1, Math.round(r.width * dpr));
      this.canvas.height = Math.max(1, Math.round(r.height * dpr));
      this.dpr = dpr;
      this.draw();
    }

    setView(mode) {
      if (mode === "top") { this.yaw = 0; this.pitch = Math.PI / 2; }
      else { this.yaw = -0.6; this.pitch = 0.5; }
      this.draw();
    }

    _bindInput() {
      let drag = null;
      this.canvas.addEventListener("pointerdown", (e) => { drag = { x: e.clientX, y: e.clientY }; this.canvas.setPointerCapture(e.pointerId); });
      this.canvas.addEventListener("pointerup", () => { drag = null; });
      this.canvas.addEventListener("pointermove", (e) => {
        if (!drag) return;
        this.yaw += (e.clientX - drag.x) * 0.008;
        this.pitch = Math.min(Math.PI / 2, Math.max(0.05, this.pitch + (e.clientY - drag.y) * 0.008));
        drag = { x: e.clientX, y: e.clientY };
        this.draw();
      });
      this.canvas.addEventListener("wheel", (e) => {
        e.preventDefault();
        this.scale = Math.min(200, Math.max(1, this.scale * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
        this.draw();
      }, { passive: false });
    }

    setPoints(pts) { this.pts = pts; this.draw(); }

    draw() {
      const { ctx, canvas } = this;
      const W = canvas.width, H = canvas.height, s = this.scale * this.dpr;
      const cx = W / 2, cy = H / 2;
      const cyaw = Math.cos(this.yaw), syaw = Math.sin(this.yaw);
      const sp = Math.sin(this.pitch), cp = Math.cos(this.pitch);
      ctx.fillStyle = "#0f1b2a";
      ctx.fillRect(0, 0, W, H);

      // Range rings on the ground plane (projected circles are ellipses), every 10 m.
      ctx.strokeStyle = "rgba(148,163,184,.25)";
      ctx.fillStyle = "rgba(148,163,184,.6)";
      ctx.lineWidth = this.dpr;
      ctx.font = 11 * this.dpr + "px sans-serif";
      for (let r = 10; r * s < Math.max(W, H); r += 10) {
        ctx.beginPath();
        ctx.ellipse(cx, cy, r * s, r * s * sp, 0, 0, 2 * Math.PI);
        ctx.stroke();
        ctx.fillText(r + " m", cx + 3, cy - r * s * sp - 3);
      }
      // Forward (+x) axis of the sensor.
      const fx = cx - (syaw) * 3 * s, fy = cy - (cyaw * sp) * 3 * s;
      ctx.strokeStyle = "#f0b429"; ctx.lineWidth = 2 * this.dpr;
      ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(fx, fy); ctx.stroke();

      const pts = this.pts, n = pts.length / 4;
      if (!n) return;
      // Colour range: height uses fixed band, intensity uses 2nd-98th percentile.
      let lo = -2, hi = 3;
      if (this.colorBy === "intensity") {
        const sample = [];
        for (let i = 0; i < n; i += Math.max(1, Math.floor(n / 2000))) sample.push(pts[i * 4 + 3]);
        sample.sort((a, b) => a - b);
        lo = sample[Math.floor(sample.length * 0.02)] || 0;
        hi = sample[Math.floor(sample.length * 0.98)] || 1;
        if (hi <= lo) hi = lo + 1;
      }
      const img = ctx.getImageData(0, 0, W, H);
      const data = img.data;
      const dot = this.dpr > 1.5 ? 2 : 1;
      for (let i = 0; i < n; i++) {
        const x = pts[i * 4], y = pts[i * 4 + 1], z = pts[i * 4 + 2];
        const x1 = x * cyaw - y * syaw, y1 = x * syaw + y * cyaw;
        const px = Math.round(cx - y1 * s);
        const py = Math.round(cy - (x1 * sp + z * cp) * s);
        if (px < 0 || py < 0 || px >= W - dot || py >= H - dot) continue;
        const v = this.colorBy === "intensity" ? pts[i * 4 + 3] : z;
        const c = ramp((v - lo) / (hi - lo));
        for (let dy = 0; dy <= dot - 1; dy++) {
          for (let dx = 0; dx <= dot - 1; dx++) {
            const o = ((py + dy) * W + px + dx) * 4;
            data[o] = c[0]; data[o + 1] = c[1]; data[o + 2] = c[2]; data[o + 3] = 255;
          }
        }
      }
      ctx.putImageData(img, 0, 0);
    }
  }

  function startCloud(root) {
    const view = new CloudView(root.querySelector("canvas"));
    const status = root.querySelector("[data-status]");
    const info = root.querySelector("[data-info]");
    root.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => view.setView(b.dataset.view)));
    const sel = root.querySelector("[data-color]");
    if (sel) sel.addEventListener("change", () => { view.colorBy = sel.value; view.draw(); });
    pullLoop(root.dataset.url, async (r, meta) => {
      const buf = await r.arrayBuffer();
      view.setPoints(new Float32Array(buf));
      status.hidden = true;
      setInfo(info, [meta.topic, (meta.count || 0).toLocaleString() + " of " + (meta.total || 0).toLocaleString() + " points",
        meta.frame && "frame " + meta.frame, "age " + AIO.fmt.age(meta.age_s)]);
    }, (msg) => { status.hidden = false; status.textContent = msg; });
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-preview=image]").forEach(startImage);
    document.querySelectorAll("[data-preview=pointcloud]").forEach(startCloud);
  });
})();
