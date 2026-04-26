/**
 * useForecast.js
 * Fetches the 72-hour Kp forecast from GET /api/forecast on mount.
 * Returns { forecast, loading } where forecast.windows is an array of
 * { label, horizon_h, kp_forecast, risk_level, gps_impact, hf_impact } objects.
 */

import { useState, useEffect } from 'react';
import { api } from '../utils/api.js';

export function useForecast() {
  const [forecast, setForecast] = useState(null);
  const [loading,  setLoading]  = useState(true);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const data = await api.forecast();
        if (!cancelled) {
          setForecast(data);
          setLoading(false);
        }
      } catch {
        if (!cancelled) setLoading(false);
      }
    }

    load();
    return () => { cancelled = true; };
  }, []);

  return { forecast, loading };
}
