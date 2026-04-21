/**
 * IonShield Dashboard — app.js
 *
 * Handles:
 *   - Leaflet map init, GeoJSON zone overlays, installation markers
 *   - Header status bar (Kp, Bz, X-ray, wind, proton, 24h forecast mini)
 *   - Location Risk panel (click map or enter coordinates)
 *   - Route Planner panel (click map or enter waypoints)
 *   - Forecast panel (72h timeline, operational windows, outlook summary)
 *   - Auto-refresh every 5 minutes with countdown
 */

'use strict';

// ── Configuration ─────────────────────────────────────────────────────────────
const CONFIG = {
  API_BASE:        '',      // empty = same origin
  API_KEY:         '',      // set if API_KEY env var is configured on the backend
  REFRESH_SECONDS: 300,
  MAP_CENTER:      [20, 10],
  MAP_ZOOM:        2,
};

// ── Risk level palette ────────────────────────────────────────────────────────
const RISK = {
  NOMINAL:  { color: '#10B981', bg: 'rgba(16,185,129,0.12)',  border: 'rgba(16,185,129,0.35)', text: '#10B981' },
  ELEVATED: { color: '#F59E0B', bg: 'rgba(245,158,11,0.12)', border: 'rgba(245,158,11,0.35)', text: '#F59E0B' },
  DEGRADED: { color: '#F97316', bg: 'rgba(249,115,22,0.12)', border: 'rgba(249,115,22,0.35)', text: '#F97316' },
  SEVERE:   { color: '#EF4444', bg: 'rgba(239,68,68,0.12)',  border: 'rgba(239,68,68,0.35)',  text: '#EF4444' },
};

function riskStyle(level) { return RISK[level] || RISK.NOMINAL; }

// ── State ─────────────────────────────────────────────────────────────────────
const state = {
  map:              null,
  zonesLayer:       null,
  basesLayer:       null,
  locationMarker:   null,
  locationMarkers:  [],    // configured-location markers (from /api/locations)
  routeMarkers:     [],
  routePolyline:    null,
  waypoints:        [],
  activeTab:        'location',
  clickMode:        null,
  refreshTimer:     null,
  countdownTimer:   null,
  countdown:        CONFIG.REFRESH_SECONDS,
  cachedForecast:   null,
};

// ── API helpers ───────────────────────────────────────────────────────────────
function apiUrl(path)   { return CONFIG.API_BASE + path; }
function apiHeaders()   {
  const h = { 'Content-Type': 'application/json' };
  if (CONFIG.API_KEY) h['X-API-Key'] = CONFIG.API_KEY;
  return h;
}

async function apiFetch(path, options = {}) {
  const res = await fetch(apiUrl(path), { headers: apiHeaders(), ...options });
  if (!res.ok) { const b = await res.text(); throw new Error(`HTTP ${res.status}: ${b}`); }
  return res.json();
}

// ── Map ───────────────────────────────────────────────────────────────────────
function initMap() {
  state.map = L.map('map', { center: CONFIG.MAP_CENTER, zoom: CONFIG.MAP_ZOOM });

  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> © <a href="https://carto.com/">CARTO</a>',
    subdomains: 'abcd', maxZoom: 19,
  }).addTo(state.map);

  state.map.on('click', onMapClick);
}

function onMapClick(e) {
  const lat = +e.latlng.lat.toFixed(4);
  const lon = +e.latlng.lng.toFixed(4);

  if (state.clickMode === 'location') {
    document.getElementById('loc-lat').value = lat;
    document.getElementById('loc-lon').value = lon;
    deactivateClickMode();
    assessLocation();
  } else if (state.clickMode === 'route') {
    const name = `WP${(state.waypoints.length + 1).toString().padStart(2, '0')}`;
    pushWaypoint({ lat, lon, name });
    deactivateClickMode();
  }
}

function updateMap(geojson) {
  if (state.zonesLayer) state.map.removeLayer(state.zonesLayer);
  if (state.basesLayer) state.map.removeLayer(state.basesLayer);

  const zones = { type: 'FeatureCollection', features: geojson.features.filter(f => f.properties.feature_type === 'zone') };
  const bases = { type: 'FeatureCollection', features: geojson.features.filter(f => f.properties.feature_type === 'installation') };

  state.zonesLayer = L.geoJSON(zones, {
    style: f => {
      const rs = riskStyle(f.properties.risk_level);
      return { fillColor: rs.color, fillOpacity: 0.10, color: rs.color, weight: 0.8, opacity: 0.5 };
    },
    onEachFeature: (feature, layer) => {
      layer.on('click', () => {
        layer.bindPopup(buildZonePopup(feature.properties, riskStyle(feature.properties.risk_level)), { maxWidth: 300 }).openPopup();
      });
    },
  }).addTo(state.map);

  state.basesLayer = L.geoJSON(bases, {
    pointToLayer: (feature, latlng) => {
      const rs = riskStyle(feature.properties.risk_level);
      return L.circleMarker(latlng, { radius: 7, fillColor: rs.color, fillOpacity: 0.9, color: '#0f172a', weight: 1.5 });
    },
    onEachFeature: (feature, layer) => {
      const p = feature.properties;
      layer.bindPopup(buildBasePopup(p, riskStyle(p.risk_level)), { maxWidth: 320 });
      layer.bindTooltip(`<b>${p.name}</b><br>${p.risk_level} · ${p.gps_error_m} m GPS`, { className: 'ion-tooltip' });
    },
  }).addTo(state.map);
}

