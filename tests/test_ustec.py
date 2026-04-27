"""Tests for app.data.ustec — F10.7 + GloTEC ingestion."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.data import ustec


@pytest.fixture(autouse=True)
def reset_cache():
    ustec._cache["f107"] = None
    ustec._cache["glotec"] = None
    ustec._cache["glotec_time_tag"] = None
    ustec._cache["fetch_status"] = {}
    yield


def test_fallbacks_when_cache_empty():
    assert ustec.get_f107_flux() == ustec.FALLBACK["f107_sfu"]
    s = ustec.get_glotec_summary()
    assert s["median_tecu"] == ustec.FALLBACK["glotec_median_tecu"]
    assert s["n_features"] == 0


def test_parses_f107_payload():
    ustec._cache["f107"] = [{"time_tag": "2026-04-26T00:00:00", "flux": 142.3}]
    assert ustec.get_f107_flux() == 142.3


def test_invalid_f107_falls_back():
    ustec._cache["f107"] = [{"flux": -5}]
    assert ustec.get_f107_flux() == ustec.FALLBACK["f107_sfu"]


def test_glotec_summary_computes_stats():
    ustec._cache["glotec"] = {
        "type": "FeatureCollection",
        "features": [
            {"properties": {"tec": 10.0, "quality_flag": 0}},
            {"properties": {"tec": 20.0, "quality_flag": 0}},
            {"properties": {"tec": 30.0, "quality_flag": 0}},
            {"properties": {"tec": 99.0, "quality_flag": 1}},  # bad quality, filtered
            {"properties": {"tec": 50.0, "quality_flag": 0}},
            {"properties": {"tec": 40.0, "quality_flag": 0}},
        ],
    }
    ustec._cache["glotec_time_tag"] = "2026-04-26T18:35:00Z"
    s = ustec.get_glotec_summary()
    assert s["n_features"] == 5  # quality_flag=1 filtered out
    assert s["median_tecu"] == 30.0
    assert s["max_tecu"] == 50.0
    assert s["time_tag"] == "2026-04-26T18:35:00Z"


def test_fetch_handles_timeout(monkeypatch):
    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(ustec.httpx, "AsyncClient", _Client)
    asyncio.run(ustec.fetch_ionosphere())
    assert ustec._cache["fetch_status"]["f107"] == "timeout"
    assert ustec._cache["fetch_status"]["glotec"] == "timeout"


def test_fetch_glotec_listing_then_snapshot(monkeypatch):
    """Verify the listing → latest-file flow."""

    class _Resp:
        def __init__(self, body):
            self._body = body
            self.status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return self._body

    f107_body = [{"flux": 123.0}]
    listing = [{"url": "/products/glotec/geojson_2d_urt/glotec_X.geojson", "time_tag": "2026-04-26T18:35:00Z"}]
    fc = {
        "type": "FeatureCollection",
        "features": [
            {"properties": {"tec": 25.0, "quality_flag": 0}},
        ],
        "time_tag": "2026-04-26T18:35:00Z",
    }

    seq = {
        ustec.USTEC_ENDPOINTS["f107"]: f107_body,
        ustec.USTEC_ENDPOINTS["glotec_listing"]: listing,
        ustec.SWPC_BASE + listing[0]["url"]: fc,
    }

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            if url not in seq:
                raise AssertionError(f"unexpected URL {url}")
            return _Resp(seq[url])

    monkeypatch.setattr(ustec.httpx, "AsyncClient", _Client)
    asyncio.run(ustec.fetch_ionosphere())
    assert ustec._cache["fetch_status"]["f107"] == "ok"
    assert ustec._cache["fetch_status"]["glotec"] == "ok"
    assert ustec.get_f107_flux() == 123.0
    assert ustec.get_glotec_summary()["median_tecu"] == 25.0


def test_cache_snapshot_shape():
    ustec._cache["f107"] = [{"flux": 100.0}]
    ustec._cache["glotec"] = {
        "type": "FeatureCollection",
        "features": [{"properties": {"tec": 12.0, "quality_flag": 0}}],
    }
    snap = ustec.cache_snapshot()
    assert snap["f107_sfu"] == 100.0
    assert snap["glotec_median_tecu"] == 12.0
    assert snap["glotec_n_features"] == 1
    assert "fetch_status" in snap


def test_glotec_stale_cache_used_when_listing_empty(monkeypatch):
    """When listing returns empty but cache is recent, status=stale and FC stays."""
    from datetime import datetime, timezone

    fc = {"type": "FeatureCollection", "features": [{"properties": {"tec": 22.0, "quality_flag": 0}}]}
    ustec._cache["glotec"] = fc
    ustec._cache["glotec_time_tag"] = "2026-04-26T18:35:00Z"
    ustec._cache["glotec_last_good_fetch"] = datetime.now(timezone.utc).isoformat()
    ustec._cache["fetch_status"] = {}

    class _Resp:
        def __init__(self, body):
            self._body = body
            self.status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return self._body

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            if url == ustec.USTEC_ENDPOINTS["f107"]:
                return _Resp([{"flux": 100.0}])
            return _Resp([])  # empty listing

    monkeypatch.setattr(ustec.httpx, "AsyncClient", _Client)
    asyncio.run(ustec.fetch_ionosphere())
    assert ustec._cache["fetch_status"]["glotec"] == "stale"
    # FC retained — operational summary still meaningful
    assert ustec.get_glotec_summary()["median_tecu"] == 22.0


def test_glotec_status_is_error_when_cache_too_old(monkeypatch):
    from datetime import datetime, timedelta, timezone

    ustec._cache["glotec"] = {"type": "FeatureCollection", "features": []}
    ustec._cache["glotec_last_good_fetch"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    ustec._cache["fetch_status"] = {}

    class _Resp:
        def __init__(self, body):
            self._body = body
            self.status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return self._body

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            if url == ustec.USTEC_ENDPOINTS["f107"]:
                return _Resp([{"flux": 100.0}])
            return _Resp([])

    monkeypatch.setattr(ustec.httpx, "AsyncClient", _Client)
    asyncio.run(ustec.fetch_ionosphere())
    assert ustec._cache["fetch_status"]["glotec"] == "error"
