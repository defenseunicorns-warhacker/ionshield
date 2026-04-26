/**
 * useSnapshots.js
 * Fetches paginated NOAA snapshots from /api/v2/snapshots.
 * Returns the accumulated list for infinite-scroll style loading.
 */

import { useState, useEffect, useCallback } from 'react';
import { api } from '../utils/api.js';

const PAGE_SIZE = 50;

export function useSnapshots(enabled = true) {
  const [snapshots, setSnapshots] = useState([]);
  const [total,     setTotal]     = useState(0);
  const [offset,    setOffset]    = useState(0);
  const [loading,   setLoading]   = useState(false);
  const [error,     setError]     = useState(null);

  const fetchPage = useCallback(async (pageOffset, reset) => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.snapshots(PAGE_SIZE, pageOffset);
      const rows  = data.snapshots ?? [];
      setTotal(data.count ?? 0);
      setSnapshots(prev => reset ? rows : [...prev, ...rows]);
      setOffset(pageOffset + rows.length);
    } catch (e) {
      setError(e.message || 'Failed to load snapshots');
    } finally {
      setLoading(false);
    }
  }, []);

  // Initial fetch whenever the drawer is opened (enabled flips true → false → true)
  useEffect(() => {
    if (!enabled) {
      setSnapshots([]);
      setOffset(0);
      setTotal(0);
      return;
    }
    fetchPage(0, true);
  }, [enabled, fetchPage]);

  const loadMore = useCallback(() => {
    if (!loading && offset < total) fetchPage(offset, false);
  }, [loading, offset, total, fetchPage]);

  const refresh = useCallback(() => fetchPage(0, true), [fetchPage]);

  return { snapshots, total, loading, error, hasMore: offset < total, loadMore, refresh };
}