// ── Popup builders ────────────────────────────────────────────────────────────
function riskBadgeHtml(level, score) {
  const rs = riskStyle(level);
  return `<span style="background:${rs.color};color:#fff;padding:2px 10px;border-radius:4px;font-size:11px;font-weight:700;letter-spacing:.07em">${level}</span>${score != null ? `<span style="color:#94a3b8;font-size:11px;margin-left:6px">${score}/99</span>` : ''}`;
}

function watchNoteHtml(notes) {
  if (!notes || !notes.length) return '';
  return `<div style="margin:8px 0">${notes.map(n => `<div style="color:#fbbf24;font-size:11px;padding:3px 0;border-bottom:1px solid #334155">⚠ ${n}</div>`).join('')}</div>`;
}

function metricRow(label, value, sub) {
  return `<tr><td style="color:#94a3b8;font-size:11px;padding:3px 4px 3px 0">${label}</td><td style="color:#e2e8f0;font-size:12px;font-weight:600;text-align:right;padding:3px 0">${value}${sub ? `<span style="color:#64748b;font-size:10px;margin-left:4px">${sub}</span>` : ''}</td></tr>`;
}

function buildZonePopup(p, rs) {
  return `<div style="font-family:system-ui,sans-serif;padding:4px 2px;min-width:240px">
    <div style="font-size:13px;font-weight:700;color:#f1f5f9;margin-bottom:6px">${p.name}</div>
    <div style="margin-bottom:8px">${riskBadgeHtml(p.risk_level, p.risk_score)}</div>
    <table style="width:100%;border-collapse:collapse">
      ${metricRow('GPS Error', `${p.gps_error_m} m`)}
      ${metricRow('HF Blackout', `${Math.round(p.hf_blackout_prob * 100)}%`)}
      ${metricRow('SATCOM Fade', `${p.satcom_fade_db} dB`)}
      ${metricRow('S4 Scint.', p.s4_index)}
      ${p.pca_active ? metricRow('PCA', 'ACTIVE', '(polar HF blackout)') : ''}
    </table>
    <div style="color:#94a3b8;font-size:10px;margin-top:8px;font-style:italic">${p.recommendation}</div>
  </div>`;
}

function buildBasePopup(p, rs) {
  return `<div style="font-family:system-ui,sans-serif;padding:4px 2px;min-width:280px">
    <div style="font-size:13px;font-weight:700;color:#f1f5f9;margin-bottom:2px">${p.name}</div>
    <div style="color:#64748b;font-size:10px;margin-bottom:8px">${p.zone.toUpperCase()} · Kp ${p.kp_current} · Bz ${p.bz_current_nt} nT</div>
    <div style="margin-bottom:6px">${riskBadgeHtml(p.risk_level, p.risk_score)}</div>
    ${watchNoteHtml(p.watch_notes)}
    <table style="width:100%;border-collapse:collapse;margin-top:6px">
      ${metricRow('GPS Error', `${p.gps_error_m} m`, `(${p.gps_error_range[0]}–${p.gps_error_range[1]} m)`)}
      ${metricRow('VTEC Est.', `${p.vtec_estimate_tecu} TECU`)}
      ${metricRow('HF Absorption', `${p.hf_absorption_db} dB`)}
      ${metricRow('HF Blackout', `${Math.round(p.hf_blackout_probability * 100)}%`)}
      ${p.pca_active ? metricRow('PCA', 'ACTIVE') : ''}
      ${metricRow('SATCOM Fade', `${p.satcom_fade_db} dB`)}
      ${metricRow('Radar Bias', `${p.radar_range_bias_lband_m} m`, '(L-band)')}
    </table>
    <div style="color:#94a3b8;font-size:10px;margin-top:10px;border-top:1px solid #334155;padding-top:8px;font-style:italic">${p.recommendation}</div>
  </div>`;
}

