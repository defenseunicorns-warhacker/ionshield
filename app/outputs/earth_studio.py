"""
B2 — Earth Studio compatibility.

Converts the time-indexed GeoJSON FeatureCollection produced by B1 into:

  • KML (XML)               — one Placemark per (region, time) with
                              <TimeStamp>; styled by severity. Importable
                              into Earth Studio, Google Earth Pro, ArcGIS,
                              QGIS — anything that reads KML 2.2.
  • KMZ (zipped KML)        — multi-folder layered overlay
                              (HF Risk / GPS Risk / SATCOM Risk)
                              packaged with the embedded styles. Smaller
                              over the wire and the standard Earth Studio
                              import format.
  • Earth Studio keyframes  — CSV in the format Earth Studio's Tracks tool
                              reads natively. Each row carries lat/lon/
                              altitude and per-parameter columns so a
                              user can drag the file into the Tracks panel
                              and animate the camera + per-region
                              metrics together.

KML color is `aabbggrr` (alpha-blue-green-red) — opposite byte order from
HTML/CSS. Earth Studio honors `<TimeStamp>` for keyframe-aware import; the
result is automatic time-driven animation when the user opens the file.

Pure stdlib — no `simplekml` / `pykml` dependencies.
"""

from __future__ import annotations

import csv
import io
import logging
import zipfile
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from app.outputs._png import draw_text, encode_png, fill_rect

logger = logging.getLogger(__name__)


# ── Severity → color buckets ─────────────────────────────────────────────────
# KML colors are aabbggrr (alpha, blue, green, red). 80% opacity = cc.

# HF absorption (dB) buckets
HF_STYLES = (
    # (threshold, KML color, friendly id)
    (40, "cc0000aa", "hf-severe"),  # red, alpha 80%
    (25, "cc00a5ff", "hf-degraded"),  # orange-amber
    (10, "cc00ffff", "hf-elevated"),  # yellow
    (0, "cc00cc00", "hf-nominal"),  # green
)

# GPS L1 error (m) buckets
GPS_STYLES = (
    (10, "cc0000aa", "gps-severe"),
    (6, "cc00a5ff", "gps-degraded"),
    (3, "cc00ffff", "gps-elevated"),
    (0, "cc00cc00", "gps-nominal"),
)

# SATCOM L fade (dB) buckets
SAT_STYLES = (
    (3, "cc0000aa", "sat-severe"),
    (1, "cc00a5ff", "sat-degraded"),
    (0, "cc00cc00", "sat-nominal"),
)


def _bucket(value: float, table: tuple) -> tuple[str, str]:
    """Return (kml_color, style_id) for the first threshold value crosses."""
    for thresh, color, sid in table:
        if value >= thresh:
            return color, sid
    return table[-1][1], table[-1][2]


# ── KML primitives ───────────────────────────────────────────────────────────


def _kml_styles() -> str:
    """Emit a <Style> block for every bucket, plus a base PolyStyle."""
    lines: list[str] = []
    for table, fill_only in (
        (HF_STYLES, False),
        (GPS_STYLES, False),
        (SAT_STYLES, False),
    ):
        for _thresh, color, sid in table:
            lines.append(f"""    <Style id="{sid}">
      <LineStyle><color>ff000000</color><width>0.4</width></LineStyle>
      <PolyStyle><color>{color}</color><fill>1</fill><outline>1</outline></PolyStyle>
    </Style>""")
    return "\n".join(lines)


def _coords_string(coordinates: list[list[list[float]]]) -> str:
    """Flatten GeoJSON Polygon coordinates into KML's `lon,lat,alt` triplets."""
    ring = coordinates[0]  # outer ring only — IonShield polygons are simple
    return " ".join(f"{lon},{lat},0" for lon, lat in ring)


def _z_iso(s: str | datetime) -> str:
    """
    Coerce a time string or datetime into broadly-compatible UTC ISO with `Z`.

    Earth Studio + Google Earth Pro accept both `+00:00` and `Z` suffixes,
    but QGIS / ArcGIS / older KML readers prefer `Z`. The B1 export emits
    `+00:00`; we normalize here so consumers pick whichever they need.
    """
    if isinstance(s, datetime):
        if s.tzinfo is None:
            s = s.replace(tzinfo=timezone.utc)
        return s.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not isinstance(s, str):
        return ""
    if s.endswith("Z"):
        return s
    if "+00:00" in s:
        return s.replace("+00:00", "Z")
    # Try parsing fallback
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return s


