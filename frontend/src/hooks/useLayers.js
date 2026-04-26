/**
 * useLayers.js
 * Manages the visibility and live data for the three Cesium data layers:
 *   • tec        — Ionosphere TEC (estimated from Kp)
 *   • pca        — Polar Cap Absorption (from proton flux)
 *   • solarWind  — Solar wind speed overlay
 *
 * Status data from useStatus() is merged in so the layers reflect live values.
 */

import { useState, useCallback, useEffect } from 'react';

const LAYER_DEFS = {
  tec: {
    label:       'Ionosphere TEC',
    description: 'Estimated total electron content disturbance based on Kp index. '
                + 'Auroral ovals expand equatorward as Kp rises.',
    visible: true,
    kp: null,
  },
  pca: {
    label:       'Polar Cap Absorption',
    description: 'Red polar cap overlay active when proton flux indicates polar '
                + 'cap absorption — HF blackout at high latitudes.',
    visible: true,
    active: false,
  },
  solarWind: {
    label:       'Solar Wind Alert',
    description: 'Current solar wind speed indicator. >700 km/s is severe; '
                + '400–700 km/s is enhanced.',
    visible: true,
    speed: null,
  },
};

export function useLayers(status) {
  const [layers, setLayers] = useState(LAYER_DEFS);

  // Sync live data values from the status API into layer state
  useEffect(() => {
    if (!status?.solar_drivers) return;
    const d = status.solar_drivers;
    setLayers(prev => ({
      ...prev,
      tec:       { ...prev.tec,       kp:    d.kp_current    ?? null },
      pca:       { ...prev.pca,       active: d.pca_active   ?? false },
      solarWind: { ...prev.solarWind, speed:  d.solar_wind_km_s ?? null },
    }));
  }, [status]);

  /** Toggle the visible flag for a layer by key. */
  const toggleLayer = useCallback((key) => {
    setLayers(prev => ({
      ...prev,
      [key]: { ...prev[key], visible: !prev[key].visible },
    }));
  }, []);

  return { layers, toggleLayer };
}
