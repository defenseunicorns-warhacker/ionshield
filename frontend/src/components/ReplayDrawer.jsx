/**
 * ReplayDrawer.jsx
 * Left-side overlay drawer for browsing + replaying archived NOAA snapshots.
 *
 * UX flow:
 *   1. Open drawer → snapshots load (most-recent first)
 *   2. Click a snapshot → onSelectSnapshot(snapshot) → App replays route
 *   3. Auto-play → cycles snapshots at configurable speed
 *   4. Close drawer → replaySnapshot cleared in App
 */

import { useState, useEffect, useRef, useCallback } from 'react';
import { useSnapshots } from '../hooks/useSnapshots.js';

// ── Helpers ───────────────────────────────────────────────────────────────────

const KP_THRESHOLDS = [
  [7, 'var(--red)'],
  [5, 'var(--orange)'],
  [4, 'var(--yellow)'],
  [0, 'var(--green)'],
];

function kpColor(kp) {
  for (const [thresh, col] of KP_THRESHOLDS) {
    if (kp >= thresh) return col;
  }
  return 'var(--green)';
}

function fmtUtc(iso) {
  // "2026-04-25 21:45Z"
  return iso ? iso.replace('T', ' ').slice(0, 16) + 'Z' : '—';
}

function SnapshotRow({ snap, isSelected, onClick }) {
  const kp    = snap.kp ?? 0;
  const barW  = Math.min(Math.round((kp / 9) * 100), 100);
  const col   = kpColor(kp);
  const bz    = snap.bz_nt;
  const bzStr = bz != null ? `Bz ${bz > 0 ? '+' : ''}${bz.toFixed(1)} nT` : null;
  const src   = snap.fetch_source ?? 'live';

  return (
    <button
      className={`replay-snap${isSelected ? ' replay-snap-active' : ''}`}
      onClick={onClick}
      aria-pressed={isSelected}
      aria-label={`Snapshot ${fmtUtc(snap.fetched_at)}, Kp ${kp.toFixed(1)}`}
    >
      <div className="replay-snap-time">{fmtUtc(snap.fetched_at)}</div>

      <div className="replay-snap-row">
        <div className="replay-kp-track">
          <div className="replay-kp-fill" style={{ width: `${barW}%`, background: col }} />
        </div>
        <span className="replay-kp-val" style={{ color: col }}>
          Kp&nbsp;{kp.toFixed(1)}
        </span>
        <span className={`replay-src-badge replay-src-${src}`}>{src}</span>
      </div>

      {bzStr && (
        <div className="replay-snap-meta">
          {bzStr}
          {snap.wind_speed_km_s != null && ` · ${snap.wind_speed_km_s.toFixed(0)} km/s`}
          {snap.xray_flux != null && snap.xray_flux > 1e-5 && ' · X-ray elevated'}
        </div>
      )}
    </button>
  );
}

// ── ReplayDrawer ──────────────────────────────────────────────────────────────

