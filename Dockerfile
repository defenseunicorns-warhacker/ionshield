# ── Build stage ───────────────────────────────────────────────────────────────
# Python 3.12-slim: stable, prebuilt wheels for all deps, minimal attack surface.
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

# Copy installed packages from builder (keeps runtime image clean of build tools)
COPY --from=builder /install /usr/local

# Application code
COPY main.py .
COPY app/ app/

# Non-root user — defence-in-depth for container breakout scenarios
RUN adduser --disabled-password --gecos "" --uid 1001 appuser \
 && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Shell form so $PORT is expanded at runtime (Railway injects PORT automatically)
CMD uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1
