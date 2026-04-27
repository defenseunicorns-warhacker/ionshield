"""Phase 3a — ATAK readiness pack: network link KML, offline KMZ, install page."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from starlette.testclient import TestClient

from app.main import app
from app.outputs import atak as atak_out


# ── Network link XML ────────────────────────────────────────────────────────


def test_network_link_kml_includes_refresh_interval():
    xml = atak_out.network_link_xml(base_url="https://ionshield.app", refresh_seconds=300)
    assert "<NetworkLink>" in xml
    assert "https://ionshield.app/overlay/risk.kml" in xml
    assert "<refreshInterval>300</refreshInterval>" in xml
    assert "<refreshMode>onInterval</refreshMode>" in xml


def test_network_link_kml_strips_trailing_slash():
    xml = atak_out.network_link_xml(base_url="https://ionshield.app/")
    assert "https://ionshield.app/overlay/risk.kml" in xml
    assert "https://ionshield.app//overlay" not in xml


def test_network_link_view_refresh_toggle():
    with_view = atak_out.network_link_xml(base_url="x", view_refresh=True)
    without = atak_out.network_link_xml(base_url="x", view_refresh=False)
    assert "<viewRefreshMode>onStop</viewRefreshMode>" in with_view
    assert "<viewRefreshMode>" not in without


# ── Offline pack KMZ ────────────────────────────────────────────────────────


def test_offline_pack_is_a_valid_kmz_archive():
    body = atak_out.offline_pack_kmz()
    assert body.startswith(b"PK"), "KMZ must be a valid zip archive"
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        names = zf.namelist()
    assert "doc.kml" in names
    assert "README.txt" in names


def test_offline_pack_includes_forecast_when_provided():
    forecast = {
        "entries": [
            {"horizon_h": 1, "valid_at": "2026-04-27T00:00:00Z", "kp_predicted": 3.2, "severity": "G0"},
            {"horizon_h": 24, "valid_at": "2026-04-27T23:00:00Z", "kp_predicted": 6.5, "severity": "G2"},
        ]
    }
    body = atak_out.offline_pack_kmz(forecast=forecast)
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        names = zf.namelist()
        assert "forecast.kml" in names
        forecast_kml = zf.read("forecast.kml").decode("utf-8")
    assert "+1h" in forecast_kml
    assert "+24h" in forecast_kml
    assert "G2" in forecast_kml


def test_offline_pack_omits_forecast_when_none():
    body = atak_out.offline_pack_kmz(forecast=None)
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        assert "forecast.kml" not in zf.namelist()


# ── HTTP endpoints ──────────────────────────────────────────────────────────


def test_atak_network_link_endpoint_serves_kml():
    with TestClient(app) as client:
        r = client.get("/atak/network-link.kml")
    assert r.status_code == 200
    assert "application/vnd.google-earth.kml+xml" in r.headers["content-type"]
    assert "<NetworkLink>" in r.text
    # Operator gets a download prompt rather than inline render
    assert "attachment" in r.headers.get("content-disposition", "")


def test_atak_offline_pack_endpoint_serves_kmz():
    with TestClient(app) as client:
        r = client.get("/atak/offline-pack.kmz")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/vnd.google-earth.kmz"
    assert r.content.startswith(b"PK")


def test_atak_install_page_renders():
    with TestClient(app) as client:
        r = client.get("/atak")
    assert r.status_code == 200
    body = r.text
    assert "ATAK" in body
    assert "/atak/network-link.kml" in body
    assert "/atak/offline-pack.kmz" in body


# ── Plugin scaffolding present in repo ──────────────────────────────────────


def test_plugin_scaffolding_files_present():
    root = Path(__file__).parent.parent / "atak-plugin"
    assert (root / "README.md").exists()
    assert (root / "AndroidManifest.xml").exists()
    assert (root / "build.gradle").exists()
    assert (root / "src/main/java/com/ionshield/atak/IonShieldPlugin.java").exists()
    assert (root / "src/main/java/com/ionshield/atak/IonShieldClient.java").exists()


def test_plugin_manifest_declares_atak_metadata():
    manifest = (Path(__file__).parent.parent / "atak-plugin" / "AndroidManifest.xml").read_text()
    assert "atakplugin.api.version" in manifest
    assert "atakplugin.entry" in manifest
    assert "com.ionshield.atak.IonShieldPlugin" in manifest


# ── Marketing nav wiring ────────────────────────────────────────────────────


def test_atak_link_in_nav():
    js = (Path(__file__).parent.parent / "app" / "static" / "nav.js").read_text()
    assert "/atak" in js
    assert "ATAK" in js
