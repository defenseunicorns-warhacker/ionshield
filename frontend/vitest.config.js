import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/__tests__/setup.js',
    // Only run unit tests in src/ — exclude Playwright e2e specs which use a
    // different test() API and cause "Playwright Test did not expect test() to
    // be called here" errors when Vitest picks them up.
    include: ['src/**/*.test.{js,jsx,ts,tsx}', 'src/**/*.spec.{js,jsx,ts,tsx}'],
    exclude: ['e2e/**', 'node_modules/**'],
    // Cesium is a browser-only library — mock it entirely in tests
    alias: {
      cesium: new URL('./src/__tests__/__mocks__/cesium.js', import.meta.url).pathname,
    },
  },
});
