/**
 * api.js
 * Thin fetch wrapper for the IonShield FastAPI backend.
 * Injects the X-API-Key header from localStorage when set.
 * All functions return parsed JSON or throw an error with .status set.
 */

// ── API key persistence ───────────────────────────────────────────────────────

export function getApiKey() {
  try { return localStorage.getItem('ionshield_api_key') || ''; }
  catch { return ''; }
}

export function setApiKey(key) {
  try { localStorage.setItem('ionshield_api_key', key || ''); }
  catch {}
}

// ── Core fetch ────────────────────────────────────────────────────────────────

function buildHeaders() {
  const h = { 'Content-Type': 'application/json' };
  const k = getApiKey();
  if (k) h['X-API-Key'] = k;
  return h;
}

export async function apiFetch(path, opts = {}) {
  const res = await fetch(path, { headers: buildHeaders(), ...opts });
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    const err = new Error(`HTTP ${res.status}: ${body}`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

// ── Typed endpoint wrappers ───────────────────────────────────────────────────

export const api = {
  /** GET /api/status — solar drivers, global risk, feed status */
  status: () => apiFetch('/api/status'),

  /** GET /overlay/risk.geojson — zones + installation markers */
  geojson: () => apiFetch('/overlay/risk.geojson'),

  /** GET /api/forecast — 72-hour Kp timeline + operational windows */
  forecast: () => apiFetch('/api/forecast'),

  /** POST /api/v2/route-decision — typed route risk recommendation */
  routeDecision: (waypoints, platform) =>
    apiFetch('/api/v2/route-decision', {
      method: 'POST',
      body: JSON.stringify({ waypoints, platform }),
    }),

  /** GET /api/v2/comms-decision — HF/SATCOM comms recommendation */
  commsDecision: (lat, lon, destLat = null, destLon = null) => {
    let url = `/api/v2/comms-decision?lat=${lat}&lon=${lon}`;
    if (destLat != null && destLon != null) url += `&dest_lat=${destLat}&dest_lon=${destLon}`;
    return apiFetch(url);
  },

  /** GET /api/v2/snapshots — paginated list of archived snapshots */
  snapshots: (limit = 20, offset = 0) =>
    apiFetch(`/api/v2/snapshots?limit=${limit}&offset=${offset}`),

  /** GET /api/v2/replay — replay comms decision at a past snapshot */
  replay: (lat, lon, snapshotId = null, at = null) => {
    let url = `/api/v2/replay?lat=${lat}&lon=${lon}`;
    if (snapshotId) url += `&snapshot_id=${snapshotId}`;
    else if (at)    url += `&at=${encodeURIComponent(at)}`;
    return apiFetch(url);
  },

  /** POST /api/v2/replay/route — replay route-decision at a past snapshot */
  replayRoute: (waypoints, platform, snapshotId = null, at = null) => {
    const body = { waypoints, platform };
    if (snapshotId != null) body.snapshot_id = snapshotId;
    else if (at != null)    body.at = at;
    return apiFetch('/api/v2/replay/route', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  },
};
