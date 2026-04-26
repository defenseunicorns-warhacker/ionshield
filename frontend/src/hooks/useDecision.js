/**
 * useDecision.js
 * Manages the async lifecycle of /api/v2/route-decision calls.
 * Returns { decision, loading, error, getRouteDecision, clearDecision }.
 */

import { useState, useCallback } from 'react';
import { api } from '../utils/api.js';

export function useDecision() {
  const [decision, setDecision] = useState(null);
  const [loading,  setLoading]  = useState(false);
  const [error,    setError]    = useState(null);

  const getRouteDecision = useCallback(async (waypoints, platform) => {
    if (!waypoints.length) {
      setError('Add at least one waypoint before requesting a decision.');
      return null;
    }
    setLoading(true);
    setError(null);
    try {
      const data = await api.routeDecision(waypoints, platform);
      setDecision(data);
      return data;
    } catch (err) {
      setError(err.message);
      setDecision(null);
      return null;
    } finally {
      setLoading(false);
    }
  }, []);

  const clearDecision = useCallback(() => {
    setDecision(null);
    setError(null);
  }, []);

  return { decision, loading, error, getRouteDecision, clearDecision };
}