export default function ReplayDrawer({ open, onClose, onSelectSnapshot, selectedSnapshotId }) {
  const { snapshots, total, loading, error, hasMore, loadMore, refresh } =
    useSnapshots(open);

  // Auto-play state
  const [playing,   setPlaying]   = useState(false);
  const [playIdx,   setPlayIdx]   = useState(0);
  const [playSpeed, setPlaySpeed] = useState(1500);
  const intervalRef = useRef(null);

  // Stop playing when drawer closes
  useEffect(() => {
    if (!open) { setPlaying(false); setPlayIdx(0); }
  }, [open]);

  // Auto-play interval
  useEffect(() => {
    clearInterval(intervalRef.current);
    if (!playing || !snapshots.length) return;

    intervalRef.current = setInterval(() => {
      setPlayIdx(prev => {
        const next = prev + 1;
        if (next >= snapshots.length) {
          setPlaying(false);
          return prev;
        }
        onSelectSnapshot(snapshots[next]);
        return next;
      });
    }, playSpeed);

    return () => clearInterval(intervalRef.current);
  }, [playing, playSpeed, snapshots, onSelectSnapshot]);

  const handleClickSnap = useCallback((snap, idx) => {
    setPlayIdx(idx);
    setPlaying(false);
    onSelectSnapshot(snap);
  }, [onSelectSnapshot]);

  const goNewest = useCallback(() => {
    if (!snapshots.length) return;
    setPlayIdx(0);
    onSelectSnapshot(snapshots[0]);
  }, [snapshots, onSelectSnapshot]);

  const goOldest = useCallback(() => {
    if (!snapshots.length) return;
    const last = snapshots.length - 1;
    setPlayIdx(last);
    onSelectSnapshot(snapshots[last]);
  }, [snapshots, onSelectSnapshot]);

  const togglePlay = useCallback(() => {
    setPlaying(p => {
      if (!p && snapshots.length) onSelectSnapshot(snapshots[playIdx]);
      return !p;
    });
  }, [snapshots, playIdx, onSelectSnapshot]);

  if (!open) return null;

  const selectedIdx = snapshots.findIndex(s => s.id === selectedSnapshotId);

  return (
    <div
      className="replay-drawer"
      role="complementary"
      aria-label="Replay archive panel"
    >
      {/* ── Header ── */}
      <div className="replay-hdr">
        <div>
          <div className="replay-hdr-title">⏪ REPLAY ARCHIVE</div>
          <div className="replay-hdr-sub">
            {total > 0 ? `${total} snapshot${total !== 1 ? 's' : ''}` : 'Loading…'}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 6 }}>
          <button className="replay-refresh-btn" onClick={refresh} title="Refresh list" aria-label="Refresh snapshot list">↺</button>
          <button className="replay-close-btn"   onClick={onClose} aria-label="Close replay panel">✕</button>
        </div>
      </div>

      {/* ── Snapshot list ── */}
      <div className="replay-list" role="listbox" aria-label="Archived snapshots">
        {error && (
          <div className="ion-error" style={{ margin: '8px 10px' }}>{error}</div>
        )}

        {loading && !snapshots.length && (
          <div className="ion-loading">Loading archive…</div>
        )}

        {!loading && !error && !snapshots.length && (
          <div className="ion-loading">No snapshots found.<br />The archive fills as the server runs.</div>
        )}

        {snapshots.map((snap, idx) => (
          <SnapshotRow
            key={snap.id}
            snap={snap}
            isSelected={snap.id === selectedSnapshotId}
            onClick={() => handleClickSnap(snap, idx)}
          />
        ))}

        {hasMore && (
          <button
            className="replay-load-more"
            onClick={loadMore}
            disabled={loading}
          >
            {loading ? 'Loading…' : `Load more (${total - snapshots.length} remaining)`}
          </button>
        )}
      </div>

      {/* ── Auto-play controls ── */}
      <div className="replay-controls">
        <div className="replay-ctrl-row">
          <button
            className={`replay-play-btn${playing ? ' playing' : ''}`}
            onClick={togglePlay}
            disabled={!snapshots.length}
            aria-pressed={playing}
            aria-label={playing ? 'Pause auto-play' : 'Start auto-play'}
          >
            {playing ? '⏸ Pause' : '▶ Auto-play'}
          </button>
          <select
            className="replay-speed-sel"
            value={playSpeed}
            onChange={e => setPlaySpeed(Number(e.target.value))}
            aria-label="Playback speed"
          >
            <option value={500}>0.5 s</option>
            <option value={1000}>1 s</option>
            <option value={1500}>1.5 s</option>
            <option value={2000}>2 s</option>
            <option value={5000}>5 s</option>
          </select>
        </div>

        <div className="replay-ctrl-row">
          <button
            className="replay-nav-btn"
            onClick={goNewest}
            disabled={!snapshots.length || selectedIdx === 0}
            title="Jump to most-recent snapshot"
          >
            ⏮ Newest
          </button>
          <span className="replay-counter" aria-live="polite">
            {snapshots.length
              ? `${selectedIdx >= 0 ? selectedIdx + 1 : '—'} / ${snapshots.length}`
              : '—'}
          </span>
          <button
            className="replay-nav-btn"
            onClick={goOldest}
            disabled={!snapshots.length || selectedIdx === snapshots.length - 1}
            title="Jump to oldest snapshot"
          >
            Oldest ⏭
          </button>
        </div>
      </div>
    </div>
  );
}
