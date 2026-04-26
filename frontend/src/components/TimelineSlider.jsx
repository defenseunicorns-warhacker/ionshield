/**
 * TimelineSlider.jsx
 * Compact forecast timeline overlay at the bottom of the globe.
 *
 * Shows the 72-hour Kp forecast windows as a mini bar-chart.
 * Clicking a window selects it — the globe's TEC layer updates to
 * reflect that forecast Kp instead of the live value.
 * Clicking "NOW" returns to live conditions.
 */

import { getRiskStyle } from '../utils/riskColors.js';

export default function TimelineSlider({ windows, activeIndex, onSelect }) {
  if (!windows?.length) return null;

  const maxKp = Math.max(...windows.map(w => w.kp_forecast), 1);
  const active = activeIndex !== null ? windows[activeIndex] : null;

  return (
    <div className="tl-bar" role="region" aria-label="Kp forecast timeline">
      {/* Header row: title + readout */}
      <div className="tl-header">
        <span className="tl-title">KP FORECAST</span>
        <span className="tl-readout" aria-live="polite">
          {active ? (
            <>
              {active.label}&ensp;Kp{' '}
              <span style={{ fontWeight: 700 }}>{active.kp_forecast}</span>
              &ensp;
              <span style={{ color: getRiskStyle(active.risk_level).color }}>
                {active.risk_level}
              </span>
              &ensp;·&ensp;
              <span style={{ fontSize: '9px', color: 'var(--text-3)' }}>
                {active.gps_impact}
              </span>
            </>
          ) : (
            <span className="tl-live">● LIVE CONDITIONS</span>
          )}
        </span>
      </div>

      {/* Window selector */}
      <div className="tl-windows" role="group" aria-label="Select forecast window">
        {/* NOW pill */}
        <button
          className={`tl-now${activeIndex === null ? ' tl-active' : ''}`}
          onClick={() => onSelect(null)}
          aria-pressed={activeIndex === null}
          aria-label="Show live conditions"
        >
          NOW
        </button>

        {windows.map((w, i) => {
          const rs     = getRiskStyle(w.risk_level);
          const barPct = Math.max(12, (w.kp_forecast / maxKp) * 100);
          const isActive = i === activeIndex;
          return (
            <button
              key={i}
              className={`tl-win${isActive ? ' tl-active' : ''}`}
              onClick={() => onSelect(i)}
              aria-pressed={isActive}
              aria-label={`${w.label}: Kp ${w.kp_forecast}, ${w.risk_level}`}
              title={`${w.label} — Kp ${w.kp_forecast} (${w.risk_level})\n${w.gps_impact}`}
            >
              <span
                className="tl-bar-fill"
                style={{ height: `${barPct}%`, background: rs.color }}
                aria-hidden="true"
              />
              <span className="tl-win-lbl">{w.label}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