def _kml_placemark(
    *,
    name: str,
    style_id: str,
    time_begin: str,
    time_end: str | None,
    coords: str,
    properties: dict,
    geom_type: str = "Polygon",
) -> str:
    """
    Build a Placemark with a `<TimeSpan>` covering [time_begin, time_end].

    TimeSpan keeps the polygon visible across the full storm-tick window
    rather than flashing only at one instant — important for Earth Studio
    keyframe import where the rendering frame rate is far finer than the
    snapshot cadence (1 hour for backfilled storms).

    When `time_end` is None (last snapshot in the series) we omit `<end>`,
    which KML 2.2 interprets as "open-ended into the future" — visible
    after the begin time. That keeps the last frame visible at end-of-replay.
    """
    desc_rows = [f"<b>{escape(k)}</b>: {escape(str(v))}" for k, v in properties.items() if v is not None]
    desc = "<![CDATA[" + "<br/>".join(desc_rows) + "]]>"
    end_xml = f"<end>{escape(_z_iso(time_end))}</end>" if time_end else ""
    if geom_type == "Point":
        geom_xml = f"<Point><coordinates>{coords}</coordinates></Point>"
    else:
        geom_xml = (
            f"<Polygon><outerBoundaryIs><LinearRing>"
            f"<coordinates>{coords}</coordinates>"
            f"</LinearRing></outerBoundaryIs></Polygon>"
        )
    return f"""      <Placemark>
        <name>{escape(name)}</name>
        <description>{desc}</description>
        <styleUrl>#{style_id}</styleUrl>
        <TimeSpan><begin>{escape(_z_iso(time_begin))}</begin>{end_xml}</TimeSpan>
        {geom_xml}
      </Placemark>"""


def _next_time_by_region(
    features: list[dict],
) -> dict[tuple[str, str], str | None]:
    """
    For every (region_id, time_tag) pair, find the next time_tag of the same
    region. Used to compute the `<end>` of each Placemark's TimeSpan.

    Returns None for the last sample of each region — that means
    "open-ended" so the polygon stays visible at end-of-replay rather than
    disappearing at the final tick.
    """
    by_region: dict[str, list[str]] = {}
    for feat in features:
        props = feat.get("properties") or {}
        rid = props.get("region_id")
        tt = props.get("time_tag")
        if rid and tt:
            by_region.setdefault(rid, []).append(tt)
    out: dict[tuple[str, str], str | None] = {}
    for rid, times in by_region.items():
        sorted_times = sorted(times)
        for i, t in enumerate(sorted_times):
            out[(rid, t)] = sorted_times[i + 1] if i + 1 < len(sorted_times) else None
    return out


# ── Public converters ────────────────────────────────────────────────────────


_LEGEND_OVERLAY_XML = """    <ScreenOverlay>
      <name>Severity legend</name>
      <Icon><href>legend.png</href></Icon>
      <overlayXY x="0" y="1" xunits="fraction" yunits="fraction"/>
      <screenXY x="20" y="20" xunits="pixels" yunits="insetPixels"/>
      <size x="0" y="0" xunits="pixels" yunits="pixels"/>
    </ScreenOverlay>"""


