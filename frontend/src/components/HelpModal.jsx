/**
 * HelpModal.jsx
 * Full-screen overlay with user guide / about information.
 * Dismissed by clicking outside the box or pressing Escape.
 */

import { useEffect } from 'react';

export default function HelpModal({ onClose }) {
  // Close on Escape
  useEffect(() => {
    function handler(e) { if (e.key === 'Escape') onClose(); }
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [onClose]);

  return (
    <div className="modal-overlay" onClick={e => e.target === e.currentTarget && onClose()}>
      <div className="modal-box" role="dialog" aria-modal="true" aria-labelledby="help-title">

        <div className="modal-header">
          <span id="help-title" className="modal-title">IonShield 3D — User Guide</span>
          <button className="modal-close" onClick={onClose} aria-label="Close help">✕</button>
        </div>

        <div className="modal-body">

          <h3>Overview</h3>
          <p>
            IonShield translates real-time NOAA space weather data into actionable
            route-risk and comms recommendations for field operators.
            The 3D globe shows your route against live ionospheric overlays.
          </p>

          <h3>Building a Route</h3>
          <ul>
            <li>Type coordinates (lat / lon) in the panel and click <strong>+ Add</strong>.</li>
            <li>Or click <strong>⊕ Click map to add waypoint</strong> then click anywhere on the globe.</li>
            <li>Waypoints appear as numbered pins on the globe with a connecting polyline.</li>
            <li>Remove individual waypoints with the <strong>✕</strong> button, or <strong>Clear all</strong>.</li>
          </ul>

          <h3>Getting a Route Decision</h3>
          <ul>
            <li>Select a <strong>Platform</strong> type (affects recommendations).</li>
            <li>Click <strong>▶ Get Route Decision</strong> — this calls <code>/api/v2/route-decision</code>.</li>
            <li>The result panel shows the overall action, confidence, per-waypoint risk, and provenance.</li>
            <li>The globe marker colours update to reflect each waypoint's risk level.</li>
          </ul>

          <h3>Decision Fields Explained</h3>
          <table>
            <thead><tr><th>Field</th><th>Meaning</th></tr></thead>
            <tbody>
              <tr><td><strong>Action badge</strong></td><td>GO / ADVISORY / CAUTION / NO-GO — the engine's recommended action.</td></tr>
              <tr><td><strong>Action sentence</strong></td><td>Plain-English rationale. Never rely on colour alone — read this text.</td></tr>
              <tr><td><strong>Confidence</strong></td><td>Score 0–1 with label. HIGH (≥0.75): trust fully. LOW: verify before acting.</td></tr>
              <tr><td><strong>Drivers</strong></td><td>Each factor that raised (+) or penalised (−) the confidence score.</td></tr>
              <tr><td><strong>Impacts</strong></td><td>Quantified system effects (GPS error, HF absorption, SATCOM fade).</td></tr>
              <tr><td><strong>Provenance</strong></td><td>Exact inputs + SHA-256 hash. Same hash = same decision. Used for audit and replay.</td></tr>
            </tbody>
          </table>

          <h3>Stale Data Warning</h3>
          <p>
            If NOAA data is more than 10 minutes old, a <strong>⚠ STALE DATA</strong> banner
            appears at the top of the decision result and the confidence score is penalised.
            Cross-check at <a href="https://www.swpc.noaa.gov/" target="_blank" rel="noopener noreferrer" style={{ color: 'var(--blue-hi)' }}>NOAA SWPC</a> before mission-critical action.
          </p>

          <h3>Data Layers (Globe Overlays)</h3>
          <table>
            <thead><tr><th>Layer</th><th>What it shows</th></tr></thead>
            <tbody>
              <tr><td><strong>Ionosphere TEC</strong></td><td>Estimated total electron content disturbance. Auroral ovals expand equatorward as Kp rises. Colour: yellow → red with severity. <em>Indicative only — derived from Kp, not a precision TEC map.</em></td></tr>
              <tr><td><strong>Polar Cap Absorption</strong></td><td>Red overlay at latitudes &gt;65° when proton flux indicates HF blackout in polar regions.</td></tr>
              <tr><td><strong>Solar Wind Alert</strong></td><td>Current solar wind speed label on the globe. Green &lt;400 km/s, yellow 400–700, red &gt;700 km/s.</td></tr>
            </tbody>
          </table>

          <h3>API Key</h3>
          <p>
            If the backend was started with <code>API_KEY=…</code>, click <strong>⚙</strong>
            in the header, enter your key, and click Save.
            The key is stored in browser localStorage only — never sent to third parties.
            Leave blank for open / unauthenticated mode.
          </p>

          <h3>Replay</h3>
          <p>
            To re-run a past decision, use <code>GET /api/v2/replay?lat=…&lon=…&snapshot_id=N</code>
            directly in the browser or with curl. See <code>docs/replay.md</code> for full instructions.
          </p>

          <h3>Disclaimer</h3>
          <p style={{ color: 'var(--orange)', fontWeight: 600 }}>
            NOT FOR SAFETY-OF-LIFE NAVIGATION. Models are approximate.
            Always verify with official NOAA SWPC products before mission-critical decisions.
          </p>

        </div>
      </div>
    </div>
  );
}
