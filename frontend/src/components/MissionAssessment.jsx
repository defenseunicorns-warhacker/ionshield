/**
 * MissionAssessment.jsx
 * Renders the mission-aware assessment from POST /api/v3/mission/assess — the
 * same response the Mission Planner (/mission) shows. Used on the globe when an
 * operator hands a mission off from the planner, so the technical view presents
 * identical data (verdict, mission-aware GNSS/comms scoring, consequences,
 * recommended actions, equipment doctrine, data quality, audit trail).
 *
 * This is a faithful React port of mission.html's renderResult(); keep the two
 * in sync if the assessment shape or framing changes.
 */

// ── Decision framing (presentation only — backend verdict unchanged) ──────────
// Engine mission_risk_level → operator-facing risk label + decision verb.
const RISK_DISPLAY = {
  CLEAR:     { label: 'CLEAR',    decision: 'GO',             cls: 'go' },
  CAUTION:   { label: 'CAUTION',  decision: 'MODIFY',         cls: 'advisory' },
  HIGH_RISK: { label: 'WARNING',  decision: 'MODIFY / DELAY', cls: 'caution' },
  DELAY:     { label: 'CRITICAL', decision: 'DELAY / ABORT',  cls: 'no_go' },
};

// Map equipment id → the mission function it supports (operator language).
const FUNCTION_BY_EQUIPMENT = {
  gps_single_freq:       'Navigation / waypoint & target accuracy',
  uas_group1:            'Autonomous flight / position hold',
  hf_radio:              'HF reachback / beyond-line-of-sight comms',
  uhf_satcom:            'UHF SATCOM / TACSAT C2',
  counter_battery_radar: 'Counter-battery detection',
};

const CONS_CHIP = { RED: 'red', AMBER: 'amber', GREEN: 'green' };
const EQ_CHIP   = { GREEN: 'green', AMBER: 'amber', RED: 'red' };

const scoreColor = (s) =>
  s >= 80 ? 'var(--green)' : s >= 55 ? 'var(--yellow)' : s >= 30 ? 'var(--orange)' : 'var(--red)';
const riskColor = (r) =>
  r >= 70 ? 'var(--red)' : r >= 40 ? 'var(--orange)' : r >= 15 ? 'var(--yellow)' : 'var(--green)';

// Build operator-facing "what could fail / what function / what decision"
// bullets strictly from the real assessment (equipment findings + scores).
function deriveConsequences(a, gnss, comms) {
  const out = [];
  const eq = a.equipment || {};
  for (const f of (eq.findings || [])) {
    if (f.risk === 'GREEN') continue;
    out.push({
      risk: f.risk,
      fn: FUNCTION_BY_EQUIPMENT[f.equipment_id] || f.display_name,
      fail: f.effect,
    });
  }
  const tol = Number(gnss.tolerance_m ?? 0);
  const worst = Number(gnss.worst_error_m ?? 0);
  if (tol > 0 && worst > tol) {
    out.push({
      risk: worst > tol * 2 ? 'RED' : 'AMBER',
      fn: 'Position confidence / blue-force tracking',
      fail: `GPS uncertainty ~${worst.toFixed(1)} m exceeds this mission's ${tol.toFixed(1)} m tolerance — waypoint, target, and reported-position confidence may degrade.`,
    });
  }
  const totalLegs = Number(comms.total_legs ?? 0);
  const hfLegs = Number(comms.hf_viable_legs ?? 0);
  if (comms.pca_active || (totalLegs > 0 && hfLegs < totalLegs)) {
    out.push({
      risk: (totalLegs > 0 && hfLegs === 0) || comms.pca_active ? 'RED' : 'AMBER',
      fn: 'HF reachback / comms continuity',
      fail: `HF reachback may be unreliable during the mission window (${hfLegs}/${totalLegs} legs viable${comms.pca_active ? ', polar-cap absorption active' : ''}).`,
    });
  }
  return out;
}

// One-sentence mission consequence for the verdict header.
function consequenceSentence(cons) {
  if (!cons.length) return 'No significant mission impact expected from current space-weather conditions.';
  const red = cons.find(c => c.risk === 'RED');
  return (red || cons[0]).fail;
}

// Primary recommendation. The engine sometimes leads its action list with a
// generic "all clear" line even when the verdict escalates; for any non-GO
// decision, lead with the first substantive action so the headline stays coherent.
function primaryRecommendation(recs, decision) {
  const list = recs || [];
  const generic = /^(No significant|Standard)\b/i;
  const substantive = list.filter(r => !generic.test(String(r).trim()));
  if (decision !== 'GO' && substantive.length) return substantive[0];
  if (list.length) return list[0];
  return {
    'GO': 'Proceed as planned. Continue normal monitoring.',
    'MODIFY': 'Modify the plan: add operator oversight or shift timing where flexible.',
    'MODIFY / DELAY': 'Increase operator oversight; delay time-sensitive autonomous operations if timing allows.',
    'DELAY / ABORT': 'Delay or abort the mission until conditions improve, or shift to unaffected systems.',
  }[decision] || 'Review mission consequences below.';
}