def geojson_to_kml(
    fc: dict,
    *,
    layer_by: str = "hf",
    document_name: str = "IonShield Scenario",
    include_legend_overlay: bool = False,
) -> str:
    """
    Convert a B1 FeatureCollection into a KML 2.2 document.

    `layer_by` selects which property drives the polygon color:
      "hf"  → hf_absorption_db
      "gps" → gps_l1_error_m
      "sat" → satcom_l_fade_db

    `include_legend_overlay` adds a `<ScreenOverlay>` referencing
    `legend.png`. Only meaningful inside a KMZ where that resource is
    bundled — the standalone .kml export defaults to False.
    """
    table = {"hf": HF_STYLES, "gps": GPS_STYLES, "sat": SAT_STYLES}[layer_by]
    metric_field = {
        "hf": "hf_absorption_db",
        "gps": "gps_l1_error_m",
        "sat": "satcom_l_fade_db",
    }[layer_by]

    next_by_region = _next_time_by_region(fc.get("features", []))

    placemarks: list[str] = []
    for feat in fc.get("features", []):
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        if gtype not in ("Polygon", "Point"):
            continue
        props = feat.get("properties") or {}
        value = float(props.get(metric_field, 0))
        _color, style_id = _bucket(value, table)
        time_tag = props.get("time_tag", "")
        if gtype == "Point":
            lon, lat = geom["coordinates"][:2]
            coords = f"{lon},{lat},0"
        else:
            coords = _coords_string(geom["coordinates"])
        name = f"{props.get('region_id', '')} · " f"{metric_field.split('_')[0].upper()} {value:.1f}"
        rid = props.get("region_id", "")
        placemarks.append(
            _kml_placemark(
                name=name,
                style_id=style_id,
                time_begin=time_tag,
                time_end=next_by_region.get((rid, time_tag)),
                coords=coords,
                properties=props,
                geom_type=gtype,
            )
        )

    overlay = _LEGEND_OVERLAY_XML if include_legend_overlay else ""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>{escape(document_name)}</name>
    <description>IonShield scenario export — layer={layer_by}</description>
{_kml_styles()}
{overlay}
    <Folder>
      <name>{escape(layer_by.upper())} Risk</name>
{chr(10).join(placemarks)}
    </Folder>
  </Document>
</kml>
"""


def geojson_to_layered_kml(
    fc: dict,
    *,
    document_name: str = "IonShield Scenario — All Layers",
    include_legend_overlay: bool = False,
) -> str:
    """
    Multi-folder KML: one folder per impact layer (GPS / HF / SATCOM).
    Each folder contains the same regions but colored by that layer's metric.
    """
    next_by_region = _next_time_by_region(fc.get("features", []))

    folders: list[str] = []
    for layer, table, metric in (
        ("HF", HF_STYLES, "hf_absorption_db"),
        ("GPS", GPS_STYLES, "gps_l1_error_m"),
        ("SATCOM", SAT_STYLES, "satcom_l_fade_db"),
    ):
        marks: list[str] = []
        for feat in fc.get("features", []):
            geom = feat.get("geometry") or {}
            gtype = geom.get("type")
            if gtype not in ("Polygon", "Point"):
                continue
            props = feat.get("properties") or {}
            value = float(props.get(metric, 0))
            _color, style_id = _bucket(value, table)
            time_tag = props.get("time_tag", "")
            if gtype == "Point":
                lon, lat = geom["coordinates"][:2]
                coords = f"{lon},{lat},0"
            else:
                coords = _coords_string(geom["coordinates"])
            rid = props.get("region_id", "")
            marks.append(
                _kml_placemark(
                    name=f"{rid} · {layer} {value:.1f}",
                    style_id=style_id,
                    time_begin=time_tag,
                    time_end=next_by_region.get((rid, time_tag)),
                    coords=coords,
                    properties=props,
                    geom_type=gtype,
                )
            )
        folders.append(f"""    <Folder>
      <name>{layer} Risk</name>
{chr(10).join(marks)}
    </Folder>""")

    overlay = _LEGEND_OVERLAY_XML if include_legend_overlay else ""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>{escape(document_name)}</name>
    <description>IonShield scenario — layered overlays per impact system</description>
{_kml_styles()}
{overlay}
{chr(10).join(folders)}
  </Document>
</kml>
"""


# ── Legend image (embedded into KMZ as legend.png) ──────────────────────────


_LEGEND_HEX_TO_RGB = {
    # Decoded from the aabbggrr KML colors — alpha stripped
    "cc0000aa": (170, 0, 0),  # severe — red
    "cc00a5ff": (255, 165, 0),  # degraded — orange
    "cc00ffff": (255, 255, 0),  # elevated — yellow
    "cc00cc00": (0, 204, 0),  # nominal — green
}


