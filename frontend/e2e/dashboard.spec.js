// @ts-check
/**
 * E2E smoke tests — 3D Dashboard and route-decision flow.
 *
 * CesiumJS loads asynchronously so we wait for the canvas element.
 * We stub the heavy /api/* calls with route interception so tests
 * are deterministic regardless of live NOAA conditions.
 */

import { test, expect } from '@playwright/test';

// ── Fixtures: deterministic API stubs ─────────────────────────────────────────

const STATUS_STUB = {
  global_risk_level: 'NOMINAL',
  data_age_seconds:  60,
  solar_drivers: {
    kp_current:         1.5,
    bz_nt:             -2.0,
    xray_class:         'A1.2',
    solar_wind_km_s:    380,
    proton_flux_10mev_pfu: 0.1,
  },
  feed_status: { kp: 'ok', xray: 'ok', wind: 'ok' },
};

const FORECAST_STUB = {
  windows: [
    { label: 'Now',  kp_forecast: 1.5, start_utc: '2026-04-25T00:00:00Z', end_utc: '2026-04-25T06:00:00Z', operational_window: true },
    { label: '+6h',  kp_forecast: 2.0, start_utc: '2026-04-25T06:00:00Z', end_utc: '2026-04-25T12:00:00Z', operational_window: true },
    { label: '+12h', kp_forecast: 3.5, start_utc: '2026-04-25T12:00:00Z', end_utc: '2026-04-25T18:00:00Z', operational_window: false },
  ],
};

const ROUTE_DECISION_STUB = {
  action:          'GO',
  action_sentence: 'All waypoints nominal — GPS L1 error < 10 m. Safe to proceed.',
  decision_type:   'ROUTE_RISK',
  confidence: { score: 0.91, label: 'HIGH', drivers: [], stale_data: false, data_completeness: 1.0, computed_at: '2026-04-25T00:00:00Z' },
  provenance: { model_version: '1.0.0', input_hash: 'sha256:abc123', computed_at: '2026-04-25T00:00:00Z', observations_used: ['kp_index'], feeds_unavailable: [] },
  waypoints: [
    { name: 'WP01', lat: 38.8, lon: -77.0, risk_level: 'NOMINAL', risk_score: 5, gps_error_m: 8, hf_viable: true, pca_active: false, watch_notes: '' },
    { name: 'WP02', lat: 39.1, lon: -76.8, risk_level: 'NOMINAL', risk_score: 6, gps_error_m: 9, hf_viable: true, pca_active: false, watch_notes: '' },
  ],
  alternatives: [],
  recommended_actions: ['Standard operations.'],
};

// ── Helper: intercept all API calls with stubs ────────────────────────────────

async function stubApis(page) {
  await page.route('**/api/status',             r => r.fulfill({ json: STATUS_STUB }));
  await page.route('**/api/forecast',           r => r.fulfill({ json: FORECAST_STUB }));
  await page.route('**/overlay/risk.geojson',   r => r.fulfill({ json: { type: 'FeatureCollection', features: [] } }));
  await page.route('**/api/v2/route-decision',  r => r.fulfill({ status: 200, json: ROUTE_DECISION_STUB }));
  await page.route('**/api/v2/snapshots**',     r => r.fulfill({ json: { count: 0, limit: 50, offset: 0, snapshots: [] } }));
  await page.route('**/api/v2/replay**',        r => r.fulfill({ json: ROUTE_DECISION_STUB }));
}

// ── Tests ─────────────────────────────────────────────────────────────────────

test('dashboard mounts React app', async ({ page }) => {
  await stubApis(page);
  await page.goto('/dashboard');
  // React root element is present
  await expect(page.locator('#root')).toBeAttached();
  // Cesium canvas renders
  await expect(page.locator('canvas')).toBeAttached({ timeout: 15_000 });
});