// ── Status bar ────────────────────────────────────────────────────────────────
function updateStatusBar(data) {
  const { solar_drivers: d, global_risk_level, data_age_seconds, feed_status } = data;
  const rs = riskStyle(global_risk_level);

  const badge = document.getElementById('global-risk-badge');
  badge.textContent = global_risk_level;
  badge.style.background = rs.color;

  const kpEl = document.getElementById('val-kp');
  kpEl.textContent  = d.kp_current.toFixed(1);
  kpEl.style.color  = d.kp_current >= 7 ? '#EF4444' : d.kp_current >= 5 ? '#F97316' : d.kp_current >= 4 ? '#F59E0B' : '#10B981';

  const bzEl = document.getElementById('val-bz');
  bzEl.textContent  = `${d.bz_nt > 0 ? '+' : ''}${d.bz_nt} nT`;
  bzEl.style.color  = d.bz_nt < -10 ? '#EF4444' : d.bz_nt < -5 ? '#F97316' : d.bz_nt < 0 ? '#F59E0B' : '#10B981';

  const xrEl = document.getElementById('val-xray');
  xrEl.textContent  = d.xray_class;
  xrEl.style.color  = d.xray_class === 'X' ? '#EF4444' : d.xray_class === 'M' ? '#F97316' : d.xray_class === 'C' ? '#F59E0B' : '#94a3b8';

  document.getElementById('val-wind').textContent = `${d.solar_wind_km_s} km/s`;

  const protonEl = document.getElementById('val-proton');
  const pf = d.proton_flux_10mev_pfu;
  protonEl.textContent = pf >= 1000 ? `${(pf / 1000).toFixed(1)}k pfu` : `${pf.toFixed(1)} pfu`;
  protonEl.style.color = pf >= 100 ? '#EF4444' : pf >= 10 ? '#F97316' : '#94a3b8';

  const ageEl = document.getElementById('data-age');
  const stale = data_age_seconds > 600;
  ageEl.textContent = `Data: ${formatAge(data_age_seconds)} old · ${feed_status ? liveFeeds(feed_status) : ''}`;
  ageEl.style.color = stale ? '#EF4444' : '#94a3b8';

  document.getElementById('header').style.borderBottomColor =
    global_risk_level === 'SEVERE'   ? '#EF4444' :
    global_risk_level === 'DEGRADED' ? '#F97316' : '';
}

function liveFeeds(status) {
  const total = Object.keys(status).length;
  const ok    = Object.values(status).filter(s => s === 'ok').length;
  return ok === total ? `${ok}/${total} feeds live` : `⚠ ${ok}/${total} feeds`;
}

