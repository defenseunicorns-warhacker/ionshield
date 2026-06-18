/**
 * Panel.jsx
 * Right-side decision panel (~420 px).
 *
 * Sections (top → bottom):
 *   1. Layer toggles (TEC / PCA / Solar Wind)
 *   2. Waypoint builder (form, list, platform selector)
 *   3. Action buttons (Get Decision, Fly to Route, Click Map)
 *   4. Decision result (action, sentence, confidence, impacts, provenance)
 */

import { useState } from 'react';
import { useDecision }   from '../hooks/useDecision.js';
import MissionAssessment from './MissionAssessment.jsx';
import { getRiskStyle, getActionStyle, confScoreColor, confLabelText } from '../utils/riskColors.js';

// ── Layer Toggles ─────────────────────────────────────────────────────────────

function LayerToggles({ layers, onToggle }) {
  const entries = [
    { key: 'tec',       dot: '#F59E0B', status: layers.tec.kp != null   ? `Kp ${layers.tec.kp.toFixed(1)}` : 'no data' },
    { key: 'pca',       dot: '#EF4444', status: layers.pca.active       ? 'ACTIVE' : 'clear' },
    { key: 'solarWind', dot: '#60a5fa', status: layers.solarWind.speed != null ? `${layers.solarWind.speed.toFixed(0)} km/s` : 'no data' },
  ];

  return (
    <div className="panel-section">
      {/* Quick links — small, restrained pills matching the rest of the
          panel's density. Replaces the previous oversized cyan-gradient
          Simulation CTA. */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: '1fr 1fr',
          gap: 6,
          marginBottom: 14,
        }}
      >
        <a
          href="/simulation"
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            background: '#0a0e1a',
            color: '#e2e8f0',
            padding: '7px 10px',
            borderRadius: 6,
            border: '1px solid #1e293b',
            textDecoration: 'none',
            fontSize: 11.5,
            fontWeight: 600,
          }}
          title="Replay historical storms"
        >
          <span>📼 Simulation</span>
          <span style={{ fontSize: 11, color: '#64748b' }}>→</span>
        </a>
        <a
          href="/integrations"
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            background: '#0a0e1a',
            color: '#e2e8f0',
            padding: '7px 10px',
            borderRadius: 6,
            border: '1px solid #1e293b',
            textDecoration: 'none',
            fontSize: 11.5,
            fontWeight: 600,
          }}
          title="ATAK · Foundry · API"
        >
          <span>⊞ Integrations</span>
          <span style={{ fontSize: 11, color: '#64748b' }}>→</span>
        </a>
      </div>
      <div className="section-label">
        DATA LAYERS
        <span className="help-icon" title="Toggle visual overlays on the 3D globe. Values are derived from live NOAA data and are indicative, not precision measurements.">?</span>
      </div>
      {entries.map(({ key, dot, status }) => (
        <div
          key={key}
          className="layer-row"
          onClick={() => onToggle(key)}
          role="switch"
          aria-checked={layers[key].visible}
          tabIndex={0}
          onKeyDown={e => e.key === 'Enter' && onToggle(key)}
        >
          <div className={`layer-toggle-switch ${layers[key].visible ? 'on' : 'off'}`}>
            <div className="layer-toggle-knob" />
          </div>
          <div className="layer-dot" style={{ background: dot }} />
          <span className="layer-name">{layers[key].label}</span>
          <span className="layer-status" style={{ color: key === 'pca' && layers.pca.active ? 'var(--red)' : undefined }}>
            {status}
          </span>
        </div>
      ))}
    </div>
  );
}

// ── Waypoint List ─────────────────────────────────────────────────────────────

function WaypointList({ waypoints, decision, onRemove }) {
  const wpResults = decision?.waypoints || [];

  if (!waypoints.length) {
    return <div className="wp-empty">No waypoints yet</div>;
  }

  return (
    <div className="wp-list">
      {waypoints.map((wp, i) => {
        const result = wpResults[i];
        const rs     = result ? getRiskStyle(result.risk_level) : null;
        return (
          <div key={i} className="wp-row">
            <span className="wp-index">{String(i + 1).padStart(2, '0')}</span>
            {rs && (
              <span
                style={{ width: 6, height: 6, borderRadius: '50%', background: rs.color, flexShrink: 0 }}
                title={result.risk_level}
              />
            )}
            <div className="wp-info">
              <div className="wp-name">{wp.name}</div>
              <div className="wp-coords">{wp.lat.toFixed(4)}°, {wp.lon.toFixed(4)}°</div>
            </div>
            <button
              className="wp-remove"
              onClick={() => onRemove(i)}
              aria-label={`Remove waypoint ${wp.name}`}
            >✕</button>
          </div>
        );
      })}
    </div>
  );
}

