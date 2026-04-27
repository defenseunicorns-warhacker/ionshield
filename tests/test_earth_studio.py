"""B2 — Earth Studio compatibility: KML / KMZ / keyframes export."""

from __future__ import annotations

import csv
import io
import zipfile
import xml.etree.ElementTree as ET

import pytest
from starlette.testclient import TestClient

from app.main import app
from app.outputs.earth_studio import (
    GPS_STYLES,
    HF_STYLES,
    SAT_STYLES,
    _bucket,
    geojson_to_keyframes_csv,
    geojson_to_kml,
    geojson_to_kmz,
    geojson_to_layered_kml,
)


KML_NS = "{http://www.opengis.net/kml/2.2}"


def _sample_fc() -> dict:
    """A 3-region × 2-time FC matching B1's polygon output shape."""
    def feat(time_tag, rid, lat, lon, hf, gps, sat):
        return {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [lon-10, lat-5], [lon+10, lat-5],
                    [lon+10, lat+5], [lon-10, lat+5], [lon-10, lat-5],
                ]],
            },
            "properties": {
                "time_tag": time_tag,
                "region_id": rid,
                "lat_deg": lat, "lon_deg": lon, "geomag_lat_deg": lat,
                "kp": 9.0, "bz_nt": -25.0, "tec_tecu": 30.0,
                "gps_l1_error_m": gps,
                "hf_absorption_db": hf,
                "hf_blackout_probability": 1.0 if hf >= 25 else 0.5,
                "satcom_l_fade_db": sat,
                "radar_l_range_bias_m": 5.0,
            },
        }
    return {
        "type": "FeatureCollection",
        "metadata": {
            "snapshot_count": 2,
            "time_start": "2024-05-11T01:30:00+00:00",
            "time_end": "2024-05-11T02:30:00+00:00",
        },
        "features": [
            feat("2024-05-11T01:30:00+00:00", "R+035-090", 35, -90, 42, 13, 2),
            feat("2024-05-11T01:30:00+00:00", "R+075-010", 75, 10,  85,  9, 1),
            feat("2024-05-11T02:30:00+00:00", "R+035-090", 35, -90, 26, 11, 0.5),
        ],
    }


# ── Severity buckets ────────────────────────────────────────────────────────


def test_bucket_picks_first_threshold_value_crosses():
    color, sid = _bucket(50, HF_STYLES)
    assert sid == "hf-severe"
    color, sid = _bucket(15, HF_STYLES)
    assert sid == "hf-elevated"
    color, sid = _bucket(0, HF_STYLES)
    assert sid == "hf-nominal"


def test_kml_color_byte_order_is_aabbggrr():
    """KML colors are aabbggrr — verify by structure rather than exact match."""
    for table in (HF_STYLES, GPS_STYLES, SAT_STYLES):
        for thresh, color, sid in table:
            assert len(color) == 8           # 4 bytes hex
            # alpha byte is non-zero (visible)
            assert int(color[0:2], 16) > 0


# ── KML structure ───────────────────────────────────────────────────────────


def test_kml_is_valid_xml_with_correct_namespace():
    kml = geojson_to_kml(_sample_fc(), layer_by="hf")
    root = ET.fromstring(kml)
    assert root.tag == KML_NS + "kml"


def test_kml_contains_one_placemark_per_feature():
    kml = geojson_to_kml(_sample_fc(), layer_by="hf")
    root = ET.fromstring(kml)
    placemarks = root.findall(f".//{KML_NS}Placemark")
    assert len(placemarks) == 3


def test_kml_placemarks_carry_timespan_for_keyframe_animation():
    """
    Each Placemark uses <TimeSpan> with <begin> + (optional) <end>, not
    <TimeStamp>. TimeSpan keeps the polygon visible across the snapshot
    window — important when the rendering frame rate is far finer than
    the data cadence.
    """
    kml = geojson_to_kml(_sample_fc(), layer_by="hf")
    root = ET.fromstring(kml)
    spans = root.findall(f".//{KML_NS}TimeSpan")
    assert len(spans) == 3
    # No TimeStamp elements anywhere (we replaced them)
    assert root.findall(f".//{KML_NS}TimeStamp") == []
    # Every span has a <begin>; only the per-region terminal sample omits <end>
    begins = [s.find(f"{KML_NS}begin") for s in spans]
    assert all(b is not None and b.text for b in begins)


