/**
 * Globe.jsx
 * CesiumJS 3D globe wrapper for React.
 *
 * Responsibilities:
 *   • Initialize and own the Cesium Viewer lifecycle
 *   • Re-render route polyline + waypoint markers when waypoints change
 *   • Re-render data-layer entities when layer state changes
 *   • Update marker colours when a route decision arrives
 *   • Forward flyTo / flyToRoute imperative commands from parent
 *   • Surface map-click coordinates via onMapClick callback
 *
 * Performance strategy
 * ─────────────────────
 *   requestRenderMode=true  — Cesium only renders on data change or camera movement;
 *                             idle scene costs ~0% GPU (vs. 60fps constant draw).
 *   targetFrameRate=30      — cap the active render rate; halves GPU load during
 *                             interaction compared to the 60fps default.
 *   suspendEvents/resumeEvents — batch entity mutations into one dirty notification
 *                             instead of one per entity add/remove.
 *   Page Visibility API     — pause the render loop entirely when the browser tab
 *                             is hidden; resume + force one render on tab focus.
 */

import { useEffect, useRef, forwardRef, useImperativeHandle } from 'react';
import * as Cesium from 'cesium';
import 'cesium/Build/Cesium/Widgets/widgets.css';
import {
  buildRiskSegments,
  buildWaypointEntity,
  buildAltitudeStick,
  buildTecBands,
  hexColor,
} from '../utils/cesiumHelpers.js';

// Cesium Ion token (optional). Set VITE_CESIUM_TOKEN in .env.local for
// Bing Maps imagery and terrain. Falls back to OSM + flat ellipsoid.
Cesium.Ion.defaultAccessToken = import.meta.env.VITE_CESIUM_TOKEN || '';

