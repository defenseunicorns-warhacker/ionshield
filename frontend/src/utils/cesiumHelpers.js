/**
 * cesiumHelpers.js
 * Factory helpers that build Cesium entity/primitive property bags.
 * All Cesium imports are lazy — this module is only called inside Globe.jsx,
 * never during tests (which mock Cesium entirely).
 */

import * as Cesium from 'cesium';
import { getRiskStyle } from './riskColors.js';

// ── Colour utilities ──────────────────────────────────────────────────────────

/** Convert a CSS hex string + alpha (0–1) to a Cesium.Color. */
export function hexColor(hex, alpha = 1.0) {
  const r = parseInt(hex.slice(1, 3), 16) / 255;
  const g = parseInt(hex.slice(3, 5), 16) / 255;
  const b = parseInt(hex.slice(5, 7), 16) / 255;
  return new Cesium.Color(r, g, b, alpha);
}

// ── Route polyline ────────────────────────────────────────────────────────────

/**
 * Build the entity property bag for the route polyline.
 * Waypoints are raised 8 km above the surface so the line is visible
 * when the camera is zoomed in.
 *
 * @deprecated Prefer buildRiskSegments when a route decision is available.
 */
export function buildRoutePolyline(waypoints) {
  if (waypoints.length < 2) return null;
  const positions = waypoints.map(wp =>
    Cesium.Cartesian3.fromDegrees(wp.lon, wp.lat, 8000),
  );
  return {
    name: 'IonShield Route',
    polyline: {
      positions: new Cesium.ConstantProperty(positions),
      width: 3,
      material: new Cesium.PolylineGlowMaterialProperty({
        glowPower: 0.25,
        color: Cesium.Color.fromCssColorString('#60a5fa').withAlpha(0.9),
      }),
      clampToGround: false,
    },
  };
}

/**
 * Build per-segment colored polylines based on waypoint risk levels.
 *
 * When a route decision is available, each leg is colored by the worst
 * risk of its two endpoint waypoints (NOMINAL → ELEVATED → DEGRADED → SEVERE).
 * Falls back to a uniform blue line when no decision data is present.
 *
 * @param {Array<{lat, lon}>} waypoints
 * @param {Array<{risk_level: string}>} decisionWaypoints – from decision.waypoints
 * @returns {Array<Object>} Cesium entity property bags, one per segment
 */
export function buildRiskSegments(waypoints, decisionWaypoints) {
  if (waypoints.length < 2) return [];

  const RISK_ORDER = ['NOMINAL', 'ELEVATED', 'CAUTION', 'DEGRADED', 'SEVERE'];
  const hasDecision = decisionWaypoints && decisionWaypoints.length > 0;

  const segments = [];
  for (let i = 0; i < waypoints.length - 1; i++) {
    const wpA = waypoints[i];
    const wpB = waypoints[i + 1];

    let color;
    if (hasDecision) {
      const rA = decisionWaypoints[i]?.risk_level     ?? 'NOMINAL';
      const rB = decisionWaypoints[i + 1]?.risk_level ?? 'NOMINAL';
      const worstRisk = RISK_ORDER.indexOf(rA) >= RISK_ORDER.indexOf(rB) ? rA : rB;
      color = getRiskStyle(worstRisk).color;
    } else {
      color = '#60a5fa'; // default blue
    }

    segments.push({
      polyline: {
        positions: new Cesium.ConstantProperty([
          Cesium.Cartesian3.fromDegrees(wpA.lon, wpA.lat, 8_000),
          Cesium.Cartesian3.fromDegrees(wpB.lon, wpB.lat, 8_000),
        ]),
        width: 5,
        material: new Cesium.PolylineGlowMaterialProperty({
          glowPower: 0.35,
          color: Cesium.Color.fromCssColorString(color).withAlpha(0.95),
        }),
        clampToGround: false,
      },
    });
  }
  return segments;
}

// ── Waypoint marker ───────────────────────────────────────────────────────────

/**
 * Build a billboard + label entity for a single waypoint.
 * When a risk level is known (post-decision), the pin colour reflects it.
 */