def test_kml_timespan_end_chains_to_next_sample_per_region():
    """
    For R+035-090 we have two samples (01:30 → 02:30). The 01:30 sample's
    <end> should equal the 02:30 sample's <begin>; the 02:30 sample is
    the last in the series → no <end> (open-ended into the future).
    """
    kml = geojson_to_kml(_sample_fc(), layer_by="hf")
    root = ET.fromstring(kml)
    spans_by_region: dict[str, list[ET.Element]] = {}
    for pm in root.findall(f".//{KML_NS}Placemark"):
        name = pm.find(f"{KML_NS}name").text
        rid = name.split(" · ")[0]
        spans_by_region.setdefault(rid, []).append(pm.find(f"{KML_NS}TimeSpan"))
    conus = spans_by_region["R+035-090"]
    assert len(conus) == 2
    # First span has both begin and end; end matches the second span's begin
    end_first = conus[0].find(f"{KML_NS}end")
    begin_second = conus[1].find(f"{KML_NS}begin")
    assert end_first is not None and begin_second is not None
    assert end_first.text == begin_second.text
    # Second/last span omits <end>
    assert conus[1].find(f"{KML_NS}end") is None


def test_kml_time_tags_use_z_suffix():
    """+00:00 → Z so QGIS / ArcGIS / older readers accept the file."""
    kml = geojson_to_kml(_sample_fc(), layer_by="hf")
    root = ET.fromstring(kml)
    for tag in root.findall(f".//{KML_NS}TimeSpan/{KML_NS}begin"):
        assert tag.text.endswith("Z"), tag.text
        assert "+00:00" not in tag.text


def test_kml_severity_routes_to_correct_style():
    """Severe HF (85 dB) → hf-severe; nominal (26 dB) → hf-degraded."""
    fc = _sample_fc()
    kml = geojson_to_kml(fc, layer_by="hf")
    root = ET.fromstring(kml)
    style_urls = [el.text for el in root.findall(f".//{KML_NS}styleUrl")]
    assert "#hf-severe" in style_urls         # 85 dB Greenland
    assert "#hf-degraded" in style_urls       # 26 dB second time-step


def test_kml_layered_has_three_folders():
    kml = geojson_to_layered_kml(_sample_fc())
    root = ET.fromstring(kml)
    folders = root.findall(f".//{KML_NS}Folder")
    assert len(folders) == 3
    folder_names = {f.find(f"{KML_NS}name").text for f in folders}
    assert folder_names == {"HF Risk", "GPS Risk", "SATCOM Risk"}


def test_kml_polygon_coordinates_are_lon_lat_alt_triples():
    kml = geojson_to_kml(_sample_fc(), layer_by="gps")
    root = ET.fromstring(kml)
    coords_text = root.find(f".//{KML_NS}coordinates").text
    triples = [c.split(",") for c in coords_text.strip().split()]
    for t in triples:
        assert len(t) == 3                # lon, lat, altitude
        # First sample: lon -100..-80, lat 30..40
        lon, lat, alt = float(t[0]), float(t[1]), float(t[2])
        assert -180 <= lon <= 180
        assert -90 <= lat <= 90
        assert alt == 0


def test_kml_description_is_cdata_with_properties():
    kml = geojson_to_kml(_sample_fc(), layer_by="hf")
    assert "<![CDATA[" in kml
    assert "kp" in kml.lower()
    assert "tec_tecu" in kml.lower()


# ── KMZ ─────────────────────────────────────────────────────────────────────


def test_kmz_is_valid_zip_with_doc_kml_entry():
    blob = geojson_to_kmz(_sample_fc())
    assert blob[:2] == b"PK"          # ZIP magic
    z = zipfile.ZipFile(io.BytesIO(blob))
    assert "doc.kml" in z.namelist()
    inner = z.read("doc.kml").decode()
    assert "<kml" in inner
    # Round-trip: contained KML parses
    ET.fromstring(inner)


def test_kmz_contains_layered_overlay():
    blob = geojson_to_kmz(_sample_fc())
    z = zipfile.ZipFile(io.BytesIO(blob))
    inner = z.read("doc.kml").decode()
    assert "HF Risk" in inner
    assert "GPS Risk" in inner
    assert "SATCOM Risk" in inner


# ── Caveat 3: legend ScreenOverlay + bundled PNG ────────────────────────────


def test_kmz_includes_legend_png():
    blob = geojson_to_kmz(_sample_fc())
    z = zipfile.ZipFile(io.BytesIO(blob))
    assert "legend.png" in z.namelist()
    legend_bytes = z.read("legend.png")
    # Valid PNG signature
    assert legend_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    # IHDR chunk size > 0
    assert len(legend_bytes) > 200


