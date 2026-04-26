import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/__tests__/setup.js',
    // Cesium is a browser-only library — mock it entirely in tests
    alias: {
      cesium: new URL('./src/__tests__/__mocks__/cesium.js', import.meta.url).pathname,
    },
  },
});
