/**
 * App.jsx
 * Root component — owns top-level state and assembles the layout.
 *
 * Layout:
 *   ┌──────────────── Header (48 px) ──────────────────┐
 *   │  Globe (flex-1) + overlays     │  Panel (420 px) │
 *   │    ReplayDrawer (abs, 300px)   │                 │
 *   └────────────────────────────────┴─────────────────┘
 */

import { useState, useRef, useCallback, useEffect } from 'react';
import Globe           from './components/Globe.jsx';
import Header          from './components/Header.jsx';
import Panel           from './components/Panel.jsx';
import HelpModal       from './components/HelpModal.jsx';
import ReplayDrawer    from './components/ReplayDrawer.jsx';
import TimelineSlider  from './components/TimelineSlider.jsx';
import ElevationProfile from './components/ElevationProfile.jsx';
import { useStatus }   from './hooks/useStatus.js';
import { useLayers }   from './hooks/useLayers.js';
import { useForecast } from './hooks/useForecast.js';
import { api }         from './utils/api.js';

// Platform preset lookup (shared with Panel — needed for auto-replay)
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

export default function App() {
  // ── Core UI state ─────────────────────────────────────────────────────────
  const [waypoints,     setWaypoints]     = useState([]);
  const [clickMode,     setClickMode]     = useState(false);
  const [helpOpen,      setHelpOpen]      = useState(false);
  const [decision,      setDecision]      = useState(null);
  const [forecastIndex, setForecastIndex] = useState(null); // null = live

  // Platform lifted here so replay can use it without prop drilling through Panel
  const [platform, setPlatform] = useState('hmmwv');

  // ── Replay state ──────────────────────────────────────────────────────────
  const [replayOpen,     setReplayOpen]     = useState(false);
  const [replaySnapshot, setReplaySnapshot] = useState(null); // selected snapshot row
  const [replayBusy,     setReplayBusy]     = useState(false);

  const globeRef = useRef(null);

  // Live NOAA status (polled every 60 s)
  const { status } = useStatus();

  // Data-layer state — synced from live status
  const { layers, toggleLayer } = useLayers(status);

  // 72-h Kp forecast (fetched once on mount)
  const { forecast } = useForecast();

  // Derived forecastKp for the TEC layer globe rendering
  const forecastKp = forecastIndex !== null
    ? (forecast?.windows?.[forecastIndex]?.kp_forecast ?? null)
    : null;

  // ── Replay: auto-run route decision when snapshot changes ─────────────────
  useEffect(() => {
    if (!replaySnapshot || !waypoints.length) return;

    let cancelled = false;
    setReplayBusy(true);
    const platformObj = PLATFORM_PRESETS[platform] ?? PLATFORM_PRESETS.generic;

    api.replayRoute(waypoints, platformObj, replaySnapshot.id)
      .then(d => { if (!cancelled) { setDecision(d); globeRef.current?.flyToRoute(waypoints); } })
      .catch(() => {})                          // errors surface via Panel's useDecision
      .finally(() => { if (!cancelled) setReplayBusy(false); });

    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [replaySnapshot]);                         // intentionally exclude waypoints/platform

  // ── Map interaction ───────────────────────────────────────────────────────
  const handleMapClick = useCallback(({ lat, lon }) => {
    if (!clickMode) return;
    const name = `WP${String(waypoints.length + 1).padStart(2, '0')}`;
    setWaypoints(prev => [...prev, { lat, lon, name }]);
    setClickMode(false);
  }, [clickMode, waypoints.length]);

  const handleFlyToRoute = useCallback(() => {
    if (waypoints.length) globeRef.current?.flyToRoute(waypoints);
  }, [waypoints]);

  // ── Replay drawer handlers ─────────────────────────────────────────────────
  const handleSelectSnapshot = useCallback(snap => {
    setReplaySnapshot(snap);
  }, []);

  const handleReturnToLive = useCallback(() => {
    setReplaySnapshot(null);
    setDecision(null);
  }, []);

  const handleToggleReplay = useCallback(() => {
    setReplayOpen(o => {
      if (o) {
        // Closing: clear replay state
        setReplaySnapshot(null);
      }
      return !o;
    });
  }, []);

  return (
    <div className="app-root">
      <Header
        status={status}
        onHelp={() => setHelpOpen(true)}
        onReplay={handleToggleReplay}
        replayActive={replayOpen}
      />

      <div className="app-body">
        {/* 3D Globe + overlays */}
        <div className="globe-container">
          <Globe
            ref={globeRef}
            waypoints={waypoints}
            decision={decision}
            layers={layers}
            onMapClick={handleMapClick}
            clickMode={clickMode}
            forecastKp={forecastKp}
          />

          {/* Click-mode hint */}
          {clickMode && (
            <div className="globe-hint" role="status">
              Click anywhere on the globe to place a waypoint
            </div>
          )}

          {/* Replay busy indicator */}
          {replayBusy && (
            <div className="globe-hint" role="status" style={{ background: 'var(--yellow)', color: '#000' }}>
              ⏪ Replaying snapshot…
            </div>
          )}

          {/* Replay drawer (left overlay) */}
          <ReplayDrawer
            open={replayOpen}
            onClose={handleToggleReplay}
            onSelectSnapshot={handleSelectSnapshot}
            selectedSnapshotId={replaySnapshot?.id ?? null}
          />

          {/* Route elevation / risk profile */}
          <ElevationProfile waypoints={waypoints} decision={decision} />

          {/* Forecast timeline scrubber */}
          <TimelineSlider
            windows={forecast?.windows}
            activeIndex={forecastIndex}
            onSelect={setForecastIndex}
          />
        </div>

        {/* Right panel */}
        <Panel
          waypoints={waypoints}
          layers={layers}
          clickMode={clickMode}
          decision={decision}
          onDecision={setDecision}
          onWaypointsChange={wps => { setWaypoints(wps); setDecision(null); }}
          onToggleLayer={toggleLayer}
          onClickModeToggle={() => setClickMode(m => !m)}
          onFlyToRoute={handleFlyToRoute}
          platform={platform}
          onPlatformChange={setPlatform}
          replaySnapshot={replaySnapshot}
          onReturnToLive={handleReturnToLive}
        />
      </div>

      {helpOpen && <HelpModal onClose={() => setHelpOpen(false)} />}
    </div>
  );
}