test('header shows live solar-driver chips', async ({ page }) => {
  await stubApis(page);
  await page.goto('/dashboard');
  // Wait for React to render (status hook fires immediately via stub)
  await expect(page.locator('.header-drivers')).toBeVisible({ timeout: 10_000 });
  // Kp chip should contain "1.5"
  await expect(page.locator('.driver-chip-value').first()).toContainText('1.5');
});

test('header shows NOMINAL risk badge', async ({ page }) => {
  await stubApis(page);
  await page.goto('/dashboard');
  await expect(page.locator('.risk-badge')).toContainText('NOMINAL', { timeout: 10_000 });
});

test('panel renders waypoint builder', async ({ page }) => {
  await stubApis(page);
  await page.goto('/dashboard');
  // The panel aside is present
  await expect(page.locator('aside.panel')).toBeVisible({ timeout: 10_000 });
  // Lat/lon inputs
  await expect(page.locator('input[aria-label="Latitude"]')).toBeVisible();
  await expect(page.locator('input[aria-label="Longitude"]')).toBeVisible();
});

test('can add a waypoint via the form', async ({ page }) => {
  await stubApis(page);
  await page.goto('/dashboard');
  await page.waitForSelector('aside.panel', { timeout: 10_000 });

  await page.fill('input[aria-label="Latitude"]',  '38.8');
  await page.fill('input[aria-label="Longitude"]', '-77.0');
  await page.click('[data-testid="wp-add-btn"]');

  // One .wp-row entry should appear in the waypoint list
  await expect(page.locator('.wp-row')).toHaveCount(1, { timeout: 5_000 });
});

test('route-decision flow returns GO and shows result', async ({ page }) => {
  // Capture any JS errors that crash React (black screen)
  const jsErrors = [];
  page.on('pageerror', err => jsErrors.push(err.message));
  page.on('console', msg => { if (msg.type() === 'error') jsErrors.push(msg.text()); });

  await stubApis(page);
  await page.goto('/dashboard');
  await page.waitForSelector('aside.panel', { timeout: 10_000 });

  // Add two waypoints — use data-testid to target the Add button precisely and
  // avoid any ambiguous selector matching.  After each Add the form clears itself
  // via React state; we wait for the inputs to reach '' before re-filling so we
  // don't race the React state flush.
  const addBtn = page.locator('[data-testid="wp-add-btn"]');

  await page.fill('input[aria-label="Latitude"]',  '38.8');
  await page.fill('input[aria-label="Longitude"]', '-77.0');
  await addBtn.click();
  await expect(page.locator('.wp-row')).toHaveCount(1, { timeout: 5_000 });

  // Wait for React to flush the clear — inputs must be empty before re-filling
  await expect(page.locator('input[aria-label="Latitude"]')).toHaveValue('', { timeout: 3_000 });
  await expect(page.locator('input[aria-label="Longitude"]')).toHaveValue('', { timeout: 3_000 });

  await page.fill('input[aria-label="Latitude"]',  '39.1');
  await page.fill('input[aria-label="Longitude"]', '-76.8');
  await addBtn.click();
  // If this fails, jsErrors will contain the React crash reason
  await expect(page.locator('.wp-row'), `JS errors: ${jsErrors.join('; ')}`).toHaveCount(2, { timeout: 5_000 });

  // Click "Get Route Decision" — button is now enabled (2 waypoints)
  const decisionBtn = page.getByRole('button', { name: /Get Route Decision/i });
  await expect(decisionBtn).toBeEnabled({ timeout: 5_000 });
  await decisionBtn.click();

  // Wait for the decision result to appear
  const result = page.locator('[data-testid="decision-result"]');
  await expect(result).toBeVisible({ timeout: 10_000 });

  // Action sentence text
  await expect(page.locator('[data-testid="action-sentence"]'))
    .toContainText('nominal', { timeout: 5_000 });
});

