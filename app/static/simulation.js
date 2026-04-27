/**
 * IonShield Simulation Mode controller.
 *
 * Drives the Leaflet map + time slider + scenario picker. Talks to:
 *   GET  /api/v3/scenarios            — pre-defined catalog
 *   GET  /api/v3/scenarios/export     — time-indexed GeoJSON export
 *
 * Scenario data is fetched once per pick, then animated client-side via
 * the time slider — the API does the heavy lifting (fusion + impact),
 * the browser just colors the polygons.
 */

(function () {
  'use strict';

  const map = L.map('map', {
    minZoom: 1, maxZoom: 6, worldCopyJump: true, attributionControl: false,
  }).setView([20, 0], 2);

  L.tileLayer(
    'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    { maxZoom: 18 },
  ).addTo(map);

  // State
  let scenarios = [];
  let activeScenarioId = null;
  let activeCustomerId = '';        // empty = base catalog (all audiences)
  let activeProfile = null;
  let frames = [];                // [{ time_tag, features: [...] }]
  let timeIndex = 0;
  let layer = null;
  let playing = false;
  let playInterval = null;

  // Severity → color buckets (matches NOAA G-scale visual conventions)
  function gpsColor(m) {
    if (m >= 10) return '#dc2626';
    if (m >= 6) return '#ef4444';
    if (m >= 4) return '#f59e0b';
    if (m >= 2.5) return '#fbbf24';
    return '#22c55e';
  }
  function hfColor(db) {
    if (db >= 30) return '#dc2626';
    if (db >= 20) return '#ef4444';
    if (db >= 10) return '#f59e0b';
    if (db >= 5) return '#fbbf24';
    return '#22c55e';
  }
  function severityClass(kp) {
    if (kp >= 7) return 'v-red';
    if (kp >= 5) return 'v-amber';
    return 'v-green';
  }

  // ── Scenario catalog ──────────────────────────────────────────────────────

  async function loadCustomers() {
    try {
      const r = await fetch('/api/v3/customers');
      const data = await r.json();
      const picker = document.getElementById('customer-picker');
      (data.customers || []).forEach((c) => {
        const opt = document.createElement('option');
        opt.value = c.id;
        opt.textContent = c.title;
        picker.appendChild(opt);
      });
      picker.addEventListener('change', (e) => {
        activeCustomerId = e.target.value || '';
        loadScenarios();
      });
    } catch (e) {
      console.warn('customers endpoint unavailable', e);
    }
  }

  async function loadScenarios() {
    const url = activeCustomerId
      ? `/api/v3/scenarios/customer/${encodeURIComponent(activeCustomerId)}`
      : '/api/v3/scenarios';
    const r = await fetch(url);
    const data = await r.json();
    scenarios = data.scenarios || [];
    activeProfile = data.profile || null;
    applyBranding();
    renderScenarioBar();
    // Auto-load the first concrete (non-live) scenario so the user doesn't
    // arrive at an empty map. They can switch with one click.
    const first = scenarios.find(s => s.start && !String(s.start).startsWith('live'));
    if (first && !activeScenarioId) {
      pickScenario(first.id);
    }
  }

  function applyBranding() {
    const title = document.getElementById('sim-title');
    if (activeProfile && activeProfile.branding) {
      const b = activeProfile.branding;
      if (b.accent_color) {
        title.style.color = b.accent_color;
        document.documentElement.style.setProperty('--ion-accent', b.accent_color);
      }
    } else {
      title.style.color = '';
    }
  }

  function renderScenarioBar() {
    const bar = document.getElementById('scenario-bar');
    bar.innerHTML = '';
    scenarios.forEach((s) => {
      const card = document.createElement('div');
      card.className = 'scenario-card' + (s.id === activeScenarioId ? ' active' : '');
      card.innerHTML = `
        <div class="title">${s.title}</div>
        <div class="tagline">${s.tagline}</div>
        <div class="tags">
          ${(s.tags || []).map(t => `<span class="tag ${t}">${t}</span>`).join('')}
        </div>`;
      card.addEventListener('click', () => pickScenario(s.id));
      bar.appendChild(card);
    });
  }

  // ── Scenario load & animation ─────────────────────────────────────────────

  async function pickScenario(id) {
    const sc = scenarios.find(s => s.id === id);
    if (!sc) return;
    activeScenarioId = id;
    renderScenarioBar();
    renderDetail(sc);

    document.getElementById('loader').style.display = '';
    try {
      let fc;
      // Prefer the precomputed static asset (B3) — zero DB roundtrip,
      // CDN-cacheable, identical between page loads. Fall back to the
      // live export endpoint when the static file isn't there yet.
      if (sc.precomputed && sc.precomputed.geojson_url) {
        const r = await fetch(sc.precomputed.geojson_url);
        if (r.ok) {
          fc = await r.json();
        }
      }
      if (!fc) {
        const range = resolveTimeRange(sc);
        const url = '/api/v3/scenarios/export'
          + `?start=${encodeURIComponent(range.start)}`
          + `&end=${encodeURIComponent(range.end)}`
          + `&fmt=geojson&geometry=polygon`
          + `&step_seconds=${sc.step_seconds || 3600}`
          + `&max_snapshots=200`;
        const r = await fetch(url);
        fc = await r.json();
      }
      indexFrames(fc);
      bindSlider();
      drawFrame(0);
    } catch (e) {
      console.error('Scenario load failed', e);
    } finally {
      document.getElementById('loader').style.display = 'none';
    }
  }

  function resolveTimeRange(sc) {
    if (sc.start === 'live-7d' && sc.end === 'now') {
      const end = new Date();
      const start = new Date(end.getTime() - 7 * 86400 * 1000);
      return { start: start.toISOString(), end: end.toISOString() };
    }
    return { start: sc.start, end: sc.end };
  }

  function indexFrames(fc) {
    // Group features by time_tag
    const byTime = new Map();
    (fc.features || []).forEach((f) => {
      const t = f.properties && f.properties.time_tag;
      if (!t) return;
      if (!byTime.has(t)) byTime.set(t, []);
      byTime.get(t).push(f);
    });
    frames = Array.from(byTime.entries())
      .sort((a, b) => (a[0] < b[0] ? -1 : 1))
      .map(([time_tag, features]) => ({ time_tag, features }));
    timeIndex = 0;
  }

  function bindSlider() {
    const slider = document.getElementById('time-slider');
    slider.disabled = frames.length === 0;
    slider.min = 0;
    slider.max = Math.max(0, frames.length - 1);
    slider.value = 0;
    document.getElementById('play-btn').disabled = frames.length === 0;
  }

  function drawFrame(i) {
    if (i < 0 || i >= frames.length) return;
    timeIndex = i;
    const frame = frames[i];

    if (layer) { map.removeLayer(layer); layer = null; }
    layer = L.geoJSON({ type: 'FeatureCollection', features: frame.features }, {
      style: (feature) => ({
        color: '#0a0e1a', weight: 0.4,
        fillColor: hfColor(feature.properties.hf_absorption_db),
        fillOpacity: 0.55,
      }),
      pointToLayer: (feat, latlng) => {
        // Point-geometry features (used by lighter scenarios like
        // halloween-2003) render as circle markers colored by HF severity.
        return L.circleMarker(latlng, {
          radius: 6,
          color: '#0a0e1a',
          weight: 0.5,
          fillColor: hfColor(feat.properties.hf_absorption_db),
          fillOpacity: 0.75,
        });
      },
      onEachFeature: (feat, lyr) => {
        const p = feat.properties;
        lyr.bindTooltip(
          `<b>${p.region_id}</b><br>`
          + `Kp ${p.kp.toFixed(1)} · TEC ${p.tec_tecu.toFixed(1)}<br>`
          + `GPS L1 ${p.gps_l1_error_m.toFixed(1)} m · HF ${p.hf_absorption_db.toFixed(1)} dB<br>`
          + `Blackout p=${p.hf_blackout_probability.toFixed(2)}`,
          { sticky: true },
        );
      },
    }).addTo(map);

    document.getElementById('time-slider').value = i;
    document.getElementById('time-label').textContent = formatTime(frame.time_tag);

    // Update aside stats with frame-wide aggregates
    let kp = 0, gpsMax = 0, hfMax = 0, satMax = 0;
    frame.features.forEach((f) => {
      const p = f.properties;
      if (p.kp > kp) kp = p.kp;
      if (p.gps_l1_error_m > gpsMax) gpsMax = p.gps_l1_error_m;
      if (p.hf_absorption_db > hfMax) hfMax = p.hf_absorption_db;
      if (p.satcom_l_fade_db > satMax) satMax = p.satcom_l_fade_db;
    });
    const kpEl = document.getElementById('stat-kp');
    kpEl.textContent = kp.toFixed(1);
    kpEl.className = 'stat-val ' + severityClass(kp);
    document.getElementById('stat-gps').textContent = gpsMax.toFixed(1);
    document.getElementById('stat-hf').textContent = hfMax.toFixed(1);
    document.getElementById('stat-sat').textContent = satMax.toFixed(1);
  }

  function renderDetail(sc) {
    document.getElementById('detail-title').textContent = sc.title;
    document.getElementById('detail-summary').textContent = sc.summary;
    const slot = document.getElementById('video-slot');
    if (slot) {
      if (sc.video_url) {
        slot.outerHTML = `<video controls poster="" src="${sc.video_url}" style="width:100%;border-radius:6px;margin-bottom:14px;"></video>`;
      } else {
        slot.style.display = 'none';
      }
    }
    // B3 caveat 4: per-scenario download buttons for Earth Studio operators
    const downloads = document.getElementById('downloads');
    const pc = sc.precomputed;
    if (pc && (pc.kmz_url || pc.keyframes_url || pc.geojson_url)) {
      downloads.style.display = 'flex';
      const set = (id, url) => {
        const el = document.getElementById(id);
        if (url) { el.href = url; el.style.display = ''; }
        else { el.style.display = 'none'; }
      };
      set('dl-kmz', pc.kmz_url);
      set('dl-csv', pc.keyframes_url);
      set('dl-gj', pc.geojson_url);
    } else {
      downloads.style.display = 'none';
    }
    const hi = document.getElementById('highlights');
    hi.innerHTML = '';
    if (sc.highlights && sc.highlights.length) {
      const h2 = document.createElement('h2');
      h2.textContent = 'Highlights';
      hi.appendChild(h2);
      sc.highlights.forEach((m) => {
        const div = document.createElement('div');
        div.style.cssText = 'font-size:12px; color:#94a3b8; margin-top:6px; cursor:pointer;';
        div.innerHTML = `<span style="color:#38bdf8;">${formatTime(m.time)}</span> — ${m.label}`;
        div.addEventListener('click', () => seekToTime(m.time));
        hi.appendChild(div);
      });
    }
  }

  function seekToTime(iso) {
    if (!frames.length) return;
    let best = 0;
    let bestDiff = Infinity;
    frames.forEach((f, i) => {
      const diff = Math.abs(new Date(f.time_tag) - new Date(iso));
      if (diff < bestDiff) { bestDiff = diff; best = i; }
    });
    drawFrame(best);
  }

  function formatTime(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toISOString().replace('T', ' ').slice(0, 19) + 'Z';
  }

  // ── Playback ──────────────────────────────────────────────────────────────

  document.getElementById('play-btn').addEventListener('click', togglePlay);
  document.getElementById('time-slider').addEventListener('input', (e) => {
    drawFrame(parseInt(e.target.value, 10));
  });

  function togglePlay() {
    playing = !playing;
    const btn = document.getElementById('play-btn');
    btn.textContent = playing ? '⏸ Pause' : '▶ Play';
    if (playing) {
      playInterval = setInterval(() => {
        const next = (timeIndex + 1) % frames.length;
        drawFrame(next);
        if (next === 0) togglePlay();
      }, 600);
    } else {
      clearInterval(playInterval);
      playInterval = null;
    }
  }

  // ── Boot ──────────────────────────────────────────────────────────────────

  loadCustomers();
  loadScenarios();
})();
