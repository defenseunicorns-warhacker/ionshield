/**
 * Header.jsx
 * Top status bar — logo, global risk badge, solar driver chips,
 * data-age indicator, settings (API key), and Help button.
 */

import { useState } from 'react';
import { getRiskStyle } from '../utils/riskColors.js';
import { getApiKey, setApiKey } from '../utils/api.js';

function formatAge(seconds) {
  if (seconds >= 9000) return 'unknown';
  if (seconds < 60)   return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

function DriverChip({ label, value, color }) {
  return (
    <div className="driver-chip">
      <div className="driver-chip-label">{label}</div>
      <div className="driver-chip-value" style={{ color: color || undefined }}>{value}</div>
    </div>
  );
}

/** Settings popover for API key. */
function SettingsPopover({ onClose }) {
  const [key, setKey] = useState(getApiKey());
  const [saved, setSaved] = useState(false);

  function save() {
    setApiKey(key.trim());
    setSaved(true);
    setTimeout(onClose, 800);
  }

  return (
    <div
      style={{
        position: 'absolute', top: 44, right: 0, zIndex: 50,
        background: 'var(--bg-2)', border: '1px solid var(--border)',
        borderRadius: 8, padding: 16, width: 280,
        boxShadow: '0 8px 30px rgba(0,0,0,0.5)',
      }}
    >
      <div style={{ fontSize: 11, fontWeight: 700, color: 'var(--text-2)', marginBottom: 8, textTransform: 'uppercase', letterSpacing: '.08em' }}>
        API Key
      </div>
      <input
        type="password"
        className="ion-input"
        value={key}
        onChange={e => setKey(e.target.value)}
        placeholder="Leave blank if not using auth"
        autoComplete="off"
        style={{ marginBottom: 8 }}
      />
      <div style={{ fontSize: 9, color: 'var(--text-3)', marginBottom: 10 }}>
        Stored in browser localStorage. Required only when API_KEY is set on the server.
      </div>
      <div style={{ display: 'flex', gap: 6 }}>
        <button className="btn-primary" onClick={save} style={{ fontSize: 11 }}>
          {saved ? 'Saved ✓' : 'Save'}
        </button>
        <button className="btn-secondary" onClick={() => { setKey(''); setApiKey(''); }}>
          Clear
        </button>
      </div>
    </div>
  );
}

export default function Header({ status, onHelp, onReplay, replayActive }) {
  const [settingsOpen, setSettingsOpen] = useState(false);

  const d       = status?.solar_drivers;
  const risk    = status?.global_risk_level || 'LOADING';
  const rs      = getRiskStyle(risk);
  const age     = status?.data_age_seconds ?? null;
  const stale   = age != null && age > 600;

  const kpColor = d ? (d.kp_current >= 7 ? 'var(--red)' : d.kp_current >= 5 ? 'var(--orange)' : d.kp_current >= 4 ? 'var(--yellow)' : 'var(--green)') : undefined;
  const bzColor = d ? (d.bz_nt < -10 ? 'var(--red)' : d.bz_nt < -5 ? 'var(--orange)' : d.bz_nt < 0 ? 'var(--yellow)' : 'var(--green)') : undefined;

  return (
    <header className="header">
      {/* Logo */}
      <div className="header-logo">
        <div className="header-logo-icon">
          <svg viewBox="0 0 24 24" fill="none" width="14" height="14" stroke="white" strokeWidth="2">
            <circle cx="12" cy="12" r="3"/>
            <path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/>
          </svg>
        </div>
        <div>
          <div className="header-title">IonShield</div>
          <div className="header-sub">Space Weather · 3D</div>
        </div>
      </div>

      <div className="header-sep" />

      {/* Global risk */}
      <div style={{ flexShrink: 0 }}>
        <div style={{ fontSize: 8, color: 'var(--text-3)', letterSpacing: '.08em', textTransform: 'uppercase', marginBottom: 2 }}>GLOBAL</div>
        <span className="risk-badge" style={{ background: rs.color }}>{risk}</span>
      </div>

      <div className="header-sep" />

      {/* Solar drivers */}
      <div className="header-drivers">
        <DriverChip label="Kp INDEX"    value={d ? d.kp_current.toFixed(1) : '—'}         color={kpColor} />
        <DriverChip label="IMF Bz"      value={d ? `${d.bz_nt > 0 ? '+' : ''}${d.bz_nt} nT` : '—'} color={bzColor} />
        <DriverChip label="X-RAY"       value={d?.xray_class || '—'} />
        <DriverChip label="SOLAR WIND"  value={d ? `${d.solar_wind_km_s} km/s` : '—'} />
        <DriverChip label="PROTON ≥10MeV" value={d ? `${d.proton_flux_10mev_pfu.toFixed(1)} pfu` : '—'} />
      </div>

      <div className="header-sep" />

      {/* Data age */}
      <div className={`data-age ${stale ? 'stale-age' : ''}`}>
        {age != null ? (
          <>
            {stale && '⚠ '}Data {formatAge(age)} old
            <br />
            <span style={{ fontSize: 9, color: 'var(--text-3)' }}>
              {status?.feed_status ? (() => {
                const total = Object.keys(status.feed_status).length;
                const ok    = Object.values(status.feed_status).filter(s => s === 'ok').length;
                return ok === total ? `${ok}/${total} feeds live` : `⚠ ${ok}/${total} feeds`;
              })() : ''}
            </span>
          </>
        ) : 'Connecting…'}
      </div>

      {/* Settings (API key) */}
      <div style={{ position: 'relative' }}>
        <button
          className="header-btn"
          onClick={() => setSettingsOpen(o => !o)}
          title="API key settings"
        >⚙</button>
        {settingsOpen && (
          <>
            <div
              style={{ position: 'fixed', inset: 0, zIndex: 49 }}
              onClick={() => setSettingsOpen(false)}
            />
            <SettingsPopover onClose={() => setSettingsOpen(false)} />
          </>
        )}
      </div>

      {/* Replay */}
      <button
        className="header-btn"
        onClick={onReplay}
        style={replayActive ? { borderColor: 'var(--yellow)', color: 'var(--yellow)' } : undefined}
        title="Browse archived NOAA snapshots and replay past decisions"
        aria-pressed={replayActive}
      >
        ⏪ Replay
      </button>

      {/* Simulation Mode — historical storm scrubber */}
      <a
        className="header-btn"
        href="/simulation"
        title="Open Simulation Mode — replay historical storms (May 2024 G5, Halloween 2003, etc.) on a 2D map"
        style={{ textDecoration: 'none', display: 'inline-flex', alignItems: 'center', gap: 4 }}
      >
        🌐 Simulation
      </a>

      {/* Help */}
      <button className="header-btn" onClick={onHelp}>? Help</button>
    </header>
  );
}
