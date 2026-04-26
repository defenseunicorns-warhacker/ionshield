import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import cesium from 'vite-plugin-cesium';

export default defineConfig({
  plugins: [react(), cesium()],

  // FastAPI mounts app/static/ at /static/, so all asset URLs must
  // carry that prefix. The built index.html is served at /dashboard
  // but loads JS/CSS from /static/assets/ and /static/cesium/.
  base: '/static/',

  build: {
    outDir: '../app/static',
    emptyOutDir: true,
  },

  server: {
    port: 5173,
    // Proxy all backend calls so the dev server and API share an origin
    proxy: {
      '/api':     { target: 'http://localhost:8000', changeOrigin: true },
      '/overlay': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
});
