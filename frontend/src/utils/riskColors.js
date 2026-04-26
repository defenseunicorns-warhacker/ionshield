/**
 * riskColors.js
 * Centralised colour/label palette for risk levels and action types.
 * All Cesium-facing colours are also provided as [r,g,b,a] arrays (0-255).
 */

export const RISK = {
  NOMINAL:  { color: '#10B981', bg: 'rgba(16,185,129,0.12)',  border: 'rgba(16,185,129,0.35)', rgba: [16,  185, 129, 180] },
  ELEVATED: { color: '#F59E0B', bg: 'rgba(245,158,11,0.12)', border: 'rgba(245,158,11,0.35)', rgba: [245, 158,  11, 180] },
  DEGRADED: { color: '#F97316', bg: 'rgba(249,115,22,0.12)', border: 'rgba(249,115,22,0.35)', rgba: [249, 115,  22, 180] },
  SEVERE:   { color: '#EF4444', bg: 'rgba(239,68,68,0.12)',  border: 'rgba(239,68,68,0.35)',  rgba: [239,  68,  68, 200] },
};

export const ACTION = {
  // Route risk actions
  GO:               { color: '#10B981', label: 'GO',               brief: 'Route viable.' },
  ADVISORY:         { color: '#F59E0B', label: 'ADVISORY',         brief: 'Proceed with awareness.' },
  CAUTION:          { color: '#F97316', label: 'CAUTION',          brief: 'Mitigations recommended.' },
  NO_GO:            { color: '#EF4444', label: 'NO-GO',            brief: 'Conditions not safe for this route.' },
  // Comms fallback actions
  USE_PRIMARY_HF:   { color: '#10B981', label: 'USE PRIMARY HF',   brief: 'Primary HF band is viable.' },
  USE_ALTERNATE_HF: { color: '#F59E0B', label: 'USE ALTERNATE HF', brief: 'Switch to alternate HF frequency.' },
  SWITCH_TO_SATCOM: { color: '#F59E0B', label: 'SWITCH TO SATCOM', brief: 'Use SATCOM instead of HF.' },
  SWITCH_TO_UHF:    { color: '#F97316', label: 'SWITCH TO UHF',    brief: 'UHF preferred over HF.' },
  DEGRADED_MODE:    { color: '#F97316', label: 'DEGRADED MODE',    brief: 'Significant comms degradation expected.' },
  HF_NOT_VIABLE:    { color: '#EF4444', label: 'HF NOT VIABLE',    brief: 'HF inoperable — use alternate.' },
};

/** Returns the palette entry for a risk level, falling back to NOMINAL. */
export function getRiskStyle(level) {
  return RISK[level] || RISK.NOMINAL;
}

/** Returns the palette entry for an action enum value. */
export function getActionStyle(action) {
  return ACTION[action] || { color: '#94a3b8', label: String(action).replace(/_/g, ' '), brief: '' };
}

/** Maps a confidence score (0–1) to a CSS colour. */
export function confScoreColor(score) {
  if (score >= 0.75) return '#10B981';
  if (score >= 0.55) return '#F59E0B';
  return '#EF4444';
}

/** Maps the confidence label enum to a human-readable string. */
export function confLabelText(label) {
  return { HIGH: 'HIGH', MEDIUM: 'MEDIUM', LOW: 'LOW', VERY_LOW: 'VERY LOW' }[label] ?? (label || '—');
}
