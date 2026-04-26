/**
 * useStatus.js
 * Polls GET /api/status on a configurable interval and returns the
 * latest solar driver data plus global risk level.
 */

import { useState, useEffect } from 'react';
import { api } from '../utils/api.js';

export function useStatus(intervalMs = 60_000) {
  const [status,  setStatus]  = useState(null);
  const [loading, setLoading] = useState(true);
  const [error,   setError]   = useState(null);

  useEffect(() => {
    let mounted = true;

    async function fetchStatus() {
      try {
        const data = await api.status();
        if (mounted) { setStatus(data); setError(null); }
      } catch (err) {
        if (mounted) setError(err.message);
      } finally {
        if (mounted) setLoading(false);
      }
    }

    fetchStatus();
    const id = setInterval(fetchStatus, intervalMs);
    return () => { mounted = false; clearInterval(id); };
  }, [intervalMs]);

  return { status, loading, error };
}
