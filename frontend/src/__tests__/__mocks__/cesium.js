/**
 * Cesium mock for Vitest / jsdom.
 * CesiumJS requires WebGL and a browser — replace the whole module
 * with no-op stubs so tests that import it don't crash.
 */
export const Viewer = vi.fn(() => ({
  dataSources:           { add: vi.fn() },
  scene:                 { backgroundColor: {}, globe: { baseColor: {}, enableLighting: true, showGroundAtmosphere: true }, skyAtmosphere: { show: true }, skyBox: { show: true } },
  camera:                { setView: vi.fn(), flyTo: vi.fn(), flyToBoundingSphere: vi.fn() },
  canvas:                { style: {} },
  screenSpaceEventHandler: { setInputAction: vi.fn() },
  destroy: vi.fn(),
}));
export const CustomDataSource  = vi.fn(() => ({ entities: { removeAll: vi.fn(), add: vi.fn() } }));
export const Cartesian3        = { fromDegrees: vi.fn(() => ({})), fromDegreesArray: vi.fn(() => []) };
export const Cartographic      = { fromCartesian: vi.fn(() => ({ latitude: 0, longitude: 0 })) };
export const BoundingSphere    = { fromPoints: vi.fn(() => ({ radius: 1000 })) };
export const Color             = vi.fn(() => ({ withAlpha: vi.fn().mockReturnThis() }));
Color.WHITE  = {};
Color.BLACK  = {};
Color.fromCssColorString = vi.fn(() => ({ withAlpha: vi.fn().mockReturnThis() }));
export const ConstantProperty             = vi.fn();
export const PolylineGlowMaterialProperty = vi.fn();
export const HeadingPitchRange            = vi.fn();
export const Cartesian2                   = vi.fn();
export const Rectangle                    = { fromDegrees: vi.fn(() => ({})) };
export const VerticalOrigin  = { BOTTOM: 0, TOP: 1 };
export const HeightReference = { NONE: 0 };
export const LabelStyle      = { FILL_AND_OUTLINE: 0 };
export const ScreenSpaceEventType = { LEFT_CLICK: 0 };
export const Math = { toRadians: vi.fn(d => d * 0.0175), toDegrees: vi.fn(r => r * 57.3) };
export const Ion = { defaultAccessToken: '' };
export const EllipsoidTerrainProvider = vi.fn();
export const OpenStreetMapImageryProvider = vi.fn();
