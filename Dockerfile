# ── Stage 1: Frontend build ────────────────────────────────────────────────────
# Builds the React + CesiumJS dashboard. Output lands in app/static/.
FROM node:20-slim AS frontend-builder

WORKDIR /workspace

# Copy only what's needed for the npm install first (better layer caching)
COPY frontend/package*.json ./frontend/

RUN cd frontend && npm ci

# Copy frontend source + the app/ directory (vite outDir is ../app/static)
COPY frontend/ ./frontend/
COPY app/ ./app/

RUN cd frontend && npm run build
# Built files are now in /workspace/app/static/ (assets/, cesium/, index.html)


# ── Stage 2: Python dependency build ──────────────────────────────────────────
FROM python:3.12-slim AS py-builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 3: Runtime image ─────────────────────────────────────────────────────
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

# Python packages from builder
COPY --from=py-builder /install /usr/local

# Application source (pages, API, data layer, hand-written static files)
COPY main.py .
COPY app/ app/

# Default locations.json — drives /overlay/ionshield.cot and the location
# monitoring endpoints. Operators can override at runtime by mounting a
# replacement file or setting LOCATIONS_FILE.
COPY locations.json .

# Generated frontend assets from frontend-builder (overlays app/static/)
COPY --from=frontend-builder /workspace/app/static/ app/static/

# Non-root user — defence-in-depth for container breakout scenarios
RUN adduser --disabled-password --gecos "" --uid 1001 appuser \
 && chown -R appuser:appuser /app
USER appuser

EXPOSE $PORT

# Shell form so $PORT is expanded at runtime
CMD uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1
