// @ts-check
import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright E2E configuration for IonShield.
 *
 * Tests run against the live FastAPI server (http://localhost:8000).
 * The server must be started before running: uvicorn app.main:app --port 8000
 *
 * Run: npx playwright test
 * UI:  npx playwright test --ui
 */
export default defineConfig({
  testDir:  './e2e',
  timeout:  30_000,          // 30 s per test
  retries:  process.env.CI ? 2 : 0,
  workers:  process.env.CI ? 1 : undefined,

  reporter: [
    ['list'],
    ['html', { outputFolder: 'playwright-report', open: 'never' }],
  ],

  use: {
    baseURL:       'http://localhost:8000',
    // Accept self-signed TLS certs in local dev
    ignoreHTTPSErrors: true,
    // Capture screenshots on failure
    screenshot:    'only-on-failure',
    // Capture traces on first retry
    trace:         'on-first-retry',
    // Headless by default; set PWDEBUG=1 for headed mode
    headless:      true,
    viewport:      { width: 1440, height: 900 },
    locale:        'en-US',
    timezoneId:    'UTC',
  },

  projects: [
    {
      name:    'chromium',
      use:     { ...devices['Desktop Chrome'] },
    },
  ],

  // Expect the FastAPI server to already be running — no webServer block.
  // In CI, start the server before the playwright job.
});
