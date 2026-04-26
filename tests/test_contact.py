"""
Tests for POST /api/v2/contact and GET /api/v2/submissions.

Uses the same in-memory SQLite pattern as the rest of the test suite
(TestClient without lifespan so the real DB is bypassed).
"""

import pytest
from starlette.testclient import TestClient

from app.main import app

client = TestClient(app, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def reset_contact_rate_limit():
    """Clear the contact-endpoint rate-limit counters before every test.

    SlowAPI's MemoryStorage exposes reset() from the underlying `limits` library.
    Without this, the 5/hour limit fires after the first few tests share the
    same 'testclient' IP address.
    """
    from app.api.routes_v2 import _limiter
    try:
        _limiter._storage.reset()
    except Exception:
        pass
    yield


# ── Helper payloads ───────────────────────────────────────────────────────────

VALID = {
    "org":      "USSOCOM",
    "email":    "j.test@agency.mil",
    "sector":   "Defense / Military",
    "interest": "GPS-degraded route planning for MRAP convoys.",
    "website":  "",   # honeypot empty
}


# ── POST /api/v2/contact — happy path ────────────────────────────────────────

def test_contact_valid_submission():
    r = client.post("/api/v2/contact", json=VALID)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "submitted"
    assert "message" in body


def test_contact_minimal_fields():
    """Only org + email are required."""
    r = client.post("/api/v2/contact", json={"org": "Acme", "email": "a@b.io"})
    assert r.status_code == 201


def test_contact_all_optional_fields_populated():
    payload = {**VALID, "sector": "Aviation / Aerospace", "interest": "polar routing"}
    r = client.post("/api/v2/contact", json=payload)
    assert r.status_code == 201


# ── Honeypot ──────────────────────────────────────────────────────────────────

def test_contact_honeypot_returns_201_silently():
    """Bots filling the hidden 'website' field should receive 201 (no rejection hint)."""
    r = client.post("/api/v2/contact", json={**VALID, "website": "http://spam.example"})
    assert r.status_code == 201   # silent accept — no 400/422 that would reveal honeypot


# ── Validation failures ───────────────────────────────────────────────────────

def test_contact_missing_org():
    r = client.post("/api/v2/contact", json={**VALID, "org": ""})
    assert r.status_code == 422


def test_contact_missing_email():
    r = client.post("/api/v2/contact", json={**VALID, "email": ""})
    assert r.status_code == 422


def test_contact_invalid_email_format():
    r = client.post("/api/v2/contact", json={**VALID, "email": "not-an-email"})
    assert r.status_code == 422


def test_contact_email_missing_tld():
    r = client.post("/api/v2/contact", json={**VALID, "email": "user@nodot"})
    assert r.status_code == 422


def test_contact_org_too_long():
    r = client.post("/api/v2/contact", json={**VALID, "org": "x" * 501})
    assert r.status_code == 422


def test_contact_interest_too_long():
    r = client.post("/api/v2/contact", json={**VALID, "interest": "x" * 4001})
    assert r.status_code == 422


# ── GET /api/v2/submissions ───────────────────────────────────────────────────

def test_submissions_returns_list():
    # Submit one first so the list is non-empty
    client.post("/api/v2/contact", json=VALID)
    r = client.get("/api/v2/submissions")
    assert r.status_code == 200
    body = r.json()
    assert "count" in body
    assert "submissions" in body
    assert isinstance(body["submissions"], list)


def test_submissions_schema():
    client.post("/api/v2/contact", json=VALID)
    r = client.get("/api/v2/submissions")
    rows = r.json()["submissions"]
    assert rows, "Expected at least one submission"
    row = rows[0]
    for field in ("id", "created_at", "org", "email", "sector", "email_sent", "status"):
        assert field in row, f"Missing field: {field}"
    # ip_hash should NOT appear in API responses
    assert "ip_hash" not in row


def test_submissions_pagination():
    # Submit several
    for i in range(3):
        client.post("/api/v2/contact", json={**VALID, "org": f"OrgPagination-{i}"})
    r = client.get("/api/v2/submissions?limit=2&offset=0")
    assert r.status_code == 200
    body = r.json()
    assert len(body["submissions"]) <= 2
    assert body["limit"] == 2


def test_submissions_honeypot_rows_marked_spam():
    client.post("/api/v2/contact", json={**VALID, "org": "SpamBot", "website": "http://x.com"})
    r = client.get("/api/v2/submissions")
    rows = r.json()["submissions"]
    spam_rows = [row for row in rows if row["org"] == "SpamBot"]
    assert spam_rows, "SpamBot submission not found"
    assert spam_rows[0]["status"] == "spam"