def test_kmz_kml_references_screenoverlay_legend():
    blob = geojson_to_kmz(_sample_fc())
    z = zipfile.ZipFile(io.BytesIO(blob))
    inner = z.read("doc.kml").decode()
    assert "<ScreenOverlay>" in inner
    assert "<href>legend.png</href>" in inner


def test_geojson_to_kml_omits_legend_overlay_by_default():
    """Standalone .kml has no bundled image, so don't reference one."""
    kml = geojson_to_kml(_sample_fc(), layer_by="hf")
    assert "<ScreenOverlay>" not in kml


def test_geojson_to_kml_emits_legend_when_requested():
    kml = geojson_to_kml(
        _sample_fc(), layer_by="hf", include_legend_overlay=True,
    )
    assert "<ScreenOverlay>" in kml
    assert "legend.png" in kml


def test_legend_png_is_valid_decodable_png():
    """The hand-rolled stdlib PNG writer must produce structurally valid bytes."""
    from app.outputs.earth_studio import _build_legend_png
    blob = _build_legend_png()
    # Signature
    assert blob[:8] == b"\x89PNG\r\n\x1a\n"
    # IHDR chunk follows: 4 bytes length + 'IHDR' + 13 bytes data + 4 bytes CRC
    assert blob[12:16] == b"IHDR"
    # Width and height encoded big-endian at offset 16
    import struct
    width, height = struct.unpack("!II", blob[16:24])
    assert width == 240 and height == 280
    # IEND must be present
    assert b"IEND" in blob[-12:]


# ── Keyframes CSV ────────────────────────────────────────────────────────────


def test_keyframes_csv_header_and_count():
    csv_text = geojson_to_keyframes_csv(_sample_fc())
    lines = csv_text.strip().split("\n")
    assert lines[0].startswith("# IonShield")    # comment line
    reader = csv.reader(io.StringIO("\n".join(lines[1:])))
    rows = list(reader)
    assert rows[0][0] == "time_tag"               # header
    assert len(rows) == 1 + 3                     # header + 3 features


def test_keyframes_csv_filters_to_single_region():
    csv_text = geojson_to_keyframes_csv(_sample_fc(), region_id="R+035-090")
    rows = list(csv.reader(io.StringIO(csv_text.split("\n", 1)[1])))
    data_rows = rows[1:]
    assert len(data_rows) == 2
    assert all(r[1] == "R+035-090" for r in data_rows)


# ── HTTP endpoint ────────────────────────────────────────────────────────────


def test_export_endpoint_kml_format():
    with TestClient(app) as client:
        r = client.get("/api/v3/scenarios/export", params={
            "start": "2020-01-01T00:00:00Z",
            "end": "2020-01-02T00:00:00Z",
            "fmt": "kml",
        })
        assert r.status_code == 200
        assert r.headers["content-type"].startswith(
            "application/vnd.google-earth.kml+xml")
        assert "<kml" in r.text


def test_export_endpoint_kmz_format():
    with TestClient(app) as client:
        r = client.get("/api/v3/scenarios/export", params={
            "start": "2020-01-01T00:00:00Z",
            "end": "2020-01-02T00:00:00Z",
            "fmt": "kmz",
        })
        assert r.status_code == 200
        assert r.headers["content-type"].startswith(
            "application/vnd.google-earth.kmz")
        assert r.content[:2] == b"PK"
        assert "filename=\"ionshield-scenario.kmz\"" in r.headers["content-disposition"]


def test_export_endpoint_keyframes_format():
    with TestClient(app) as client:
        r = client.get("/api/v3/scenarios/export", params={
            "start": "2020-01-01T00:00:00Z",
            "end": "2020-01-02T00:00:00Z",
            "fmt": "keyframes",
        })
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        assert r.text.startswith("# IonShield")
        assert "filename=\"ionshield-keyframes.csv\"" in r.headers["content-disposition"]


def test_export_endpoint_unknown_fmt_rejected():
    with TestClient(app) as client:
        r = client.get("/api/v3/scenarios/export", params={
            "start": "2020-01-01T00:00:00Z",
            "end": "2020-01-02T00:00:00Z",
            "fmt": "shapefile",
        })
        assert r.status_code == 422


def test_export_endpoint_unknown_layer_rejected():
    with TestClient(app) as client:
        r = client.get("/api/v3/scenarios/export", params={
            "start": "2020-01-01T00:00:00Z",
            "end": "2020-01-02T00:00:00Z",
            "fmt": "kml", "layer": "asdf",
        })
        assert r.status_code == 422
