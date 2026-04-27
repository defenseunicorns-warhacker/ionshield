"""Tests for app.data.foundry_sync — payload shape + transactional upload."""

from __future__ import annotations

import asyncio


from app.data import foundry_sync


def test_build_snapshot_payload_shape():
    noaa = {
        "fetch_source": "live",
        "kp_index": 3.7,
        "bz_nt": -2.1,
        "xray_flux": 1.2e-6,
        "proton_flux_10mev": 0.4,
        "wind_speed_km_s": 420.0,
        "kp_forecast_24h": 4.0,
        "fetch_status": {"kp": "ok"},
        "data_age_seconds": 30,
    }
    iono = {
        "f107_sfu": 142.3,
        "glotec_median_tecu": 18.5,
        "glotec_p95_tecu": 32.0,
        "glotec_max_tecu": 41.0,
        "glotec_time_tag": "2026-04-26T18:35:00Z",
        "glotec_n_features": 5184,
        "fetch_status": {"f107": "ok", "glotec": "ok"},
    }
    p = foundry_sync.build_snapshot_payload(noaa, iono)
    assert p["kp_index"] == 3.7
    assert p["f107_sfu"] == 142.3
    assert p["glotec_median_tecu"] == 18.5
    assert p["glotec_n_features"] == 5184
    assert p["noaa_feed_status"] == {"kp": "ok"}
    assert p["iono_feed_status"] == {"f107": "ok", "glotec": "ok"}
    assert "fetched_at" in p


def test_sync_skipped_when_unconfigured():
    ok = asyncio.run(foundry_sync.sync_snapshot({"k": "v"}, stack_url="", dataset_rid="", token=""))
    assert ok is False


def test_sync_full_lifecycle(monkeypatch):
    """v2 flow: POST start tx → POST file/upload → POST commit."""
    calls: list[tuple[str, str]] = []

    class _Resp:
        def __init__(self, status, body=None):
            self.status_code = status
            self._body = body or {}
            self.text = ""

        def json(self):
            return self._body

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            calls.append(("POST", url))
            assert kw["headers"]["Authorization"] == "Bearer T"
            if "/files/" in url and "/upload" in url:
                assert kw["content"]
                return _Resp(200, {"path": "x.jsonl"})
            if url.endswith("/commit"):
                return _Resp(200, {"status": "COMMITTED"})
            # start_transaction
            assert "/api/v2/datasets/" in url
            assert url.endswith("/transactions")
            return _Resp(200, {"rid": "tx-rid", "status": "OPEN"})

    monkeypatch.setattr(foundry_sync.httpx, "AsyncClient", _Client)

    ok = asyncio.run(
        foundry_sync.sync_snapshot(
            {"kp_index": 4.0},
            stack_url="https://stack.example.com",
            dataset_rid="ri.foundry.main.dataset.abc",
            token="T",
        )
    )
    assert ok is True
    assert len(calls) == 3
    # All v2 calls are POSTs in this API
    assert all(m == "POST" for m, _ in calls)
    assert calls[0][1].endswith("/transactions")
    assert "/files/" in calls[1][1] and "/upload" in calls[1][1]
    assert "transactionRid=tx-rid" in calls[1][1]
    assert calls[2][1].endswith("/commit")


def test_sync_returns_false_on_http_error(monkeypatch):
    class _Resp:
        status_code = 401
        text = "unauthorized"

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(foundry_sync.httpx, "AsyncClient", lambda **kw: _Client())

    ok = asyncio.run(foundry_sync.sync_snapshot({"kp": 4}, stack_url="https://x", dataset_rid="r", token="T"))
    assert ok is False