def _build_legend_png() -> bytes:
    """
    Render a 220×260 RGBA PNG legend covering all three layer scales.

    Three columns — HF / GPS / SATCOM — each with severity bands and the
    threshold values from HF_STYLES / GPS_STYLES / SAT_STYLES. Pure-stdlib
    bitmap rendering with the 5×7 font in app.outputs._png.
    """
    W, H = 240, 280
    pixels = bytearray(W * H * 4)
    fill_rect(pixels, W, 0, 0, W, H, (15, 23, 42, 235))  # IonShield navy bg

    draw_text(pixels, W, 12, 8, "IONSHIELD LEGEND", (56, 189, 248))
    draw_text(pixels, W, 12, 22, "BY HF DB", (148, 163, 184))

    def render_band(y: int, color: tuple[int, int, int], label: str) -> None:
        fill_rect(pixels, W, 12, y, 28, 14, (*color, 255))
        draw_text(pixels, W, 46, y + 3, label, (226, 232, 240))

    # HF column (left)
    draw_text(pixels, W, 12, 38, "HF DB", (148, 163, 184))
    render_band(48, _LEGEND_HEX_TO_RGB["cc0000aa"], "GE 40")
    render_band(66, _LEGEND_HEX_TO_RGB["cc00a5ff"], "GE 25")
    render_band(84, _LEGEND_HEX_TO_RGB["cc00ffff"], "GE 10")
    render_band(102, _LEGEND_HEX_TO_RGB["cc00cc00"], "0 TO 10")

    # GPS column (middle band)
    draw_text(pixels, W, 12, 124, "GPS L1 M", (148, 163, 184))
    render_band(134, _LEGEND_HEX_TO_RGB["cc0000aa"], "GE 10")
    render_band(152, _LEGEND_HEX_TO_RGB["cc00a5ff"], "GE 6")
    render_band(170, _LEGEND_HEX_TO_RGB["cc00ffff"], "GE 3")
    render_band(188, _LEGEND_HEX_TO_RGB["cc00cc00"], "0 TO 3")

    # SATCOM column (bottom band)
    draw_text(pixels, W, 12, 210, "SATCOM L DB", (148, 163, 184))
    render_band(220, _LEGEND_HEX_TO_RGB["cc0000aa"], "GE 3")
    render_band(238, _LEGEND_HEX_TO_RGB["cc00a5ff"], "GE 1")
    render_band(256, _LEGEND_HEX_TO_RGB["cc00cc00"], "0 TO 1")

    return encode_png(W, H, bytes(pixels))


def geojson_to_kmz(fc: dict, *, document_name: str | None = None) -> bytes:
    """
    Bundle the layered KML and the severity-bucket legend into a KMZ.

    KMZ is the canonical Earth Studio import format — single download,
    smaller over the wire, and able to carry image resources (the legend
    PNG is referenced as a `<ScreenOverlay>` so users get a corner legend
    without having to recreate one inside Earth Studio).
    """
    kml = geojson_to_layered_kml(
        fc,
        document_name=document_name or "IonShield Scenario",
        include_legend_overlay=True,
    )
    legend_bytes = _build_legend_png()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", kml)
        z.writestr("legend.png", legend_bytes)
    return buf.getvalue()


# ── Earth Studio keyframe CSV ───────────────────────────────────────────────


# The fields a user is most likely to want as Earth Studio Track parameters.
# Earth Studio reads the file as numeric tracks indexed by row (one keyframe
# per row); time isn't enforced inside the file but is documented in the
# header comment so the user can match it to project frame rate.
KEYFRAME_FIELDS = (
    "time_tag",
    "region_id",
    "lat_deg",
    "lon_deg",
    "kp",
    "tec_tecu",
    "gps_l1_error_m",
    "hf_absorption_db",
    "hf_blackout_probability",
    "satcom_l_fade_db",
)


def geojson_to_keyframes_csv(
    fc: dict,
    *,
    region_id: str | None = None,
) -> str:
    """
    Emit a CSV that Earth Studio's Tracks tool reads as keyframe data.

    If `region_id` is given, output is filtered to that single region
    (one row per timestep) — typical when animating a camera POI to follow
    the storm front. Otherwise emits all (region × time) rows so a user
    can pivot or filter in Earth Studio.

    The header row includes a leading `# IonShield ...` comment line that
    Earth Studio ignores, naming the source scenario for traceability.
    """
    buf = io.StringIO()
    meta = fc.get("metadata") or {}
    buf.write(
        f"# IonShield Earth Studio keyframes  "
        f"start={meta.get('time_start')}  end={meta.get('time_end')}  "
        f"snapshots={meta.get('snapshot_count')}\n"
    )
    writer = csv.writer(buf)
    writer.writerow(KEYFRAME_FIELDS)
    for feat in fc.get("features", []):
        props = feat.get("properties") or {}
        if region_id and props.get("region_id") != region_id:
            continue
        writer.writerow([props.get(k) for k in KEYFRAME_FIELDS])
    return buf.getvalue()
