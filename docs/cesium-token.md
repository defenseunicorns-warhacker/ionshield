# Cesium Ion Token Setup

IonShield uses **CesiumJS** for the 3D globe. Without a token the globe falls back
to free **OpenStreetMap** tiles with a flat ellipsoid terrain model — fully functional
for operational use.

Adding a free **Cesium Ion** token unlocks:
- **Bing Maps Aerial** — high-resolution satellite imagery worldwide
- **Cesium World Terrain** — real 3-D terrain (planned for Phase 2)
- **Cesium OSM Buildings** — optional 3D building layer

---

## 1. Get a free token

1. Create a free account at <https://ion.cesium.com/signup>
2. Sign in → **Access Tokens** → **Create token**
3. Name it `ionshield-dev` (or anything you like)
4. Copy the token string (starts with `eyJ...`)

---

## 2. Configure for local development

Create `frontend/.env.local` (this file is git-ignored):

```bash
VITE_CESIUM_TOKEN=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

Restart the Vite dev server (`npm run dev`) — the globe will automatically
upgrade to Bing Aerial imagery on next load.

---

## 3. Configure for production (Docker / Render)

The token is baked into the Vite build at **build time** via the `VITE_CESIUM_TOKEN`
environment variable. Set it in your deployment environment before running the
Docker build:

### Render
In the Render dashboard → your service → **Environment**:

```
VITE_CESIUM_TOKEN = eyJ...
```

Then trigger a new deploy.

### Docker (local / CI)

```bash
docker build \
  --build-arg VITE_CESIUM_TOKEN=eyJ... \
  -t ionshield .
```

Update the `Dockerfile` frontend build stage to accept the arg:

```dockerfile
FROM node:20-slim AS frontend-builder
ARG VITE_CESIUM_TOKEN=""
ENV VITE_CESIUM_TOKEN=$VITE_CESIUM_TOKEN
WORKDIR /workspace
# ... rest of stage unchanged
```

### GitHub Actions CI

Add `VITE_CESIUM_TOKEN` as a repository secret (`Settings → Secrets → Actions`),
then pass it to the Docker build step:

```yaml
- name: Build Docker image
  uses: docker/build-push-action@v5
  with:
    build-args: VITE_CESIUM_TOKEN=${{ secrets.VITE_CESIUM_TOKEN }}
```

> **Without a token** the Dockerfile and CI work fine — the globe falls back to OSM
> tiles. The token is entirely optional.

---

## 4. Verify

Open the dashboard and look at the globe imagery:
- **OSM (no token):** street-map style tiles
- **Bing Aerial (token set):** satellite photography

The browser console will show `[Globe] Upgraded to Ion World Imagery` when the
token is detected and Bing imagery loads successfully.

---

## 5. Token security

- **Never commit** `frontend/.env.local` to git (already in `.gitignore`)
- **Never hard-code** the token in source files
- **Rotate** the token at <https://ion.cesium.com/tokens> if it is ever leaked
- The token is used **only** for fetching map tiles from Cesium's CDN — it is
  not sent to the IonShield backend