function formatAge(seconds) {
  if (seconds >= 9000) return 'unknown';
  if (seconds < 60)   return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

// ── Risk card (sidebar) ───────────────────────────────────────────────────────
function buildRiskCard(a, context = {}) {
  const rs = riskStyle(a.risk_level);
  const watchHtml = (a.watch_notes && a.watch_notes.length)
    ? `<div class="ion-watch-notes">${a.watch_notes.map(n => `<div class="ion-watch-note">⚠ ${n}</div>`).join('')}</div>` : '';
  const gpsRange = a.gps_error_range
    ? `<div class="metric-sub">${a.gps_error_range[0]}–${a.gps_error_range[1]} m range</div>` : '';
  const ctxHtml = Object.keys(context).length
    ? `<div class="ion-context-row">${Object.entries(context).map(([k, v]) => `<span><span class="ion-ctx-label">${k}</span> ${v}</span>`).join(' · ')}</div>` : '';

  return `<div class="ion-risk-card" style="border-color:${rs.border};background:${rs.bg}">
    <div class="ion-card-header">
      <span class="ion-risk-badge" style="background:${rs.color}">${a.risk_level}</span>
      <span class="ion-score">${a.risk_score}<span class="ion-score-max">/99</span></span>
    </div>
    ${ctxHtml}
    <div class="ion-recommendation">${a.recommendation}</div>
    ${watchHtml}
    <div class="ion-metrics">
      <div class="ion-metric">
        <div class="ion-metric-label">GPS Error</div>
        <div class="ion-metric-value" style="color:${rs.text}">${a.gps_error_m} m</div>
        ${gpsRange}
        <div class="metric-sub">${a.asset_type}${a.iono_correction_active ? ' · iono-corr' : ''}</div>
      </div>
      <div class="ion-metric">
        <div class="ion-metric-label">VTEC Est.</div>
        <div class="ion-metric-value">${a.vtec_estimate_tecu} TECU</div>
      </div>
      <div class="ion-metric">
        <div class="ion-metric-label">HF Absorption</div>
        <div class="ion-metric-value">${a.hf_absorption_db} dB</div>
        <div class="metric-sub">Blackout: ${Math.round(a.hf_blackout_probability * 100)}%</div>
        ${a.pca_active ? '<div class="metric-sub" style="color:#EF4444">PCA ACTIVE</div>' : ''}
      </div>
      <div class="ion-metric">
        <div class="ion-metric-label">SATCOM Fade</div>
        <div class="ion-metric-value">${a.satcom_fade_db} dB</div>
        <div class="metric-sub">Outage: ${Math.round(a.satcom_outage_probability * 100)}%</div>
      </div>
      <div class="ion-metric">
        <div class="ion-metric-label">S4 Scintillation</div>
        <div class="ion-metric-value">${a.s4_index}</div>
      </div>
      <div class="ion-metric">
        <div class="ion-metric-label">Radar Bias (L-band)</div>
        <div class="ion-metric-value">${a.radar_range_bias_lband_m} m</div>
      </div>
    </div>
  </div>`;
}

// ── Location panel ────────────────────────────────────────────────────────────
async function assessLocation() {
  const lat       = parseFloat(document.getElementById('loc-lat').value);
  const lon       = parseFloat(document.getElementById('loc-lon').value);
  const assetType = document.getElementById('loc-asset').value;
  const resultEl  = document.getElementById('loc-result');

  if (isNaN(lat) || isNaN(lon))            { resultEl.innerHTML = `<div class="ion-error">Enter valid latitude and longitude.</div>`; return; }
  if (lat < -90 || lat > 90 || lon < -180 || lon > 180) { resultEl.innerHTML = `<div class="ion-error">Coordinates out of range.</div>`; return; }

  resultEl.innerHTML = `<div class="ion-loading">Assessing…</div>`;
  try {
    const data = await apiFetch(`/api/risk/location?lat=${lat}&lon=${lon}&asset_type=${assetType}`);
    const a    = data.assessment;
    const ctx  = { Zone: data.zone, Kp: data.kp_current, Bz: `${data.bz_current_nt} nT`, Wind: `${data.solar_wind_km_s} km/s` };
    resultEl.innerHTML = `<div class="ion-coords">${lat.toFixed(4)}°, ${lon.toFixed(4)}°</div>${buildRiskCard(a, ctx)}`;

    if (state.locationMarker) state.map.removeLayer(state.locationMarker);
    const rs = riskStyle(a.risk_level);
    state.locationMarker = L.circleMarker([lat, lon], { radius: 9, fillColor: rs.color, fillOpacity: 1, color: '#f1f5f9', weight: 2 })
      .addTo(state.map)
      .bindPopup(`<div style="font-family:system-ui,sans-serif;padding:4px 2px;min-width:240px">
        <div style="font-size:12px;color:#94a3b8;margin-bottom:4px">${lat.toFixed(4)}°, ${lon.toFixed(4)}° · ${data.zone}</div>
        <div style="margin-bottom:8px">${riskBadgeHtml(a.risk_level, a.risk_score)}</div>
        ${watchNoteHtml(a.watch_notes)}
        <table style="width:100%;border-collapse:collapse;margin-top:4px">
          ${metricRow('GPS Error', `${a.gps_error_m} m`)}
          ${metricRow('HF Absorption', `${a.hf_absorption_db} dB`)}
          ${metricRow('SATCOM Fade', `${a.satcom_fade_db} dB`)}
          ${metricRow('S4', a.s4_index)}
        </table>
      </div>`, { maxWidth: 300 });
    state.map.panTo([lat, lon]);
  } catch (err) {
    resultEl.innerHTML = `<div class="ion-error">Assessment failed: ${err.message}</div>`;
  }
}

// ── Route panel ───────────────────────────────────────────────────────────────
function pushWaypoint(wp) {
  state.waypoints.push(wp);
  renderWaypointList();
  updateRouteOnMap();
}

function addWaypoint() {
  const lat  = parseFloat(document.getElementById('wp-lat').value);
  const lon  = parseFloat(document.getElementById('wp-lon').value);
  const name = document.getElementById('wp-name').value.trim() ||
               `WP${(state.waypoints.length + 1).toString().padStart(2, '0')}`;
  if (isNaN(lat) || isNaN(lon) || lat < -90 || lat > 90 || lon < -180 || lon > 180) {
    document.getElementById('route-result').innerHTML = `<div class="ion-error">Invalid coordinates.</div>`;
    return;
  }
  pushWaypoint({ lat, lon, name });
  document.getElementById('wp-lat').value = '';
  document.getElementById('wp-lon').value = '';
  document.getElementById('wp-name').value = '';
}

function removeWaypoint(index) {
  state.waypoints.splice(index, 1);
  renderWaypointList();
  updateRouteOnMap();
}

function clearWaypoints() {
  state.waypoints = [];
  renderWaypointList();
  updateRouteOnMap();
  document.getElementById('route-result').innerHTML = '';
}

function renderWaypointList() {
  const list  = document.getElementById('wp-list');
  const empty = document.getElementById('wp-empty');
  document.getElementById('wp-count').textContent = state.waypoints.length;
  if (!state.waypoints.length) { list.innerHTML = ''; list.appendChild(empty); empty.style.display = ''; return; }
  empty.style.display = 'none';
  list.innerHTML = state.waypoints.map((wp, i) => `
    <div class="wp-row">
      <span class="wp-index">${(i + 1).toString().padStart(2, '0')}</span>
      <div class="wp-info"><div class="wp-name">${escHtml(wp.name)}</div><div class="wp-coords">${wp.lat.toFixed(4)}°, ${wp.lon.toFixed(4)}°</div></div>
      <button onclick="removeWaypoint(${i})" class="wp-remove">✕</button>
    </div>`).join('');
}

function updateRouteOnMap() {
  state.routeMarkers.forEach(m => state.map.removeLayer(m));
  state.routeMarkers = [];
  if (state.routePolyline) { state.map.removeLayer(state.routePolyline); state.routePolyline = null; }
  if (!state.waypoints.length) return;
  const latlngs = state.waypoints.map(wp => [wp.lat, wp.lon]);
  state.routePolyline = L.polyline(latlngs, { color: '#60a5fa', weight: 2, opacity: 0.6, dashArray: '5,5' }).addTo(state.map);
  state.waypoints.forEach((wp, i) => {
    const m = L.circleMarker([wp.lat, wp.lon], { radius: 6, fillColor: '#60a5fa', fillOpacity: 0.9, color: '#0f172a', weight: 1.5 })
      .addTo(state.map)
      .bindTooltip(`${(i+1).toString().padStart(2,'0')} ${escHtml(wp.name)}<br>${wp.lat.toFixed(4)}°, ${wp.lon.toFixed(4)}°`, { className: 'ion-tooltip' });
    state.routeMarkers.push(m);
  });
  state.map.fitBounds(state.routePolyline.getBounds(), { padding: [40, 40] });
}

async function analyzeRoute() {
  const resultEl  = document.getElementById('route-result');
  const assetType = document.getElementById('route-asset').value;
  if (!state.waypoints.length) { resultEl.innerHTML = `<div class="ion-error">Add at least one waypoint.</div>`; return; }
  resultEl.innerHTML = `<div class="ion-loading">Analyzing route…</div>`;
  try {
    const data = await apiFetch('/api/risk/route', { method: 'POST', body: JSON.stringify({ waypoints: state.waypoints, asset_type: assetType }) });
    renderRouteResult(data);
    updateRouteMarkersWithRisk(data.waypoints);
  } catch (err) {
    resultEl.innerHTML = `<div class="ion-error">Route analysis failed: ${err.message}</div>`;
  }
}

function renderRouteResult(data) {
  const s  = data.route_summary;
  const rs = riskStyle(s.max_risk_level);
  const rows = data.waypoints.map(wp => `
    <tr class="route-row">
      <td class="route-idx">${(wp.index+1).toString().padStart(2,'0')}</td>
      <td class="route-name">${escHtml(wp.name)}</td>
      <td><span class="route-badge" style="background:${riskStyle(wp.risk_level).color}">${wp.risk_level}</span></td>
      <td class="route-num">${wp.gps_error_m} m</td>
      <td class="route-num">${Math.round(wp.hf_blackout_prob * 100)}%</td>
    </tr>`).join('');
  document.getElementById('route-result').innerHTML = `
    <div class="ion-route-summary" style="border-color:${rs.border};background:${rs.bg}">
      <div class="ion-card-header">
        <span class="ion-risk-badge" style="background:${rs.color}">${s.max_risk_level}</span>
        <span style="color:#94a3b8;font-size:11px">${s.total_waypoints} waypoints</span>
      </div>
      <div class="ion-recommendation" style="margin-top:6px">${s.route_recommendation}</div>
      <div style="color:#64748b;font-size:10px;margin-top:4px">Worst: WP${(s.worst_waypoint_index+1).toString().padStart(2,'0')} · GPS ${s.worst_gps_error_m} m · ${s.asset_type}</div>
    </div>
    <div class="route-table-wrap">
      <table class="route-table">
        <thead><tr><th>#</th><th>Name</th><th>Risk</th><th>GPS Err</th><th>HF BKT</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function updateRouteMarkersWithRisk(waypointResults) {
  waypointResults.forEach((wp, i) => {
    if (!state.routeMarkers[i]) return;
    const rs = riskStyle(wp.risk_level);
    state.routeMarkers[i].setStyle({ fillColor: rs.color, fillOpacity: 0.95 });
    state.routeMarkers[i].bindPopup(`
      <div style="font-family:system-ui,sans-serif;min-width:200px;padding:4px 2px">
        <div style="font-weight:700;color:#f1f5f9;margin-bottom:4px">${escHtml(wp.name)}</div>
        <div>${riskBadgeHtml(wp.risk_level, wp.risk_score)}</div>
        <table style="width:100%;border-collapse:collapse;margin-top:8px">
          ${metricRow('GPS Error', `${wp.gps_error_m} m`)}
          ${metricRow('HF Absorption', `${wp.hf_absorption_db} dB`)}
          ${metricRow('SATCOM Fade', `${wp.satcom_fade_db} dB`)}
          ${metricRow('S4', wp.s4_index)}
          ${wp.pca_active ? metricRow('PCA', 'ACTIVE') : ''}
        </table>
        ${watchNoteHtml(wp.watch_notes)}
      </div>`, { maxWidth: 260 });
  });
}

// ── Configured locations ──────────────────────────────────────────────────────

function renderLocationMarkers(locations) {
  state.locationMarkers.forEach(m => state.map.removeLayer(m));
  state.locationMarkers = [];

  locations.forEach(loc => {
    if (!loc.assessment) return;   // not yet assessed (startup race)
    const a = loc.assessment.assessment;
    const rs = riskStyle(a.risk_level);
    const alertActive = loc.alert.active;

    const m = L.circleMarker([loc.lat, loc.lon], {
      radius:      alertActive ? 10 : 7,
      fillColor:   rs.color,
      fillOpacity: alertActive ? 1.0 : 0.80,
      color:       alertActive ? '#ffffff' : '#0f172a',
      weight:      alertActive ? 2.5 : 1.5,
    }).addTo(state.map);

    m.bindPopup(buildConfiguredLocationPopup(loc), { maxWidth: 320 });
    m.bindTooltip(
      `<b>${escHtml(loc.name)}</b><br>${a.risk_level} · GPS ±${a.gps_error_m} m` +
      (alertActive ? '<br><span style="color:#f87171">⚠ ALERT ACTIVE</span>' : ''),
      { className: 'ion-tooltip' },
    );
    state.locationMarkers.push(m);
  });
}

function buildConfiguredLocationPopup(loc) {
  const a  = loc.assessment ? loc.assessment.assessment : null;
  const rs = a ? riskStyle(a.risk_level) : riskStyle('NOMINAL');

  const alertHtml = loc.alert.active
    ? `<div style="background:rgba(239,68,68,.15);border:1px solid rgba(239,68,68,.4);
        color:#f87171;font-size:11px;font-weight:600;padding:4px 8px;border-radius:4px;
        margin:4px 0 6px">
        ⚠ ALERT ACTIVE since ${new Date(loc.alert.entered_at).toUTCString().replace(' GMT', 'Z')}
       </div>`
    : '';

  if (!a) {
    return `<div style="font-family:system-ui,sans-serif;padding:4px 2px;min-width:220px">
      <div style="font-weight:700;color:#f1f5f9;margin-bottom:4px">${escHtml(loc.name)}</div>
      <div style="color:#64748b;font-size:11px">Assessment pending…</div>
    </div>`;
  }

  return `<div style="font-family:system-ui,sans-serif;padding:4px 2px;min-width:280px">
    <div style="font-weight:700;color:#f1f5f9;font-size:13px;margin-bottom:2px">${escHtml(loc.name)}</div>
    <div style="color:#64748b;font-size:10px;margin-bottom:6px">
      ${loc.lat.toFixed(3)}°, ${loc.lon.toFixed(3)}° · ${loc.asset_type} · alert ≥${loc.alert_threshold}
    </div>
    ${alertHtml}
    <div style="margin-bottom:6px">${riskBadgeHtml(a.risk_level, a.risk_score)}</div>
    ${watchNoteHtml(a.watch_notes)}
    <table style="width:100%;border-collapse:collapse;margin-top:6px">
      ${metricRow('GPS Error',    `${a.gps_error_m} m`)}
      ${metricRow('HF Absorption',`${a.hf_absorption_db} dB`)}
      ${metricRow('Blackout Prob',`${Math.round(a.hf_blackout_probability * 100)}%`)}
      ${metricRow('SATCOM Fade',  `${a.satcom_fade_db} dB`)}
      ${a.pca_active ? metricRow('PCA', 'ACTIVE', '(polar HF blackout)') : ''}
    </table>
    <div style="color:#94a3b8;font-size:10px;margin-top:8px;border-top:1px solid #334155;
      padding-top:6px;font-style:italic">${escHtml(a.recommendation)}</div>
  </div>`;
}

function updateAlertBadge(locations) {
  const active = locations.filter(l => l.alert.active);
  const wrap   = document.getElementById('alert-badge-wrap');
  const badge  = document.getElementById('alert-badge');
  if (!wrap || !badge) return;
  if (active.length === 0) {
    wrap.classList.add('hidden');
  } else {
    wrap.classList.remove('hidden');
    badge.textContent = `${active.length} ACTIVE`;
  }
}

// ── Forecast panel ────────────────────────────────────────────────────────────
function updateForecastHeader(data) {
  const s = data.summary;
  const rs = riskStyle(s.max_risk_24h);
  const peakEl  = document.getElementById('val-forecast-peak');
  const badgeEl = document.getElementById('val-forecast-badge');
  if (peakEl)  { peakEl.textContent = `Kp ${s.max_kp_24h}`; peakEl.style.color = rs.color; }
  if (badgeEl) { badgeEl.textContent = s.max_risk_24h; badgeEl.style.background = rs.color; }
}

function buildTimelineSVG(timeline) {
  if (!timeline || !timeline.length) return '<div class="ion-loading">No forecast timeline data</div>';

  const W = 740, H = 110, BAR_TOP = 8, BAR_H = 68, LABEL_Y = H - 5;
  const times  = timeline.map(e => new Date(e.time).getTime());
  const tMin   = Math.min(...times), tMax = Math.max(...times);
  const tRange = tMax - tMin || 1;
  const nowMs  = Date.now();
  const nowX   = Math.max(0, Math.min(W, ((nowMs - tMin) / tRange) * W));
  const blockW = W / timeline.length;

  // Past-region overlay
  const pastBg = nowX > 0
    ? `<rect x="0" y="${BAR_TOP}" width="${nowX.toFixed(1)}" height="${BAR_H}" fill="#000" opacity="0.3"/>`
    : '';

  // Kp bars — observed at lower opacity, forecast at full
  const bars = timeline.map((e, i) => {
    const x    = i * blockW;
    const kpH  = Math.max(2, (e.kp / 9) * BAR_H);
    const y    = BAR_TOP + BAR_H - kpH;
    const col  = riskStyle(e.risk_level).color;
    const op   = e.type === 'observed' ? 0.40 : e.type === 'trend_estimate' ? 0.65 : 0.88;
    return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${Math.max(1, blockW - 0.5).toFixed(1)}" height="${kpH.toFixed(1)}" fill="${col}" opacity="${op}" rx="1"/>`;
  }).join('');

  // Threshold reference lines: Kp 4 (G1), 5 (G2), 7 (G3)
  const thLines = [
    { kp: 4, label: 'G1', col: '#F59E0B' },
    { kp: 5, label: 'G2', col: '#F97316' },
    { kp: 7, label: 'G3', col: '#EF4444' },
  ].map(t => {
    const y = BAR_TOP + BAR_H - (t.kp / 9) * BAR_H;
    return `<line x1="0" y1="${y.toFixed(1)}" x2="${W}" y2="${y.toFixed(1)}" stroke="${t.col}" stroke-width="0.5" stroke-dasharray="2,4" opacity="0.5"/>
            <text x="2" y="${(y - 2).toFixed(1)}" font-size="7" fill="${t.col}" opacity="0.8">${t.label}</text>`;
  }).join('');

  // NOW marker
  const nowLine = (nowX >= 0 && nowX <= W)
    ? `<line x1="${nowX.toFixed(1)}" y1="${BAR_TOP}" x2="${nowX.toFixed(1)}" y2="${BAR_TOP + BAR_H}" stroke="#60a5fa" stroke-width="1.5" opacity="0.9"/>
       <text x="${(nowX + 2).toFixed(1)}" y="${(BAR_TOP + 9).toFixed(1)}" font-size="7" fill="#60a5fa" font-weight="bold">NOW</text>`
    : '';

  // Time axis labels — show up to 8 evenly spaced
  const step  = Math.max(1, Math.floor(timeline.length / 7));
  const tLabels = timeline.map((e, i) => {
    if (i % step !== 0 && i !== timeline.length - 1) return '';
    const d = new Date(e.time);
    const lbl = `${d.getUTCDate()}/${d.getUTCMonth() + 1} ${d.getUTCHours().toString().padStart(2, '0')}Z`;
    return `<text x="${(i * blockW).toFixed(1)}" y="${LABEL_Y}" font-size="7" fill="#475569">${escHtml(lbl)}</text>`;
  }).join('');

  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:${H}px;display:block">
    ${pastBg}${thLines}${bars}${nowLine}${tLabels}
  </svg>`;
}

function buildWindowCard(w) {
  const rs         = riskStyle(w.risk_level);
  const isEstimate = w.source && w.source.includes('ESTIMATED');
  const gpsShort   = w.gps_impact.split('—')[0].trim();
  const hfShort    = w.hf_impact.split(' ')[0];
  return `<div class="forecast-window${isEstimate ? ' fw-estimate' : ''}" style="border-color:${rs.border}">
    <div class="fw-header">
      <span class="fw-label">${w.label}${isEstimate ? ' <span class="fw-est-tag">est</span>' : ''}</span>
      <span class="fw-badge" style="background:${rs.color}">${w.risk_level}</span>
    </div>
    <div class="fw-kp">Kp <span style="color:${rs.text};font-weight:700">${w.kp_forecast.toFixed(1)}</span></div>
    <div class="fw-impact" title="${escHtml(w.gps_impact)}">GPS: ${escHtml(gpsShort)}</div>
    <div class="fw-impact" title="${escHtml(w.hf_impact)}">HF: ${escHtml(hfShort)}</div>
  </div>`;
}

function renderForecastPanel(data) {
  const el = document.getElementById('panel-forecast');
  if (!el) return;

  const s  = data.summary;
  const rs = riskStyle(s.max_risk_72h);

  const stormBadge = s.storm_warning
    ? `<div class="storm-badge storm-warning">⚠ STORM WARNING — ${s.storm_level}</div>`
    : s.storm_watch
    ? `<div class="storm-badge storm-watch">⚡ STORM WATCH — ${s.storm_level || 'G1+'}</div>`
    : '';

  const trendBlock = data.kp_trend_1h != null ? `
    <div class="forecast-trend">
      <span class="forecast-trend-label">1H TREND</span>
      <span style="color:${riskStyle(data.kp_trend_1h_risk).color};font-weight:700">&nbsp;Kp ${data.kp_trend_1h}</span>
      <span class="fw-source">&nbsp;${data.kp_trend_1h_risk} (estimated)</span>
    </div>` : '';

  const peakUtc = s.peak_time
    ? new Date(s.peak_time).toUTCString().replace(':00 GMT', 'Z').replace(' GMT', 'Z')
    : 'Unknown';
  const hoursStr = s.hours_to_peak != null
    ? s.hours_to_peak < 0   ? '(in progress)'
    : s.hours_to_peak < 1   ? '(< 1h from now)'
    : `(${s.hours_to_peak.toFixed(0)}h from now)`
    : '';

  el.innerHTML = `
    <!-- Outlook summary card -->
    <div class="ion-risk-card" style="border-color:${rs.border};background:${rs.bg}">
      <div class="ion-card-header">
        <span class="ion-risk-badge" style="background:${rs.color}">72H PEAK ${s.max_risk_72h}</span>
        <span class="ion-score">${s.peak_kp}<span class="ion-score-max"> Kp</span></span>
      </div>
      ${stormBadge}
      <div class="ion-recommendation">${escHtml(s.outlook_text)}</div>
      ${trendBlock}
      <div style="color:#475569;font-size:10px;margin-top:8px;line-height:1.6">
        Peak: ${escHtml(peakUtc)} ${escHtml(hoursStr)}<br>
        24h max: Kp ${s.max_kp_24h} (${s.max_risk_24h})
      </div>
    </div>

    <!-- 72h Kp timeline -->
    <div>
      <div class="section-label">72-HOUR Kp TIMELINE</div>
      <div class="timeline-wrap">${buildTimelineSVG(data.timeline)}</div>
      <div class="timeline-legend">
        <span class="tl-leg" style="color:#10B981">■ NOMINAL</span>
        <span class="tl-leg" style="color:#F59E0B">■ ELEVATED</span>
        <span class="tl-leg" style="color:#F97316">■ DEGRADED</span>
        <span class="tl-leg" style="color:#EF4444">■ SEVERE</span>
        <span class="tl-leg" style="color:#64748b;opacity:.5">▪ past</span>
      </div>
    </div>

    <!-- Operational windows -->
    <div>
      <div class="section-label">OPERATIONAL WINDOWS</div>
      <div class="forecast-grid">${data.windows.map(buildWindowCard).join('')}</div>
    </div>

    <!-- Caveats -->
    <div class="forecast-caveat">
      Bz cannot be forecast beyond ~1h. Kp forecast skill degrades beyond 48h.
      Monitor <a href="https://www.swpc.noaa.gov/alerts/watch-warning-advisory" target="_blank" rel="noopener" style="color:#60a5fa">NOAA SWPC alerts</a> for CME watches.
    </div>

    <div style="color:#334155;font-size:10px;padding-top:4px;border-top:1px solid #1e293b">
      Source: ${escHtml(data.forecast_source)} &nbsp;·&nbsp;
      Generated: ${escHtml(new Date(data.generated).toUTCString().replace(' GMT', 'Z'))}
    </div>`;
}

// ── Tab switching ─────────────────────────────────────────────────────────────
function switchTab(tab) {
  state.activeTab = tab;
  deactivateClickMode();

  ['location', 'route', 'forecast'].forEach(t => {
    document.getElementById(`panel-${t}`).classList.toggle('hidden', t !== tab);
    const btn = document.getElementById(`tab-btn-${t}`);
    if (btn) { btn.className = `flex-1 py-3 text-sm font-medium tab-btn ${t === tab ? 'tab-active' : 'tab-inactive'}`; }
  });

  // Lazy-render forecast panel on first open or when cached data is available
  if (tab === 'forecast' && state.cachedForecast) {
    renderForecastPanel(state.cachedForecast);
    state.cachedForecast = null;
  }
}

// ── Map click mode ────────────────────────────────────────────────────────────
function activateClickMode(mode) {
  state.clickMode = mode;
  state.map.getContainer().style.cursor = 'crosshair';
  const hint = document.getElementById('map-hint');
  hint.classList.remove('hidden');
  hint.textContent = mode === 'location' ? 'Click the map to assess that location' : 'Click the map to add a waypoint';
  if (state.activeTab !== mode) switchTab(mode);
}

function deactivateClickMode() {
  state.clickMode = null;
  if (state.map) state.map.getContainer().style.cursor = '';
  document.getElementById('map-hint').classList.add('hidden');
}

// ── Auto-refresh ──────────────────────────────────────────────────────────────
function startCountdown() {
  if (state.countdownTimer) clearInterval(state.countdownTimer);
  state.countdown = CONFIG.REFRESH_SECONDS;

  state.countdownTimer = setInterval(() => {
    state.countdown -= 1;
    const m = Math.floor(state.countdown / 60);
    const s = state.countdown % 60;
    const el = document.getElementById('refresh-countdown');
    if (el) el.textContent = `Refresh in ${m}:${s.toString().padStart(2, '0')}`;
    if (state.countdown <= 0) { clearInterval(state.countdownTimer); doRefresh(); }
  }, 1000);
}

async function doRefresh() {
  const el = document.getElementById('refresh-countdown');
  if (el) el.textContent = 'Refreshing…';

  try {
    // Fetch locations separately so a missing locations.json never blocks the main refresh
    const [statusData, geojsonData, forecastData, locationsData] = await Promise.all([
      apiFetch('/api/status'),
      apiFetch('/overlay/risk.geojson'),
      apiFetch('/api/forecast'),
      apiFetch('/api/locations').catch(() => null),
    ]);

    updateStatusBar(statusData);
    updateMap(geojsonData);
    updateForecastHeader(forecastData);

    if (state.activeTab === 'forecast') {
      renderForecastPanel(forecastData);
    } else {
      state.cachedForecast = forecastData;
    }

    if (locationsData && locationsData.locations) {
      renderLocationMarkers(locationsData.locations);
      updateAlertBadge(locationsData.locations);
    }
  } catch (err) {
    console.error('Refresh failed:', err);
    const ageEl = document.getElementById('data-age');
    if (ageEl) ageEl.textContent = `⚠ Refresh failed: ${err.message}`;
  }

  startCountdown();
}

// ── Utility ───────────────────────────────────────────────────────────────────
function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── Init ──────────────────────────────────────────────────────────────────────
async function init() {
  initMap();
  await doRefresh();
}

document.addEventListener('DOMContentLoaded', init);
