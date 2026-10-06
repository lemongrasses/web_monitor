/* North-up Leaflet map with heading marker and session trajectory. */
(function () {
  "use strict";

  const TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
  const TILE_PROBE = "https://tile.openstreetmap.org/0/0/0.png";
  const ARROW_SVG =
    '<svg viewBox="0 0 24 24" width="30" height="30" style="transition:transform .2s linear">' +
    '<path d="M12 2 L20 21 L12 16.5 L4 21 Z" fill="#0f4c81" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';

  class NavMap {
    /* opts.full: draw the whole session (older 1 Hz + recent 10 Hz); otherwise only the recent window. */
    constructor(elId, opts = {}) {
      this.full = !!opts.full;
      this.el = document.getElementById(elId);
      this.map = L.map(this.el, { zoomControl: true, attributionControl: true, worldCopyJump: false })
        .setView([23.7, 120.9], 7);
      this.olderLine = L.polyline([], { color: "#64748b", weight: 2, opacity: 0.75 }).addTo(this.map);
      this.recentLine = L.polyline([], { color: "#0f4c81", weight: 3.5, opacity: 0.9 }).addTo(this.map);
      this.marker = null;
      this.follow = true;
      this.rot = AIO.rotator();
      this.session = null;
      this.cursor = 0;
      this.older = [];
      this.map.on("dragstart", () => { this.follow = false; this._emitFollow(); });
      this._probeBasemap();
    }

    _probeBasemap() {
      const note = document.createElement("div");
      note.className = "map-note";
      note.textContent = "Loading basemap…";
      this.el.appendChild(note);
      const img = new Image();
      const timer = setTimeout(() => { img.src = ""; fail(); }, 4000);
      const fail = () => { note.textContent = "Basemap offline — position and trajectory still shown"; };
      img.onload = () => {
        clearTimeout(timer);
        L.tileLayer(TILE_URL, { maxZoom: 19, attribution: "© OpenStreetMap contributors" }).addTo(this.map);
        note.remove();
      };
      img.onerror = () => { clearTimeout(timer); fail(); };
      img.src = TILE_PROBE + "?t=" + Date.now();
    }

    _emitFollow() {
      this.el.dispatchEvent(new CustomEvent("follow-changed", { detail: this.follow }));
    }

    setFollow(on) {
      this.follow = on;
      if (on && this.marker) this.map.panTo(this.marker.getLatLng(), { animate: false });
      this._emitFollow();
    }

    setPosition(lat, lon, heading, stale) {
      if (!AIO.isNum(lat) || !AIO.isNum(lon) || (lat === 0 && lon === 0)) return;
      const ll = L.latLng(lat, lon);
      if (!this.marker) {
        const icon = L.divIcon({ className: "nav-marker", html: ARROW_SVG, iconSize: [30, 30], iconAnchor: [15, 15] });
        this.marker = L.marker(ll, { icon, keyboard: false, interactive: false }).addTo(this.map);
        this.map.setView(ll, 17, { animate: false });
      } else {
        this.marker.setLatLng(ll);
        if (this.follow && !this.map.getBounds().pad(-0.25).contains(ll)) {
          this.map.panTo(ll, { animate: false });
        }
      }
      const svg = this.marker.getElement() && this.marker.getElement().querySelector("svg");
      if (svg) {
        const r = this.rot(heading);
        if (r !== null) svg.style.transform = "rotate(" + r + "deg)";
        svg.style.opacity = stale ? 0.35 : 1;
      }
    }

    applyTrajectory(d) {
      if (d.reset || d.session !== this.session) {
        this.older = this.full ? d.older.slice() : [];
        this.session = d.session;
      } else if (this.full && d.older.length) {
        Array.prototype.push.apply(this.older, d.older);
      }
      this.cursor = d.cursor;
      this.recentLine.setLatLngs(d.recent);
      if (this.full) {
        const joined = d.recent.length ? this.older.concat([d.recent[0]]) : this.older;
        this.olderLine.setLatLngs(joined);
      }
    }

    startTrajectory(intervalMs = 2000) {
      return AIO.poll(
        () => "/api/trajectory?cursor=" + this.cursor + (this.session !== null ? "&session=" + this.session : ""),
        intervalMs, (d) => this.applyTrajectory(d));
    }

    fitTrajectory() {
      const pts = this.older.concat(this.recentLine.getLatLngs());
      if (pts.length > 1) { this.follow = false; this._emitFollow(); this.map.fitBounds(L.latLngBounds(pts).pad(0.1)); }
    }

    invalidate() { this.map.invalidateSize(); }
  }

  window.NavMap = NavMap;
})();
