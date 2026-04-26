/**
 * api.test.js
 * Tests for the API fetch wrapper — header injection, error handling,
 * and typed endpoint URL construction.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { apiFetch, getApiKey, setApiKey, api } from '../utils/api.js';

// ── Helpers ────────────────────────────────────────────────────────────────

function mockFetch(body, status = 200) {
  return vi.fn(() =>
    Promise.resolve({
      ok:   status >= 200 && status < 300,
      status,
      json: () => Promise.resolve(body),
      text: () => Promise.resolve(JSON.stringify(body)),
    }),
  );
}

// ── API key persistence ────────────────────────────────────────────────────

describe('API key', () => {
  afterEach(() => setApiKey(''));   // clean up

  it('stores and retrieves a key', () => {
    setApiKey('secret123');
    expect(getApiKey()).toBe('secret123');
  });

  it('clears key when empty string is set', () => {
    setApiKey('abc');
    setApiKey('');
    expect(getApiKey()).toBe('');
  });
});

// ── apiFetch header injection ──────────────────────────────────────────────

describe('apiFetch', () => {
  beforeEach(() => setApiKey(''));

  it('does not set X-API-Key when key is empty', async () => {
    global.fetch = mockFetch({ ok: true });
    await apiFetch('/api/test');
    const headers = global.fetch.mock.calls[0][1].headers;
    expect(headers['X-API-Key']).toBeUndefined();
  });

  it('sets X-API-Key header when key is configured', async () => {
    setApiKey('my-secret-key');
    global.fetch = mockFetch({ result: 1 });
    await apiFetch('/api/test');
    const headers = global.fetch.mock.calls[0][1].headers;
    expect(headers['X-API-Key']).toBe('my-secret-key');
    setApiKey('');
  });

  it('always sets Content-Type: application/json', async () => {
    global.fetch = mockFetch({});
    await apiFetch('/api/test');
    const headers = global.fetch.mock.calls[0][1].headers;
    expect(headers['Content-Type']).toBe('application/json');
  });

  it('throws an error with .status on non-2xx response', async () => {
    global.fetch = mockFetch({ detail: 'Not Found' }, 404);
    await expect(apiFetch('/api/missing')).rejects.toMatchObject({ status: 404 });
  });

  it('throws on 401 with status set', async () => {
    global.fetch = mockFetch({ detail: 'Unauthorized' }, 401);
    const err = await apiFetch('/api/secure').catch(e => e);
    expect(err.status).toBe(401);
    expect(err.message).toContain('401');
  });

  it('passes through fetch options', async () => {
    global.fetch = mockFetch({ id: 1 });
    await apiFetch('/api/route', { method: 'POST', body: '{}' });
    expect(global.fetch.mock.calls[0][1].method).toBe('POST');
  });
});

// ── Typed api helpers ──────────────────────────────────────────────────────

describe('api.routeDecision', () => {
  afterEach(() => setApiKey(''));

  it('posts to /api/v2/route-decision with correct payload', async () => {
    global.fetch = mockFetch({ action: 'GO' });
    const wps = [{ lat: 38.8, lon: -77.0, name: 'WP01' }];
    await api.routeDecision(wps, 'hmmwv');
    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toBe('/api/v2/route-decision');
    expect(opts.method).toBe('POST');
    const body = JSON.parse(opts.body);
    expect(body.waypoints).toEqual(wps);
    expect(body.platform).toBe('hmmwv');
  });
});

describe('api.commsDecision', () => {
  it('builds URL without dest when not provided', async () => {
    global.fetch = mockFetch({ action: 'USE_PRIMARY_HF' });
    await api.commsDecision(38.8, -77.0);
    const url = global.fetch.mock.calls[0][0];
    expect(url).toContain('lat=38.8');
    expect(url).toContain('lon=-77');
    expect(url).not.toContain('dest_lat');
  });

  it('includes dest params when provided', async () => {
    global.fetch = mockFetch({ action: 'USE_PRIMARY_HF' });
    await api.commsDecision(38.8, -77.0, 51.5, -0.1);
    const url = global.fetch.mock.calls[0][0];
    expect(url).toContain('dest_lat=51.5');
    expect(url).toContain('dest_lon=-0.1');
  });
});

describe('api.replay', () => {
  it('appends snapshot_id when provided', async () => {
    global.fetch = mockFetch({ action: 'GO' });
    await api.replay(38.8, -77.0, 42);
    expect(global.fetch.mock.calls[0][0]).toContain('snapshot_id=42');
  });

  it('URL-encodes the at parameter', async () => {
    global.fetch = mockFetch({ action: 'GO' });
    await api.replay(38.8, -77.0, null, '2024-05-11T18:00:00Z');
    const url = global.fetch.mock.calls[0][0];
    expect(url).toContain('at=');
    expect(url).not.toContain(':');   // colons encoded
  });
});