const Globe = forwardRef(function Globe(
  { waypoints, decision, layers, onMapClick, clickMode, forecastKp },
  ref,
) {
  const containerRef   = useRef(null);
  const viewerRef      = useRef(null);
  const routeDSRef     = useRef(null);  // CustomDataSource for route entities
  const layerDSRef     = useRef(null);  // CustomDataSource for layer entities

  // ── Imperative API ──────────────────────────────────────────────────────────
  // Camera-safety rule: never land in the surface, but stop close enough to
  // see real geographic detail (state/country scale, not hemisphere).
  //
  // MIN_RANGE_M  — minimum camera-to-target distance (~600 km). Below this,
  //                the original "lost in a blank background" bug starts — for
  //                a 1-WP route, BoundingSphere.radius is 0 and the legacy
  //                offset of `radius * 3.8` collapsed to 0. 600 km is roughly
  //                ISS altitude — close enough to see a region, far enough
  //                to keep geography readable.
  // SAFE_PITCH   — −35° gives an aerial-oblique look; straight-down (-90°) is
  //                disorienting on a globe and was a frequent complaint.
  const MIN_RANGE_M = 600_000;
  const SAFE_PITCH  = -Cesium.Math.toRadians(35);

  useImperativeHandle(ref, () => ({
    /** Fly the camera to a lon/lat position with an oblique aerial view. */
    flyTo(lat, lon, altM = 900_000) {
      const v = viewerRef.current;
      if (!v) return;
      // Use flyToBoundingSphere (with a degenerate sphere centred on the
      // target) so we get a consistent oblique pitch instead of a
      // straight-down look-at.
      const target = Cesium.Cartesian3.fromDegrees(lon, lat);
      const bs = new Cesium.BoundingSphere(target, 1);
      v.camera.flyToBoundingSphere(bs, {
        duration: 1.4,
        offset: new Cesium.HeadingPitchRange(0, SAFE_PITCH, Math.max(altM, MIN_RANGE_M)),
      });
    },
    /** Fit the camera to the bounding sphere of an array of waypoints. */
    flyToRoute(wps) {
      const v = viewerRef.current;
      if (!wps.length || !v) return;
      const pts = wps.map(wp => Cesium.Cartesian3.fromDegrees(wp.lon, wp.lat));
      const bs  = Cesium.BoundingSphere.fromPoints(pts);
      // Single-point bs has radius 0 → offset would also be 0 → camera dives
      // into the surface. Floor the range so we always land at a useful
      // aerial view above the point.
      const range = Math.max(bs.radius * 3.8, MIN_RANGE_M);
      v.camera.flyToBoundingSphere(bs, {
        duration: 1.4,
        offset: new Cesium.HeadingPitchRange(0, SAFE_PITCH, range),
      });
    },
  }), []);

  // ── Initialise Cesium (once) ────────────────────────────────────────────────
  useEffect(() => {
    if (!containerRef.current) return;

    // ── Cesium Viewer ────────────────────────────────────────────────────────
    // Note: `imageryProvider` on the Viewer constructor was deprecated in
    // Cesium 1.104. We pass `baseLayer: false` and add imagery manually so
    // the real Earth surface is always visible regardless of token status.
    const viewer = new Cesium.Viewer(containerRef.current, {
      baseLayer:              false,          // suppress default imagery; we add our own
      terrainProvider:        new Cesium.EllipsoidTerrainProvider(),
      baseLayerPicker:        false,
      geocoder:               false,
      homeButton:             false,
      sceneModePicker:        false,
      navigationHelpButton:   false,
      animation:              false,
      timeline:               false,
      fullscreenButton:       false,
      infoBox:                false,
      selectionIndicator:     false,
      creditContainer: Object.assign(document.createElement('div'), { style: 'display:none' }),

      // ── Performance ───────────────────────────────────────────────────────
      // Only redraw when data changes or the camera moves — saves ~100% GPU on
      // an idle scene.  Call scene.requestRender() after any programmatic change.
      requestRenderMode:       true,
      maximumRenderTimeChange: 0.0,

      // Cap active render rate at 30 fps (halves GPU load vs Cesium default of 60).
      targetFrameRate:         30,
    });

    // ── Base imagery ─────────────────────────────────────────────────────────
    // Layer 0: bundled NaturalEarthII tileset (ships inside the Cesium build —
    // zero network). Guarantees a readable globe in air-gapped / disconnected
    // deployments where OSM tiles can't load. Failed OSM tiles are transparent,
    // so this layer shows through automatically.
    Cesium.TileMapServiceImageryProvider.fromUrl(
      Cesium.buildModuleUrl('Assets/Textures/NaturalEarthII'),
    )
      .then(offlineProvider => {
        if (!viewerRef.current) return;
        // Insert at index 0 so OSM/Ion (added synchronously below) stay on top.
        viewerRef.current.imageryLayers.add(new Cesium.ImageryLayer(offlineProvider), 0);
        viewerRef.current.scene.requestRender();
      })
      .catch(() => { /* bundled asset missing — OSM still covers connected use */ });

    // Layer 1: OpenStreetMap tiles via UrlTemplateImageryProvider
    // (the non-deprecated path for Cesium 1.104+).
    // If an Ion token is set, also attempt to swap in Bing/Ion imagery.
    const osmProvider = new Cesium.UrlTemplateImageryProvider({
      url:          'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
      credit:       '© OpenStreetMap contributors',
      maximumLevel: 19,
    });
    viewer.imageryLayers.add(new Cesium.ImageryLayer(osmProvider));

    // Optional: upgrade to Ion World Imagery when a token is configured
    if (Cesium.Ion.defaultAccessToken) {
      Cesium.IonImageryProvider.fromAssetId(2)   // asset 2 = Bing Maps Aerial
        .then(provider => {
          if (!viewerRef.current) return;
          viewerRef.current.imageryLayers.removeAll();
          viewerRef.current.imageryLayers.add(new Cesium.ImageryLayer(provider));
        })
        .catch(() => { /* OSM already added above — stay on it */ });
    }

    // ── Scene aesthetics ─────────────────────────────────────────────────────
    viewer.scene.backgroundColor        = new Cesium.Color(0.03, 0.06, 0.12, 1);
    viewer.scene.globe.baseColor        = new Cesium.Color(0.05, 0.09, 0.16, 1);
    // enableLighting=false keeps the whole globe lit so OSM tiles are readable
    viewer.scene.globe.enableLighting   = false;
    viewer.scene.skyAtmosphere.show     = true;
    viewer.scene.skyBox.show            = true;

    // ── Scene performance tweaks ─────────────────────────────────────────────
    // Disable atmospheric fog (expensive GLSL; not needed for tactical display)
    viewer.scene.fog.enabled            = false;
    // Disable water effect (requires extra shader pass)
    viewer.scene.globe.showWaterEffect  = false;

    // ── Camera safety ────────────────────────────────────────────────────────
    // Floor manual zoom-in so the user can never end up "inside" the surface,
    // but allow getting genuinely close — 150 km altitude is roughly aircraft
    // cruise + a bit, lets users inspect a city block while still keeping
    // geographic context.
    viewer.scene.screenSpaceCameraController.minimumZoomDistance = 150_000;
    // Cap zoom-out at geosynchronous-ish range so the globe stays in frame.
    viewer.scene.screenSpaceCameraController.maximumZoomDistance = 60_000_000;

    // Don't auto-track entities on click — Cesium's default behaviour pinches
    // the camera into the entity, which was a frequent complaint.
    viewer.trackedEntity  = undefined;
    viewer.selectedEntity = undefined;

    // Initial camera position (whole Earth view)
    viewer.camera.setView({
      destination: Cesium.Cartesian3.fromDegrees(10, 28, 20_000_000),
    });

    // Route data source
    const routeDS = new Cesium.CustomDataSource('route');
    viewer.dataSources.add(routeDS);
    routeDSRef.current = routeDS;

    // Layer data source
    const layerDS = new Cesium.CustomDataSource('layers');
    viewer.dataSources.add(layerDS);
    layerDSRef.current = layerDS;

    // Map click → surface lat/lon → onMapClick callback
    viewer.screenSpaceEventHandler.setInputAction((evt) => {
      // Use a ref-captured callback to avoid stale closure issues
      const pos = viewer.camera.pickEllipsoid(
        evt.position,
        viewer.scene.globe.ellipsoid,
      );
      if (!pos) return;
      const carto = Cesium.Cartographic.fromCartesian(pos);
      const lat   = +Cesium.Math.toDegrees(carto.latitude).toFixed(4);
      const lon   = +Cesium.Math.toDegrees(carto.longitude).toFixed(4);
      onMapClickRef.current?.({ lat, lon });
    }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

    viewerRef.current = viewer;

    // ── Page Visibility API ──────────────────────────────────────────────────
    // Pause Cesium's render loop when the tab is hidden (saves 100% GPU).
    // Resume and force a single render when the tab becomes visible again.
    function onVisibilityChange() {
      if (!viewerRef.current) return;
      if (document.hidden) {
        viewerRef.current.useDefaultRenderLoop = false;
      } else {
        viewerRef.current.useDefaultRenderLoop = true;
        viewerRef.current.scene.requestRender();
      }
    }
    document.addEventListener('visibilitychange', onVisibilityChange);

    return () => {
      document.removeEventListener('visibilitychange', onVisibilityChange);
      viewer.destroy();
      viewerRef.current  = null;
      routeDSRef.current = null;
      layerDSRef.current = null;
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps — intentionally once

  // Keep the map-click callback in a ref so the Cesium event handler
  // always invokes the latest closure without re-running the init effect.
  const onMapClickRef = useRef(onMapClick);
  useEffect(() => { onMapClickRef.current = onMapClick; }, [onMapClick]);

  // ── Cursor changes with clickMode ───────────────────────────────────────────
  useEffect(() => {
    if (!viewerRef.current) return;
    viewerRef.current.canvas.style.cursor = clickMode ? 'crosshair' : 'default';
  }, [clickMode]);

  // ── Route + waypoint markers ────────────────────────────────────────────────
  useEffect(() => {
    const ds = routeDSRef.current;
    if (!ds) return;

    // Batch all entity mutations — prevents N separate dirty notifications
    // (one per entity add/remove) when Cesium is in requestRenderMode.
    ds.entities.suspendEvents();
    ds.entities.removeAll();

    if (waypoints.length) {
      const decisionWps = decision?.waypoints ?? [];
      buildRiskSegments(waypoints, decisionWps).forEach(seg => ds.entities.add(seg));
      waypoints.forEach((wp, i) => {
        const riskLevel = decisionWps[i]?.risk_level ?? null;
        ds.entities.add(buildWaypointEntity(wp, i, riskLevel));
        ds.entities.add(buildAltitudeStick(wp));
      });
    }

    ds.entities.resumeEvents();
    // requestRenderMode requires an explicit nudge after programmatic changes
    viewerRef.current?.scene.requestRender();
  }, [waypoints, decision]);

  // ── Data layers ─────────────────────────────────────────────────────────────
  useEffect(() => {
    const ds = layerDSRef.current;
    if (!ds) return;

    ds.entities.suspendEvents();
    ds.entities.removeAll();

    // 1. Ionosphere TEC bands
    // forecastKp overrides the live status Kp when the timeline slider is scrubbed
    const effectiveKp = forecastKp ?? layers.tec.kp;
    if (layers.tec.visible && effectiveKp != null) {
      buildTecBands(effectiveKp).forEach(band => {
        ds.entities.add({
          rectangle: {
            coordinates: Cesium.Rectangle.fromDegrees(-180, band.latMin, 180, band.latMax),
            material:    hexColor(band.color, band.alpha),
          },
        });
      });
    }

    // 2. Polar Cap Absorption
    if (layers.pca.visible && layers.pca.active) {
      [{ latMin: 65, latMax: 90 }, { latMin: -90, latMax: -65 }].forEach(({ latMin, latMax }) => {
        ds.entities.add({
          rectangle: {
            coordinates: Cesium.Rectangle.fromDegrees(-180, latMin, 180, latMax),
            material:    Cesium.Color.fromCssColorString('#EF4444').withAlpha(0.30),
            outline:     true,
            outlineColor: Cesium.Color.fromCssColorString('#EF4444').withAlpha(0.7),
            outlineWidth: 2,
          },
        });
      });
      // Label at north pole
      ds.entities.add({
        position: Cesium.Cartesian3.fromDegrees(0, 72, 300_000),
        label: {
          text:  'POLAR CAP ABSORPTION ACTIVE',
          font:  'bold 12px system-ui, sans-serif',
          fillColor:   Cesium.Color.fromCssColorString('#F87171'),
          outlineColor: Cesium.Color.BLACK,
          outlineWidth: 2,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
      });
    }

    // 3. Solar wind speed indicator
    if (layers.solarWind.visible && layers.solarWind.speed != null) {
      const spd   = layers.solarWind.speed;
      const color = spd > 700 ? '#EF4444' : spd > 500 ? '#F97316' : spd > 400 ? '#F59E0B' : '#10B981';
      ds.entities.add({
        position: Cesium.Cartesian3.fromDegrees(-80, 5, 800_000),
        label: {
          text:        `☀  Solar Wind  ${spd.toFixed(0)} km/s`,
          font:        'bold 13px system-ui, sans-serif',
          fillColor:   Cesium.Color.fromCssColorString(color),
          outlineColor: Cesium.Color.BLACK,
          outlineWidth: 2,
          style:       Cesium.LabelStyle.FILL_AND_OUTLINE,
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
          showBackground: true,
          backgroundColor: new Cesium.Color(0, 0, 0, 0.5),
          backgroundPadding: new Cesium.Cartesian2(8, 4),
        },
      });
    }
    ds.entities.resumeEvents();
    viewerRef.current?.scene.requestRender();
  }, [layers, forecastKp]);

  return (
    <div
      ref={containerRef}
      style={{ width: '100%', height: '100%' }}
      role="application"
      aria-label="IonShield 3D globe — click to place waypoints when in click mode"
      tabIndex={0}
    />
  );
});

export default Globe;
