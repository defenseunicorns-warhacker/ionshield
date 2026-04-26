/**
 * ElevationProfile.jsx
 * Compact SVG route elevation/risk profile that sits below the globe.
 *
 * Shows a horizontal bar divided into route segments, each segment
 * colored by the worst risk of its two endpoint waypoints.
 * When no decision is available, all segments render in neutral blue.
 *
 * Phase 1: uses flat (0 m) elevation — real terrain sampling via
 * Cesium.sampleTerrain requires a Cesium Ion token and an async call
 * which is left as a TODO for Phase 2.
 */

import { useMemo } from 'react';
import { getRiskStyle } from '../utils/riskColors.js';

const RISK_ORDER = ['NOMINAL', 'ELEVATED', 'CAUTION', 'DEGRADED', 'SEVERE'];

/** Haversine distance between two lat/lon points (km). */
function haversineDist(a, b) {
  const R  = 6371;
  const φ1 = (a.lat * Math.PI) / 180;
  const φ2 = (b.lat * Math.PI) / 180;
  const Δφ = ((b.lat - a.lat) * Math.PI) / 180;
  const Δλ = ((b.lon - a.lon) * Math.PI) / 180;
  const s  =
    Math.sin(Δφ / 2) ** 2 +
    Math.cos(φ1) * Math.cos(φ2) * Math.sin(Δλ / 2) ** 2;
  return R * 2 * Math.atan2(Math.sqrt(s), Math.sqrt(1 - s));
}

function worstRisk(rA, rB) {
  return RISK_ORDER.indexOf(rA) >= RISK_ORDER.indexOf(rB) ? rA : rB;
}

export default function ElevationProfile({ waypoints, decision }) {
  // ── ALL hooks must be called unconditionally (Rules of Hooks) ──────────────
  // Both useMemos are placed before any early return so the hook call count
  // is identical regardless of waypoints.length.

  const segments = useMemo(() => {
    if (waypoints.length < 2) return [];
    const decWps = decision?.waypoints ?? [];
    const segs   = [];
    let total    = 0;

    // First pass: compute distances
    const dists = waypoints.slice(0, -1).map((wp, i) =>
      haversineDist(wp, waypoints[i + 1]),
    );
    total = dists.reduce((s, d) => s + d, 0);
    if (total === 0) return [];

    // Second pass: build segment descriptors
    let cursor = 0;
    for (let i = 0; i < waypoints.length - 1; i++) {
      const d    = dists[i];
      const rA   = decWps[i]?.risk_level     ?? 'NOMINAL';
      const rB   = decWps[i + 1]?.risk_level ?? 'NOMINAL';
      const risk = decWps.length ? worstRisk(rA, rB) : null;
      const rs   = risk ? getRiskStyle(risk) : null;
      segs.push({
        x:      (cursor / total) * 100,
        width:  (d / total) * 100,
        color:  rs?.color ?? '#3b82f6',
        risk,
        dist:   d,
        fromWp: waypoints[i].name,
        toWp:   waypoints[i + 1].name,
      });
      cursor += d;
    }
    return segs;
  }, [waypoints, decision]);

  // Compute total route distance — handles the <2 case safely so this hook
  // is always called, never conditionally skipped by an early return above.
  const totalKm = useMemo(
    () => waypoints.length < 2
      ? 0
      : waypoints.slice(0, -1).reduce((s, wp, i) => s + haversineDist(wp, waypoints[i + 1]), 0),
    [waypoints],
  );

  // Early return AFTER all hooks
  if (waypoints.length < 2) return null;

  return (
    <div
      className="elev-profile"
      role="img"
      aria-label={`Route elevation profile, ${totalKm.toFixed(0)} km total`}
    >
      <div className="elev-header">
        <span className="elev-title">ROUTE PROFILE</span>
        <span className="elev-dist">{totalKm.toFixed(0)} km</span>
      </div>

      {/* Segment bar */}
      <div className="elev-bar" aria-hidden="true">
        {segments.map((seg, i) => (
          <div
            key={i}
            className="elev-seg"
            style={{
              left:    `${seg.x}%`,
              width:   `${seg.width}%`,
              background: seg.color,
              opacity: 0.82,
            }}
            title={`${seg.fromWp} → ${seg.toWp}: ${seg.risk ?? 'NO DATA'} · ${seg.dist.toFixed(0)} km`}
          />
        ))}
        {/* Waypoint tick marks */}
        {waypoints.map((wp, i) => {
          const pct = segments.slice(0, i).reduce((s, sg) => s + sg.width, 0);
          return (
            <div
              key={i}
              className="elev-tick"
              style={{ left: `${pct}%` }}
              aria-label={wp.name}
            />
          );
        })}
      </div>

      {/* Waypoint name labels (first, mid if many, last) */}
      <div className="elev-labels" aria-hidden="true">
        <span>{waypoints[0].name}</span>
        {waypoints.length > 2 && (
          <span style={{ position: 'absolute', left: '50%', transform: 'translateX(-50%)' }}>
            {waypoints[Math.floor(waypoints.length / 2)].name}
          </span>
        )}
        <span>{waypoints[waypoints.length - 1].name}</span>
      </div>

      {/* Risk legend when decision is available */}
      {decision && (
        <div className="elev-legend" aria-hidden="true">
          {['NOMINAL', 'ELEVATED', 'DEGRADED', 'SEVERE'].map(r => {
            const rs = getRiskStyle(r);
            const used = segments.some(s => s.risk === r);
            if (!used) return null;
            return (
              <span key={r} className="elev-legend-item">
                <span className="elev-legend-dot" style={{ background: rs.color }} />
                {r}
              </span>
            );
          })}
        </div>
      )}
    </div>
  );
}