// ── Confidence Bar ────────────────────────────────────────────────────────────

function ConfidenceSection({ confidence }) {
  const score   = confidence?.score ?? 0;
  const label   = confLabelText(confidence?.label);
  const color   = confScoreColor(score);
  const barW    = Math.round(score * 100);
  const drivers = confidence?.drivers || [];

  return (
    <div className="dec-block">
      <div className="conf-header">
        <span className="conf-label">
          CONFIDENCE
          <span className="help-icon" title="HIGH (≥0.75): fresh data, all feeds live. MEDIUM: minor gaps. LOW / VERY LOW: stale or missing feeds. Do not act on LOW confidence decisions without verifying current NOAA data.">?</span>
        </span>
        <span className="conf-score" style={{ color }}>{label} · {score.toFixed(2)}</span>
      </div>
      <div className="conf-bar-bg">
        <div className="conf-bar-fill" style={{ width: `${barW}%`, background: color }} />
      </div>
      <div>
        {drivers.map((dr, i) => {
          const sign  = dr.effect >= 0 ? '+' : '';
          const dcol  = dr.effect >= 0 ? 'var(--green)' : 'var(--red)';
          return (
            <div key={i} className="driver">
              <span className="driver-factor">{String(dr.factor).replace(/_/g, ' ')}</span>
              <span className="driver-effect" style={{ color: dcol }}>{sign}{dr.effect.toFixed(2)}</span>
              <span className="driver-detail">{dr.detail}</span>
            </div>
          );
        })}
        {typeof confidence?.data_completeness === 'number' && (
          <div className="driver" style={{ marginTop: 4, paddingTop: 4, borderTop: '1px solid var(--bg-3)' }}>
            <span className="driver-factor">data completeness</span>
            <span className="driver-effect" style={{ color: 'var(--text-2)' }}>
              {Math.round(confidence.data_completeness * 100)}%
            </span>
            <span className="driver-detail">
              {confidence.stale_penalty_applied ? 'stale penalty applied' : 'no stale penalty'}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Impacts ───────────────────────────────────────────────────────────────────

function ImpactsSection({ impacts }) {
  if (!impacts?.length) return null;
  return (
    <div className="dec-block">
      <div className="section-label">IMPACTS</div>
      {impacts.map((imp, i) => (
        <div key={i}>
          <div className="impact-row">
            <span className="impact-system">{imp.system}</span>
            <span className="impact-metric">{String(imp.metric).replace(/_/g, ' ')}</span>
            <span className="impact-value">{String(imp.value ?? '—')}</span>
          </div>
          <div className="impact-detail">{imp.detail}</div>
        </div>
      ))}
    </div>
  );
}

// ── Per-waypoint Table ────────────────────────────────────────────────────────

function WaypointTable({ waypoints }) {
  if (!waypoints?.length) return null;
  return (
    <div className="dec-block">
      <div className="section-label">PER-WAYPOINT RISK</div>
      <div className="wp-table-wrap">
        <table className="wp-table">
          <thead>
            <tr><th>#</th><th>Name</th><th>Risk</th><th>GPS Err</th><th>HF</th></tr>
          </thead>
          <tbody>
            {waypoints.map((wp, i) => {
              const rs = getRiskStyle(wp.risk_level || 'NOMINAL');
              return (
                <tr key={i}>
                  <td className="ri">{String(i + 1).padStart(2, '0')}</td>
                  <td className="rn">{wp.name || `WP${i + 1}`}</td>
                  <td>
                    <span style={{ background: rs.color, color: '#fff', fontSize: 8, fontWeight: 700, padding: '2px 5px', borderRadius: 3, letterSpacing: '.05em' }}>
                      {wp.risk_level}
                    </span>
                    {wp.pca_active && (
                      <span style={{ color: 'var(--red)', fontSize: 8, fontWeight: 700, marginLeft: 3 }}>PCA</span>
                    )}
                  </td>
                  <td className="rv">{wp.gps_error_m != null ? `${wp.gps_error_m} m` : '—'}</td>
                  <td className="rv" style={{ color: wp.hf_viable ? 'var(--green)' : 'var(--red)' }}>
                    {wp.hf_viable ? '✓' : '✗'}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Provenance ────────────────────────────────────────────────────────────────

function ProvenanceSection({ provenance }) {
  if (!provenance) return null;
  const hash    = provenance.input_hash || '—';
  const feedsOff = provenance.feeds_unavailable || [];

  return (
    <div className="dec-block">
      <details>
        <summary className="prov-details" style={{ fontSize: 9, fontWeight: 700, letterSpacing: '.09em', textTransform: 'uppercase', color: 'var(--text-3)', cursor: 'pointer', userSelect: 'none', display: 'flex', alignItems: 'center', gap: 5 }}>
          <span style={{ fontSize: 10 }}>▸</span>
          Provenance
          <span className="help-icon" title="Records the exact inputs used. The input_hash is a SHA-256 fingerprint — the same inputs always produce the same hash, enabling deterministic replay via /api/v2/replay.">?</span>
        </summary>
        <div style={{ paddingTop: 6 }}>
          {[
            ['Model',        provenance.model_version],
            ['Hash',         hash.slice(0, 22) + '…'],
            ['Computed',     provenance.computed_at ? new Date(provenance.computed_at).toUTCString().replace(' GMT', 'Z') : '—'],
            ['Observations', (provenance.observations_used || []).join(', ')],
            ...(provenance.forecasts_used?.length ? [['Forecasts', provenance.forecasts_used.join(', ')]] : []),
          ].map(([k, v]) => (
            <div key={k} className="prov-row">
              <span>{k}</span><span title={k === 'Hash' ? hash : undefined}>{v}</span>
            </div>
          ))}
          {feedsOff.length > 0 && (
            <div className="prov-feeds-warn">⚠ Feeds offline: {feedsOff.join(', ')}</div>
          )}
        </div>
      </details>
    </div>
  );
}

// ── Mission vocabulary helpers ────────────────────────────────────────────────
// Stage 3: dashboard reads as a mission tool. Engine vocabulary stays in the
// API (GO / ADVISORY / CAUTION / NO_GO), but the operator sees mission
// vocabulary (CLEAR / CAUTION / HIGH_RISK / DELAY) — same words as the
// Mission Planner at /mission so the two surfaces are consistent.

const MISSION_VERDICT_BY_ENGINE_ACTION = {
  GO:       { level: 'CLEAR',     summary: 'CLEAR · Proceed',                  color: 'var(--green)'  },
  ADVISORY: { level: 'CAUTION',   summary: 'CAUTION · Monitor and adapt',      color: 'var(--yellow)' },
  CAUTION:  { level: 'HIGH_RISK', summary: 'HIGH RISK · Mitigate before launch', color: 'var(--orange)' },
  NO_GO:    { level: 'DELAY',     summary: 'DELAY · Do not proceed',           color: 'var(--red)'    },
};

/** Translate the engine response into mission-vocab + reliability scores.
 *  Derived client-side from the same `decision` the engine already returned —
 *  no extra network call. Stage 2's /api/v3/mission/assess endpoint applies
 *  the full mission-aware tolerance logic; the dashboard uses civilian
 *  thresholds (10 m GPS error) which is what users selecting a Platform
 *  preset implicitly opted into. Operators who need RTK-grade tolerance
 *  click the "Run as Mission" link to /mission and get the tighter scoring.
 */
function deriveMissionView(decision) {
  const verdict =
    MISSION_VERDICT_BY_ENGINE_ACTION[(decision?.action || 'GO').toUpperCase()] ||
    MISSION_VERDICT_BY_ENGINE_ACTION.GO;

  const wps = decision?.waypoints || [];
  const worstGps = wps.reduce((m, w) => Math.max(m, w.gps_error_m || 0), 0);
  const hfBad = wps.filter(w => w.hf_viable === false).length;
  const hfTotal = wps.length || 1;
  const pcaAny = wps.some(w => w.pca_active);

  // Dashboard tolerance: 10 m (civilian primary nav). Aligned with the
  // "medium" GNSS-dependence preset on /mission.
  const GNSS_TOL_M = 10;
  const gnssScore = Math.max(0, Math.min(100, 100 - (worstGps / (GNSS_TOL_M * 3)) * 100));
  const gnssLabel =
    gnssScore >= 80 ? 'GOOD' :
    gnssScore >= 55 ? 'DEGRADED' :
    gnssScore >= 30 ? 'POOR' : 'UNRELIABLE';

  let commsRisk = (hfBad / hfTotal) * 80;
  if (pcaAny) commsRisk += 25;
  commsRisk = Math.max(0, Math.min(100, commsRisk));
  const commsLabel =
    commsRisk >= 70 ? 'CRITICAL' :
    commsRisk >= 40 ? 'HIGH' :
    commsRisk >= 15 ? 'MODERATE' : 'LOW';

  return {
    verdict,
    gnss: { score: gnssScore, label: gnssLabel, worst_error_m: worstGps, tolerance_m: GNSS_TOL_M },
    comms: { score: commsRisk, label: commsLabel, hf_viable_legs: hfTotal - hfBad, total_legs: hfTotal, pca_active: pcaAny },
  };
}

function scoreColor(s) {
  return s >= 80 ? 'var(--green)' : s >= 55 ? 'var(--yellow)' : s >= 30 ? 'var(--orange)' : 'var(--red)';
}
function riskColor(r) {
  return r >= 70 ? 'var(--red)' : r >= 40 ? 'var(--orange)' : r >= 15 ? 'var(--yellow)' : 'var(--green)';
}

// ── Decision Result ───────────────────────────────────────────────────────────
// Verdict-first layout (Stage 3): mission verdict + reliability cards lead,
// recommended actions next, and the science details (confidence drivers,
// per-waypoint table, provenance) collapse into a "Scientific view" section
// for operators who want them. Engine vocabulary stays intact in the
// underlying decision object — the dashboard just renders it in mission
// language.

function DecisionResult({ decision }) {
  if (!decision) return null;
  const stale = decision.confidence?.stale_data === true;
  const recs  = decision.recommended_actions || [];
  const view  = deriveMissionView(decision);
  const v     = view.verdict;

  return (
    <div className="dec-panel" data-testid="decision-result">
      {/* Stale data warning — visible text, not colour-only */}
      {stale && (
        <div className="stale-banner">
          <span style={{ fontSize: 16 }}>⚠</span>
          <span>
            <strong>STALE DATA</strong> — observation age exceeds 10 minutes.
            Confidence is penalised. Verify at NOAA SWPC before acting.
          </span>
        </div>
      )}

      {/* ── Mission verdict (verdict-first per the operational-mode principle) */}
      <div
        className="dec-action-header"
        style={{ borderLeftColor: v.color, background: `${v.color}14` }}
      >
        <div>
          <div className="dec-action-badge" style={{ background: v.color }}>
            {v.level.replace('_', ' ')}
          </div>
          <div className="dec-type-sub">MISSION RISK · {v.summary.toUpperCase()}</div>
        </div>
        {decision.valid_until && (
          <div className="dec-valid">
            Valid until<br />
            {new Date(decision.valid_until).toUTCString().replace(' GMT', 'Z')}
          </div>
        )}
      </div>

      {/* Plain-language explanation — operator language, not Kp */}
      <div className="dec-sentence" data-testid="action-sentence">
        {decision.action_sentence || '—'}
      </div>

      {/* ── Reliability score cards (the operator's headline numbers) */}
      <div className="dec-block">
        <div className="ms-scores">
          <div className="ms-score">
            <div className="ms-score-label">GNSS Reliability · {view.gnss.label}</div>
            <div className="ms-score-value" style={{ color: scoreColor(view.gnss.score) }}>
              {view.gnss.score.toFixed(0)}
              <span className="ms-score-unit"> / 100</span>
            </div>
            <div className="ms-score-bar">
              <div
                className="ms-score-fill"
                style={{ width: `${view.gnss.score.toFixed(0)}%`, background: scoreColor(view.gnss.score) }}
              />
            </div>
            <div className="ms-score-sub">
              Worst-leg {view.gnss.worst_error_m.toFixed(1)} m · {view.gnss.tolerance_m} m tolerance
            </div>
          </div>
          <div className="ms-score">
            <div className="ms-score-label">Comms Risk · {view.comms.label}</div>
            <div className="ms-score-value" style={{ color: riskColor(view.comms.score) }}>
              {view.comms.score.toFixed(0)}
              <span className="ms-score-unit"> / 100</span>
            </div>
            <div className="ms-score-bar">
              <div
                className="ms-score-fill"
                style={{ width: `${view.comms.score.toFixed(0)}%`, background: riskColor(view.comms.score) }}
              />
            </div>
            <div className="ms-score-sub">
              {view.comms.hf_viable_legs}/{view.comms.total_legs} HF-viable
              {view.comms.pca_active ? ' · PCA active' : ''}
            </div>
          </div>
        </div>
      </div>

      {/* ── Recommended action(s) — what the operator should do */}
      {recs.length > 0 && (
        <div className="dec-block">
          <div className="section-label">RECOMMENDED ACTION</div>
          <ul className="dec-rec-list">
            {recs.map((r, i) => <li key={i}>{r}</li>)}
          </ul>
        </div>
      )}

      {/* Alternatives — engine-level fallback actions (e.g. DELAY_OPERATION) */}
      {(decision.alternatives || []).length > 0 && (
        <div className="dec-block dec-alts">
          Also consider:{' '}
          {(decision.alternatives || []).map(a => (
            <span key={a} className="dec-alt-pill">{String(a).replace(/_/g, ' ')}</span>
          ))}
        </div>
      )}

      {/* ── "Run as Mission Planner" handoff for full mission-aware scoring */}
      <div className="dec-block ms-handoff">
        <div style={{ fontSize: 11, color: 'var(--text-2)', lineHeight: 1.5 }}>
          Need RTK-grade tolerance, custom GNSS / comms dependence, or
          mission-specific risk profile?
        </div>
        <a href="/mission" className="ms-handoff-btn">▶ Run as Mission Planner</a>
      </div>

      {/* ── Scientific view (collapsed by default) ──────────────────────── */}
      <details className="dec-block ms-sci">
        <summary className="ms-sci-summary">
          <span className="ms-sci-chev">▸</span>
          Scientific view · confidence, impacts, per-waypoint table, raw inputs
        </summary>
        <div className="ms-sci-body">
          <ConfidenceSection confidence={decision.confidence} />
          <ImpactsSection impacts={decision.impacts} />
          <WaypointTable waypoints={decision.waypoints} />
          <ProvenanceSection provenance={decision.provenance} />
        </div>
      </details>
    </div>
  );
}

// ── Panel (main export) ───────────────────────────────────────────────────────

// Maps the UI preset key to the PlatformRequest object the backend expects.
// asset_type: GPS_L1 | GPS_L1L2 | GPS_L1L5 | GPS_INS | SBAS
// criticality: 1 (low) … 5 (high) — raises NO-GO threshold for critical platforms
const PLATFORM_PRESETS = {
  hmmwv:       { asset_type: 'GPS_L1',   criticality: 3, system_dependencies: [] },
  lmtv:        { asset_type: 'GPS_L1',   criticality: 2, system_dependencies: [] },
  mrap:        { asset_type: 'GPS_L1L2', criticality: 4, system_dependencies: [] },
  rotary_wing: { asset_type: 'GPS_L1L2', criticality: 4, system_dependencies: [] },
  fixed_wing:  { asset_type: 'GPS_L1L5', criticality: 4, system_dependencies: [] },
  dismounted:  { asset_type: 'GPS_L1',   criticality: 2, system_dependencies: [] },
  maritime:    { asset_type: 'GPS_INS',  criticality: 3, system_dependencies: [] },
  generic:     { asset_type: 'GPS_L1',   criticality: 3, system_dependencies: [] },
};

const PLATFORMS = [
  { value: 'hmmwv',        label: 'HMMWV' },
  { value: 'lmtv',         label: 'LMTV' },
  { value: 'mrap',         label: 'MRAP' },
  { value: 'rotary_wing',  label: 'Rotary Wing' },
  { value: 'fixed_wing',   label: 'Fixed Wing' },
  { value: 'dismounted',   label: 'Dismounted' },
  { value: 'maritime',     label: 'Maritime' },
  { value: 'generic',      label: 'Generic / Other' },
];

export default function Panel({
  waypoints,
  layers,
  clickMode,
  decision:       decisionProp,   // lifted from App for Globe sync
  onDecision,                     // (decision | null) → void
  onWaypointsChange,
  onLayersChange,
  onClickModeToggle,
  onFlyToRoute,
  onToggleLayer,
  // Replay props
  platform:       platformProp,   // lifted to App so replay can use it
  onPlatformChange,
  replaySnapshot,                 // currently selected snapshot | null
  onReturnToLive,                 // () → void — clears replay
}) {
  const { decision: localDecision, loading, error, getRouteDecision, clearDecision } = useDecision();
  // Use lifted decision if provided (reflects Globe's current state), else local
  const decision = decisionProp ?? localDecision;

  // Local form state
  const [lat,          setLat]          = useState('');
  const [lon,          setLon]          = useState('');
  const [wpName,       setWpName]       = useState('');
  // Platform may be lifted to App (for replay); fall back to local state
  const [localPlatform, setLocalPlatform] = useState('hmmwv');
  const platform    = platformProp   ?? localPlatform;
  const setPlatform = onPlatformChange ?? setLocalPlatform;

  function addWaypoint() {
    const la = parseFloat(lat);
    const lo = parseFloat(lon);
    if (isNaN(la) || isNaN(lo) || la < -90 || la > 90 || lo < -180 || lo > 180) return;
    const name = wpName.trim() || `WP${String(waypoints.length + 1).padStart(2, '0')}`;
    onWaypointsChange([...waypoints, { lat: +la.toFixed(4), lon: +lo.toFixed(4), name }]);
    setLat(''); setLon(''); setWpName('');
  }

  function removeWaypoint(i) {
    onWaypointsChange(waypoints.filter((_, idx) => idx !== i));
    clearDecision();
    onDecision?.(null);
  }

  function clearAll() {
    onWaypointsChange([]);
    clearDecision();
    onDecision?.(null);
  }

  async function handleGetDecision() {
    if (!waypoints.length) return;
    const platformObj = PLATFORM_PRESETS[platform] ?? PLATFORM_PRESETS.generic;
    const d = await getRouteDecision(waypoints, platformObj);
    if (d) {
      onDecision?.(d);   // lift to App so Globe can re-colour markers
      onFlyToRoute?.();
    }
  }

  // Explicit live-decision call even when a snapshot is selected
  async function handleGetLiveDecision() {
    if (!waypoints.length) return;
    const platformObj = PLATFORM_PRESETS[platform] ?? PLATFORM_PRESETS.generic;
    const d = await getRouteDecision(waypoints, platformObj);
    if (d) {
      onDecision?.(d);
      onFlyToRoute?.();
    }
  }

  return (
    <aside className="panel" aria-label="IonShield control panel">
      {/* Layer toggles */}
      <LayerToggles layers={layers} onToggle={onToggleLayer} />

      {/* Scrollable body */}
      <div className="panel-scroll">

        {/* Waypoint builder */}
        <div className="panel-section">
          <div className="section-label" style={{ display: 'flex', justifyContent: 'space-between' }}>
            <span>WAYPOINTS ({waypoints.length})</span>
            {waypoints.length > 0 && (
              <button
                style={{ background: 'none', border: 'none', color: 'var(--text-3)', fontSize: 9, cursor: 'pointer' }}
                onClick={clearAll}
              >Clear all</button>
            )}
          </div>

          <WaypointList
            waypoints={waypoints}
            decision={decision}
            onRemove={removeWaypoint}
          />

          {/* Add-waypoint form */}
          <div style={{ marginTop: 10, background: 'var(--bg-3)', border: '1px solid var(--border)', borderRadius: 6, padding: '10px 10px 8px' }}>
            <div className="field-label" style={{ marginBottom: 6 }}>ADD WAYPOINT</div>
            <div className="input-row" style={{ marginBottom: 6 }}>
              <div style={{ flex: 1 }}>
                <div className="field-label">Lat</div>
                <input className="ion-input" type="number" step="0.0001" min="-90" max="90"
                  placeholder="38.8" value={lat} onChange={e => setLat(e.target.value)}
                  aria-label="Latitude" />
              </div>
              <div style={{ flex: 1 }}>
                <div className="field-label">Lon</div>
                <input className="ion-input" type="number" step="0.0001" min="-180" max="180"
                  placeholder="-77.0" value={lon} onChange={e => setLon(e.target.value)}
                  aria-label="Longitude" />
              </div>
            </div>
            <div className="input-row">
              <input
                className="ion-input"
                type="text"
                maxLength={64}
                placeholder="Name (optional)"
                value={wpName}
                onChange={e => setWpName(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && addWaypoint()}
                style={{ flex: 1 }}
              />
              <button className="btn-secondary" onClick={addWaypoint} style={{ flexShrink: 0 }}
                data-testid="wp-add-btn">
                + Add
              </button>
            </div>
          </div>

          {/* Map click mode */}
          <button
            className="btn-secondary"
            onClick={onClickModeToggle}
            style={{ width: '100%', marginTop: 6, borderColor: clickMode ? 'var(--blue)' : undefined, color: clickMode ? 'var(--blue-hi)' : undefined }}
          >
            {clickMode ? '✕ Cancel map click' : '⊕ Click map to add waypoint'}
          </button>
        </div>

        {/* Platform + decision action */}
        <div className="panel-section">
          <div className="field-label" style={{ marginBottom: 4 }}>PLATFORM</div>
          <select className="ion-select" value={platform} onChange={e => setPlatform(e.target.value)}
            style={{ marginBottom: 10 }}>
            {PLATFORMS.map(p => <option key={p.value} value={p.value}>{p.label}</option>)}
          </select>

          {/* Replay-mode action buttons */}
          {replaySnapshot ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <div style={{ fontSize: 10, color: 'var(--yellow)', fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase' }}>
                ⏪ Snapshot selected
              </div>
              <button
                className="btn-primary"
                onClick={handleGetDecision}
                disabled={loading || !waypoints.length}
                style={{ background: 'var(--yellow)', color: '#000' }}
              >
                {loading ? 'Replaying…' : '⏪ Replay with this snapshot'}
              </button>
              <div className="btn-row">
                <button
                  className="btn-secondary"
                  onClick={handleGetLiveDecision}
                  disabled={loading || !waypoints.length}
                  style={{ flex: 1 }}
                >
                  ▶ Get Live Decision
                </button>
                {waypoints.length > 0 && (
                  <button className="btn-secondary" onClick={onFlyToRoute}
                    title="Fly camera to route" aria-label="Fly camera to route">
                    ⊙
                  </button>
                )}
              </div>
            </div>
          ) : (
            <div className="btn-row">
              <button
                className="btn-primary"
                onClick={handleGetDecision}
                disabled={loading || !waypoints.length}
              >
                {loading ? 'Computing…' : '▶ Get Route Decision'}
              </button>
              {waypoints.length > 0 && (
                <button className="btn-secondary" onClick={onFlyToRoute}
                  title="Fly camera to route" aria-label="Fly camera to route">
                  ⊙
                </button>
              )}
            </div>
          )}
        </div>

        {/* Result area */}
        <div className="panel-section" aria-live="polite" aria-label="Route decision result">

          {/* Replay-mode banner — only when decision is a replay result */}
          {decision?.replay && (
            <div className="replay-result-banner">
              <div>
                <div style={{ fontWeight: 700, fontSize: 11 }}>
                  ⏪ REPLAYING ARCHIVE SNAPSHOT
                </div>
                <div style={{ fontSize: 9, marginTop: 2, color: 'var(--yellow)', opacity: 0.8 }}>
                  {decision.replay.fetched_at
                    ? decision.replay.fetched_at.replace('T', ' ').slice(0, 16) + 'Z'
                    : `Snapshot #${decision.replay.snapshot_id}`}
                  {' · '}Kp {decision.replay.kp_at_snapshot?.toFixed(1) ?? '—'}
                  {' · '}{decision.replay.fetch_source ?? ''}
                </div>
              </div>
              {onReturnToLive && (
                <button className="replay-return-btn" onClick={onReturnToLive}>
                  ✕ Live
                </button>
              )}
            </div>
          )}

          {error && (
            <div className="ion-error">
              {error}
              {error.includes('401') && (
                <span> — set API key via ⚙ in the header.</span>
              )}
            </div>
          )}
          {!error && !decision && !loading && (
            <div style={{ fontSize: 11, color: 'var(--text-4)', textAlign: 'center', padding: '12px 0' }}>
              Add waypoints and click "Get Route Decision" to see the v2 typed recommendation.
            </div>
          )}
          {loading && <div className="ion-loading">Computing decision…</div>}
          {/* Mission-aware assessment (handoff from /mission) renders the v3
              MissionAssessment card; the normal click-to-place flow renders the
              v2 DecisionResult. Branch on the v3-only mission_risk_level field. */}
          {decision?.mission_risk_level
            ? <MissionAssessment assessment={decision} />
            : <DecisionResult decision={decision} />}
        </div>

      </div>
    </aside>
  );
}

// Export sub-components for unit tests
export { DecisionResult, ConfidenceSection, WaypointList };
