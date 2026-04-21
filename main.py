"""
Entry point for IonShield backend.

Run locally:
    uvicorn main:app --reload --port 8000

Production (via Procfile):
    uvicorn main:app --host 0.0.0.0 --port $PORT
"""
from app.main import app  # noqa: F401 — re-exported for uvicorn
