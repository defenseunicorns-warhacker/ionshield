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

// Shared mission store — round-trips waypoints + profile with /mission.
// Both surfaces read it on load and write it on change; a 30-min TTL keeps
// it fresh so a stale visit doesn't silently reload an old mission.
const MISSION_KEY = 'ionshield_mission';
const MISSION_TTL_MS = 30 * 60 * 1000;

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

  // Transient note when waypoints arrive from the Mission Planner handoff
  const [handoffNote, setHandoffNote] = useState(null);

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

  // ── Mission Planner handoff ───────────────────────────────────────────────
  // On first load, pick up the mission from the shared store, drop its
  // waypoints on the globe, and auto-run the mission-aware assessment so the
  // same analytics / recommended actions render without re-entry. The store
  // persists (not one-shot) so the round-trip back to /mission is retained.
  useEffect(() => {
    let payload;
    try {
      const raw = localStorage.getItem(MISSION_KEY);
      if (!raw) return;
      payload = JSON.parse(raw);
    } catch {
      return;
    }
    if (!payload) return;

    // Drop stale missions (> 30 min old) so an old visit doesn't reload.
    if (payload.ts && Date.now() - payload.ts > MISSION_TTL_MS) {
      try { localStorage.removeItem(MISSION_KEY); } catch {}
      return;
    }

    const wps = (payload.waypoints || []).filter(
      w => Number.isFinite(w.lat) && Number.isFinite(w.lon)
    );
    if (!wps.length) return;

    setWaypoints(wps);
    setHandoffNote(
      `${wps.length} waypoint${wps.length > 1 ? 's' : ''} loaded from Mission Planner`
    );

    // Fly once the globe viewer is ready, then auto-run the assessment.
    const flyTimer = setTimeout(() => globeRef.current?.flyToRoute(wps), 600);
    const noteTimer = setTimeout(() => setHandoffNote(null), 6000);

    let cancelled = false;
    // Run the SAME mission-aware assessment the planner uses (v3), passing the
    // full carried profile (mission_type, gnss/comms dependence, risk tolerance,
    // equipment, scenario). The globe colors markers from decision.waypoints, but
    // v3 nests the per-waypoint array under raw_decision.waypoints — lift it.
    const { ts: _ts, ...profile } = payload; // drop the handoff timestamp
    api.missionAssess({ ...profile, waypoints: wps })
      .then(a => {
        if (cancelled || !a) return;
        setDecision({ ...a, waypoints: a.raw_decision?.waypoints ?? [] });
      })
      .catch(() => {}); // errors surface via Panel when the user re-runs

    return () => {
      cancelled = true;
      clearTimeout(flyTimer);
      clearTimeout(noteTimer);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []); // run once on mount

  // ── Persist waypoints back to the shared store ────────────────────────────
  // So edits made on the globe survive the trip back to /mission. Merge into
  // any existing profile (mission_type, dependencies, …) rather than replacing
  // it. Skip the first render so we don't clobber a stored mission before the
  // load effect above has read it.
  const didHydrate = useRef(false);
  useEffect(() => {
    if (!didHydrate.current) { didHydrate.current = true; return; }
    try {
      let existing = {};
      try { existing = JSON.parse(localStorage.getItem(MISSION_KEY)) || {}; } catch {}
      if (waypoints.length) {
        localStorage.setItem(MISSION_KEY, JSON.stringify({ ...existing, waypoints, ts: Date.now() }));
      } else if (localStorage.getItem(MISSION_KEY)) {
        // All waypoints cleared on the globe — reflect that, keep the profile.
        localStorage.setItem(MISSION_KEY, JSON.stringify({ ...existing, waypoints: [], ts: Date.now() }));
      }
    } catch { /* localStorage unavailable — non-fatal */ }
  }, [waypoints]);

  // ── Map interaction ───────────────────────────────────────────────────────
  const handleMapClick = useCallback(({ lat, lon }) => {
    if (!clickMode) return;
    const name = `WP${String(waypoints.length + 1).padStart(2, '0')}`;
    const newWPs = [...waypoints, { lat, lon, name }];
    setWaypoints(newWPs);
    setClickMode(false);
    // Auto-fly: zoom to the newly placed point (1 WP) or fit the route (≥2)
    if (newWPs.length === 1) {
      globeRef.current?.flyTo(lat, lon, 2_500_000);
    } else {
      globeRef.current?.flyToRoute(newWPs);
    }
  }, [clickMode, waypoints]);

  const handleFlyToRoute = useCallback(() => {
    if (waypoints.length) globeRef.current?.flyToRoute(waypoints);
  }, [waypoints]);

  // ── Waypoint list changes (Panel form) ────────────────────────────────────
  const handleWaypointsChange = useCallback((wps) => {
    setWaypoints(prev => {
      // Auto-fly when a waypoint is added (not on remove / clear)
      if (wps.length > prev.length) {
        if (wps.length === 1) {
          const { lat, lon } = wps[0];
          globeRef.current?.flyTo(lat, lon, 2_500_000);
        } else {
          globeRef.current?.flyToRoute(wps);
        }
      }
      return wps;
    });
    setDecision(null);
  }, []);

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

          {/* Mission Planner handoff confirmation */}
          {handoffNote && (
            <div className="globe-hint" role="status">
              {handoffNote}
            </div>
          )}

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
          onWaypointsChange={handleWaypointsChange}
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
