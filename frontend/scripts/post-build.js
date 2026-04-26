/**
 * post-build.js
 *
 * When Vite is configured with base: '/static/', vite-plugin-cesium copies
 * Cesium assets into outDir/static/ instead of directly into outDir/.
 * This script hoists everything from that nested static/ directory up one
 * level so FastAPI's /static/ mount serves the files at the expected paths.
 *
 * Before: app/static/static/cesium/Cesium.js  →  404
 * After:  app/static/cesium/Cesium.js          →  200
 */

import { existsSync, readdirSync, cpSync, rmSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = fileURLToPath(new URL('.', import.meta.url));
const staticDir = resolve(__dirname, '../../app/static');
const nestedDir = join(staticDir, 'static');

if (existsSync(nestedDir)) {
  for (const item of readdirSync(nestedDir)) {
    cpSync(join(nestedDir, item), join(staticDir, item), { recursive: true, force: true });
  }
  rmSync(nestedDir, { recursive: true, force: true });
  console.log('✓ post-build: hoisted app/static/static/ → app/static/');
} else {
  console.log('post-build: no nested static/ directory found, nothing to move.');
}