// ── Equipment readout (WarHacker P0-2) ────────────────────────────────────────
function EquipmentReadout({ eq }) {
  if (!eq || (!eq.findings?.length && !eq.unaffected?.length)) return null;
  return (
    <div className="ma-eq">
      <h4>Equipment impact{eq.weather_state ? ` · ${eq.weather_state}` : ''}</h4>
      {eq.likelihood && <div className="ma-eq-basis">{eq.likelihood}</div>}
      {(eq.findings || []).map((f, i) => (
        <div className="ma-eq-row" key={`f${i}`}>
          <span className={`ma-eq-chip ${EQ_CHIP[f.risk] || 'green'}`}>{f.risk}</span>
          <div className="ma-eq-body">
            <div className="nm">{f.display_name} <span className="sub">· {f.nomenclature}</span></div>
            <div>{f.effect}</div>
            {f.action && <div className="act">→ {f.action}</div>}
            {f.citation && <div className="cit">{f.citation}</div>}
          </div>
        </div>
      ))}
      {(eq.unaffected || []).map((u, i) => (
        <div className="ma-eq-row" key={`u${i}`}>
          <span className="ma-eq-chip unaff">AVAIL</span>
          <div className="ma-eq-body">
            <div className="nm">{u.display_name} <span className="sub">· {u.nomenclature}</span></div>
            <div>Confirmed unaffected — {u.why}</div>
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Main export ───────────────────────────────────────────────────────────────
export default function MissionAssessment({ assessment }) {
  if (!assessment) return null;

  const level = assessment.mission_risk_level || 'CLEAR';
  const disp = RISK_DISPLAY[level] || RISK_DISPLAY.CLEAR;

  const gnss = assessment.gnss || {};
  const comms = assessment.comms || {};
  const dq = assessment.data_quality || {};
  const inputs = assessment.inputs_echo || {};
  const raw = assessment.raw_decision || {};
  const prov = raw.provenance || {};

  const gnssScore = Number(gnss.score || 0);
  const commsScore = Number(comms.score || 0);
  const recs = assessment.recommended_actions || [];

  const consequences = deriveConsequences(assessment, gnss, comms);
  const primaryRec = primaryRecommendation(recs, disp.decision);
  const oneLineConsequence = consequenceSentence(consequences);

  const obs = (prov.observations_used || []).join(', ') || '—';
  const fcs = (prov.forecasts_used || []).join(', ') || '—';
  const dqClass = (dq.label || 'MEDIUM').toLowerCase();

  const worstErr = Number(gnss.worst_error_m || 0);
  const hfShort = Number(comms.total_legs || 0) - Number(comms.hf_viable_legs || 0);

  return (
    <div className="ma">
      {inputs.scenario && (
        <div className="ma-replay-banner">
          ⟲ REPLAY · {inputs.scenario_title || inputs.scenario} — real recorded
          measurements, not live conditions
        </div>
      )}

      {/* Decision verdict (operator-first) */}
      <div className={`ma-verdict ${disp.cls}`}>
        <div className="level">MISSION RISK: {disp.label} — {disp.decision}</div>
        <div className="ma-primary-rec">
          <span className="k">Primary recommendation:</span> {primaryRec}
        </div>
        <div className="summary">{oneLineConsequence}</div>
      </div>

      {/* Mission Consequences */}
      <div className="ma-cons">
        <h4>Mission Consequences</h4>
        {consequences.length ? (
          <ul>
            {consequences.map((c, i) => (
              <li key={i}>
                <span className={`ma-cons-chip ${CONS_CHIP[c.risk] || 'amber'}`}>{c.risk}</span>
                <div className="ma-cons-body">
                  <div className="ma-cons-fn">{c.fn}</div>
                  <div className="ma-cons-fail">{c.fail}</div>
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <div className="ma-cons-none">No significant mission functions affected by current conditions.</div>
        )}
        <div className="ma-cons-foot">What could fail → which mission function → drives the decision above.</div>
      </div>

      {/* Reliability scores */}
      <div className="ma-scores">
        <div className="ma-score">
          <div className="ma-score-label">GNSS Reliability{gnss.label ? ` · ${gnss.label}` : ''}</div>
          <div className="ma-score-value" style={{ color: scoreColor(gnssScore) }}>
            {gnssScore.toFixed(0)}<span className="ma-score-denom"> / 100</span>
          </div>
          <div className="ma-score-bar">
            <div className="ma-score-fill" style={{ width: `${gnssScore.toFixed(0)}%`, background: scoreColor(gnssScore) }} />
          </div>
          <div className="ma-score-sub">
            Worst-leg {worstErr.toFixed(2)} m vs {(gnss.tolerance_m ?? 0).toFixed(2)} m tolerance
            {gnss.affected_legs ? ` · ${gnss.affected_legs}/${gnss.total_legs} legs over tolerance` : ''}
          </div>
        </div>
        <div className="ma-score">
          <div className="ma-score-label">Comms Risk{comms.label ? ` · ${comms.label}` : ''}</div>
          <div className="ma-score-value" style={{ color: riskColor(commsScore) }}>
            {commsScore.toFixed(0)}<span className="ma-score-denom"> / 100</span>
          </div>
          <div className="ma-score-bar">
            <div className="ma-score-fill" style={{ width: `${commsScore.toFixed(0)}%`, background: riskColor(commsScore) }} />
          </div>
          <div className="ma-score-sub">
            {comms.hf_viable_legs ?? 0}/{comms.total_legs ?? 0} HF-viable legs
            {comms.pca_active ? ' · PCA active' : ''}
          </div>
        </div>
      </div>

      {/* Drivers (operator language) */}
      <div className="ma-driver-strip">
        <div className={`ma-driver ${worstErr > 5 ? 'warn' : ''} ${worstErr > 15 ? 'bad' : ''}`}>
          <span className="k">GPS error</span>
          <span className="v">{worstErr.toFixed(2)} m</span>
        </div>
        <div className={`ma-driver ${hfShort > 0 ? 'warn' : ''} ${(comms.hf_viable_legs === 0 && comms.total_legs > 0) ? 'bad' : ''}`}>
          <span className="k">HF availability</span>
          <span className="v">{comms.hf_viable_legs ?? 0}/{comms.total_legs ?? 0}</span>
        </div>
        <div className={`ma-driver ${comms.pca_active ? 'bad' : ''}`}>
          <span className="k">Polar cap</span>
          <span className="v">{comms.pca_active ? 'ACTIVE' : 'clear'}</span>
        </div>
      </div>

      {/* Recommended action(s) */}
      {recs.length > 0 && (
        <div className="ma-recs">
          <h4>Recommended action</h4>
          <ul>{recs.map((r, i) => <li key={i}>{r}</li>)}</ul>
        </div>
      )}

      <EquipmentReadout eq={assessment.equipment} />

      {/* Data quality */}
      <div className="ma-conf">
        <span className="ma-conf-label">Data quality</span>
        <span className={`ma-conf-pill ${dqClass}`}>
          {dq.label || '—'}{dq.score != null ? ` · ${Number(dq.score).toFixed(2)}` : ''}
        </span>
        <span className="ma-conf-detail">
          {(dq.notes || []).join(' ')}
          {dq.completeness != null ? ` Completeness ${Math.round(dq.completeness * 100)}%.` : ''}
        </span>
      </div>

      {/* Inputs & Provenance / Audit Trail */}
      <details className="ma-prov">
        <summary>Inputs &amp; Provenance / Audit Trail</summary>
        <div className="ma-prov-body">
          <div className="ma-prov-row">
            <span className="k">Mission</span>
            <span className="v">{inputs.callsign || '—'} · {inputs.mission_type || '—'} · {inputs.time_window || '—'}</span>
          </div>
          <div className="ma-prov-row">
            <span className="k">GNSS dep.</span>
            <span className="v">{inputs.gnss_dependence || '—'} · receiver {gnss.asset_type || '—'}</span>
          </div>
          <div className="ma-prov-row">
            <span className="k">Comms dep.</span>
            <span className="v">{inputs.comms_dependence || '—'} · {comms.fallback_hint || ''}</span>
          </div>
          <div className="ma-prov-row">
            <span className="k">Model version</span>
            <span className="v">{prov.model_version || '—'}</span>
          </div>
          <div className="ma-prov-row">
            <span className="k">Input hash</span>
            <span className="v">{prov.input_hash || '—'}</span>
          </div>
          <div className="ma-prov-row">
            <span className="k">Observations</span>
            <span className="v"><span className="ma-prov-tag measured">measured</span>{obs}</span>
          </div>
          <div className="ma-prov-row">
            <span className="k">Forecasts</span>
            <span className="v"><span className="ma-prov-tag modeled">modeled</span>{fcs}</span>
          </div>
          <div className="ma-prov-row">
            <span className="k">Decision engine</span>
            <span className="v"><span className="ma-prov-tag heuristic">heuristic</span>route-risk v2 (Klobuchar GPS · CCIR-888 HF · Bailey PCA)</span>
          </div>
          <div className="ma-prov-row">
            <span className="k">Generated</span>
            <span className="v">{assessment.generated_at || prov.computed_at || '—'}</span>
          </div>
        </div>
      </details>
    </div>
  );
}