export function buildWaypointEntity(wp, index, riskLevel = null) {
  const rs    = riskLevel ? getRiskStyle(riskLevel) : null;
  const color = rs ? rs.color : '#60a5fa';

  return {
    name: wp.name || `WP${index + 1}`,
    position: Cesium.Cartesian3.fromDegrees(wp.lon, wp.lat, 10000),
    billboard: {
      image:          buildPinSvg(index + 1, color),
      width:          32,
      height:         40,
      verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
    },
    label: {
      text:            wp.name || `WP${String(index + 1).padStart(2, '0')}`,
      font:            '11px system-ui, -apple-system, sans-serif',
      fillColor:       Cesium.Color.WHITE,
      outlineColor:    Cesium.Color.BLACK,
      outlineWidth:    2,
      style:           Cesium.LabelStyle.FILL_AND_OUTLINE,
      verticalOrigin:  Cesium.VerticalOrigin.TOP,
      pixelOffset:     new Cesium.Cartesian2(0, 4),
      disableDepthTestDistance: Number.POSITIVE_INFINITY,
    },
  };
}

/**
 * Thin vertical line from ground to the waypoint altitude —
 * helps the user see which surface point a marker refers to.
 */
export function buildAltitudeStick(wp) {
  return {
    polyline: {
      positions: new Cesium.ConstantProperty([
        Cesium.Cartesian3.fromDegrees(wp.lon, wp.lat, 0),
        Cesium.Cartesian3.fromDegrees(wp.lon, wp.lat, 10000),
      ]),
      width: 1,
      material: Cesium.Color.fromCssColorString('#334155').withAlpha(0.5),
    },
  };
}

/** Inline SVG pin rendered as a data-URI billboard image. */
function buildPinSvg(number, color) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="32" height="40" viewBox="0 0 32 40">
    <defs>
      <filter id="d"><feDropShadow dx="0" dy="2" stdDeviation="2"
        flood-color="#000" flood-opacity="0.55"/></filter>
    </defs>
    <path d="M16 0C7.16 0 0 7.16 0 16c0 8.84 16 24 16 24S32 24.84 32 16C32 7.16 24.84 0 16 0Z"
      fill="${color}" filter="url(#d)"/>
    <circle cx="16" cy="16" r="9" fill="rgba(0,0,0,0.35)"/>
    <text x="16" y="20.5" text-anchor="middle"
      font-family="system-ui,-apple-system,sans-serif"
      font-size="10" font-weight="700" fill="white">${number}</text>
  </svg>`;
  return `data:image/svg+xml;base64,${btoa(svg)}`;
}

// ── Data layer builders ───────────────────────────────────────────────────────

/**
 * Procedural ionosphere TEC visualisation derived from the Kp index.
 * Returns an array of latitude-band descriptor objects; Globe.jsx turns
 * each into a Cesium rectangle entity.
 *
 * Physics rationale:
 *   – The auroral oval expands equatorward as Kp rises (rough rule:
 *     equatorward boundary ≈ 75° − 5*Kp).
 *   – Higher Kp → stronger mid-latitude TEC disturbance.
 *   – The equatorial anomaly is always present and intensified by proton flux.
 */
export function buildTecBands(kp) {
  const kpClamped    = Math.max(0, Math.min(9, kp));
  const auroralLat   = Math.max(30, 75 - kpClamped * 5);
  const auroralAlpha = Math.min(0.55, 0.20 + kpClamped * 0.04);
  const auroralColor = kpClamped >= 5 ? '#EF4444' : kpClamped >= 3 ? '#F97316' : '#F59E0B';

  const bands = [
    // Auroral ovals (both hemispheres)
    { latMin: 60,          latMax: 90,  color: auroralColor, alpha: auroralAlpha },
    { latMin: -90,         latMax: -60, color: auroralColor, alpha: auroralAlpha },
    // Equatorial anomaly (always present)
    { latMin: -20,         latMax: 20,  color: '#10B981',    alpha: Math.min(0.20, 0.06 + kpClamped * 0.015) },
  ];

  // Mid-latitude disturbance belt only when Kp ≥ 4
  if (kpClamped >= 4) {
    const midAlpha = Math.min(0.30, 0.08 + kpClamped * 0.025);
    bands.push(
      { latMin: auroralLat, latMax: 60,           color: '#F59E0B', alpha: midAlpha },
      { latMin: -60,        latMax: -auroralLat,  color: '#F59E0B', alpha: midAlpha },
    );
  }

  return bands;
}