test('replay drawer opens and closes', async ({ page }) => {
  await stubApis(page);
  await page.goto('/dashboard');
  await page.waitForSelector('header.header', { timeout: 10_000 });

  // Open replay drawer
  await page.click('button:text("Replay")');
  await expect(page.locator('.replay-drawer')).toBeVisible({ timeout: 5_000 });

  // Close it
  await page.click('.replay-close-btn');
  await expect(page.locator('.replay-drawer')).not.toBeVisible({ timeout: 5_000 });
});

test('layer toggles are present and interactive', async ({ page }) => {
  await stubApis(page);
  await page.goto('/dashboard');
  await page.waitForSelector('.layer-row', { timeout: 10_000 });

  const toggles = page.locator('.layer-toggle-switch');
  // Three toggles: TEC, PCA, Solar Wind
  await expect(toggles).toHaveCount(3);

  // Click the first toggle — should change aria-checked
  const firstRow = page.locator('.layer-row').first();
  const beforeState = await firstRow.getAttribute('aria-checked');
  await firstRow.click();
  const afterState = await firstRow.getAttribute('aria-checked');
  expect(beforeState).not.toBe(afterState);
});

test('forecast timeline slider renders windows', async ({ page }) => {
  await stubApis(page);
  await page.goto('/dashboard');
  // Timeline bar should appear with the 3 stubbed windows
  await expect(page.locator('.tl-win')).toHaveCount(3, { timeout: 10_000 });
});

test('help modal opens and closes', async ({ page }) => {
  await stubApis(page);
  await page.goto('/dashboard');
  await page.waitForSelector('header.header', { timeout: 10_000 });

  // Open help
  await page.click('button:text("Help")');
  await expect(page.locator('.modal-box')).toBeVisible({ timeout: 5_000 });

  // Close via × button
  await page.click('.modal-close');
  await expect(page.locator('.modal-box')).not.toBeVisible({ timeout: 3_000 });
});

// ── API endpoint smoke tests (via fetch, not browser) ─────────────────────────

test('GET /api/status returns global_risk_level', async ({ request }) => {
  const res = await request.get('/api/status');
  expect(res.status()).toBe(200);
  const body = await res.json();
  expect(body).toHaveProperty('global_risk_level');
  expect(body).toHaveProperty('solar_drivers');
});

test('GET /api/forecast returns windows array', async ({ request }) => {
  const res = await request.get('/api/forecast');
  expect(res.status()).toBe(200);
  const body = await res.json();
  expect(body).toHaveProperty('windows');
  expect(Array.isArray(body.windows)).toBe(true);
});

test('POST /api/v2/route-decision with valid payload returns action', async ({ request }) => {
  const res = await request.post('/api/v2/route-decision', {
    data: {
      waypoints: [
        { lat: 38.8, lon: -77.0, name: 'WP01' },
        { lat: 39.1, lon: -76.8, name: 'WP02' },
      ],
      platform: { asset_type: 'GPS_L1', criticality: 3, system_dependencies: [] },
    },
  });
  expect(res.status()).toBe(200);
  const body = await res.json();
  expect(body).toHaveProperty('action');
  expect(body).toHaveProperty('waypoints');
  expect(body.waypoints).toHaveLength(2);
});

test('GET /api/v2/snapshots returns paginated list', async ({ request }) => {
  const res = await request.get('/api/v2/snapshots?limit=5');
  expect(res.status()).toBe(200);
  const body = await res.json();
  expect(body).toHaveProperty('count');
  expect(body).toHaveProperty('snapshots');
  expect(Array.isArray(body.snapshots)).toBe(true);
});

test('POST /api/v2/contact returns 201 for valid submission', async ({ request }) => {
  const res = await request.post('/api/v2/contact', {
    data: {
      org:      'Playwright API Test',
      email:    'playwright@test.example',
      sector:   'Research / Academic',
      interest: 'Automated API test — ignore.',
      website:  '',
    },
  });
  expect(res.status()).toBe(201);
  const body = await res.json();
  expect(body.status).toBe('submitted');
});
