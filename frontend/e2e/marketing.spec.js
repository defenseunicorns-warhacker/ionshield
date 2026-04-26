// @ts-check
/**
 * E2E smoke tests — marketing funnel.
 *
 * Verifies all 7 public pages load correctly, contain key content,
 * and that cross-page navigation works.
 */

import { test, expect } from '@playwright/test';

// ── Page inventory ─────────────────────────────────────────────────────────────

const PAGES = [
  { path: '/',           title: /IonShield/i,       heading: /ionshield|space weather|mission/i },
  { path: '/features',   title: /features/i,        heading: /feature|capability/i },
  { path: '/demo',       title: /demo/i,             heading: /experience|route|check/i },
  { path: '/use-cases',  title: /use cases/i,        heading: /use case|operators|scenario/i },
  // Note: /docs now always serves the marketing page (Swagger moved to /api-docs).
  // Pattern also matches "documentation" to cover the full page title.
  { path: '/docs',       title: /docs|documentation|swagger/i,     heading: /learn|integrate|ionshield|swagger/i },
  { path: '/pricing',    title: /pricing/i,          heading: /pricing|pilot/i },
  { path: '/compliance', title: /compliance/i,       heading: /regulated|operating|compliance/i },
];

// ── 200 / content checks ───────────────────────────────────────────────────────

for (const { path, title, heading } of PAGES) {
  test(`${path} — loads with 200 and expected content`, async ({ page }) => {
    const res = await page.goto(path);
    expect(res?.status()).toBe(200);
    await expect(page).toHaveTitle(title);
    await expect(page.locator('h1, h2').first()).toContainText(heading);
  });
}

// ── Shared nav is injected on every page ─────────────────────────────────────

test('shared nav is present on landing page', async ({ page }) => {
  await page.goto('/');
  // nav.js injects a <nav> element
  const nav = page.locator('nav');
  await expect(nav).toBeVisible();
});

test('nav link to /features works', async ({ page }) => {
  await page.goto('/');
  // nav.js injects links; click the first visible "Features" link
  await page.locator('a[href="/features"]').first().click();
  await expect(page).toHaveURL('/features');
  expect((await page.title()).toLowerCase()).toContain('feature');
});

// ── robots.txt + sitemap.xml ─────────────────────────────────────────────────

test('robots.txt is served with correct content', async ({ page }) => {
  const res = await page.goto('/robots.txt');
  expect(res?.status()).toBe(200);
  const body = await page.content();
  expect(body).toContain('User-agent');
  expect(body).toContain('Disallow: /api/');
  expect(body).toContain('Sitemap:');
});

test('sitemap.xml contains all 7 marketing URLs', async ({ page }) => {
  const res = await page.goto('/sitemap.xml');
  expect(res?.status()).toBe(200);
  const body = await page.content();
  for (const p of ['/', '/features', '/demo', '/use-cases', '/docs', '/pricing', '/compliance']) {
    expect(body).toContain(p);
  }
});

// ── /dashboard redirect ───────────────────────────────────────────────────────

test('/dashboard serves the 3D app HTML', async ({ page }) => {
  const res = await page.goto('/dashboard');
  expect(res?.status()).toBe(200);
  // The SPA index.html mounts to #root
  await expect(page.locator('#root')).toBeAttached();
});

// ── /pricing contact form ─────────────────────────────────────────────────────

test('pricing page renders contact form with required fields', async ({ page }) => {
  await page.goto('/pricing');
  await expect(page.locator('#pilot-form')).toBeVisible();
  await expect(page.locator('#org')).toBeVisible();
  await expect(page.locator('#email')).toBeVisible();
  await expect(page.locator('#interest')).toBeVisible();
  await expect(page.locator('#submit-btn')).toBeVisible();
});

test('contact form shows success state after valid submission', async ({ page }) => {
  await page.goto('/pricing');

  // Wait for form to be ready
  await page.fill('#org',      'Playwright Test Org');
  await page.fill('#email',    'test@playwright.example');
  await page.fill('#interest', 'Automated E2E test submission — please ignore.');

  // Submit
  await page.click('#submit-btn');

  // Success div should appear
  await expect(page.locator('#form-success')).toBeVisible({ timeout: 10_000 });
  // Form should be hidden
  await expect(page.locator('#pilot-form')).toBeHidden();
});

test('contact form shows error for invalid email', async ({ page }) => {
  await page.goto('/pricing');

  await page.fill('#org',   'Test Org');
  await page.fill('#email', 'not-an-email');
  await page.click('#submit-btn');

  // Our client-side check fires before the API call
  await expect(page.locator('#form-error')).toBeVisible({ timeout: 5_000 });
});

test('contact form shows error for empty org', async ({ page }) => {
  await page.goto('/pricing');

  await page.fill('#email', 'valid@example.com');
  // Leave org empty
  await page.click('#submit-btn');

  await expect(page.locator('#form-error')).toBeVisible({ timeout: 5_000 });
});
