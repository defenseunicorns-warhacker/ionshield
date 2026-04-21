"""
IonShield CoT (Cursor-on-Target) generator and TAK server push client.

CoT is the XML-based protocol used by ATAK, WinTAK, and TAK Server for
situational awareness data exchange. Each configured IonShield location is
represented as a CoT event (type a-u-G) with ionospheric risk data embedded
in the <remarks> field and ARGB colour keyed to risk level.

Two delivery modes:
  Pull  — GET /overlay/ionshield.cot  (always available, returns <events> feed)
  Push  — async TCP to TAK Server (port 8087) when COT_SERVER_HOST is set.
          Best-effort: failures are logged and never propagate to callers.

Reference: CoT schema v2.0, MIL-STD-2525B type taxonomy, ITU-R P.531.
"""

import asyncio
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# CoT type: a-u-G = atom · unknown affiliation · ground track
# 'unknown' (u) rather than 'friendly' (f) because these are monitored sites,
# not confirmed friendly tactical elements.
_COT_TYPE = "a-u-G"


def _argb(r: int, g: int, b: int) -> int:
    """RGB → signed int32 ARGB as ATAK expects (alpha always 0xFF)."""
    unsigned = (0xFF << 24) | (r << 16) | (g << 8) | b
    return unsigned - (1 << 32) if unsigned >= (1 << 31) else unsigned


_RISK_ARGB: dict[str, int] = {
    "NOMINAL":  _argb(0x10, 0xB9, 0x81),   # #10B981 green
    "ELEVATED": _argb(0xF5, 0x9E, 0x0B),   # #F59E0B amber
    "DEGRADED": _argb(0xF9, 0x73, 0x16),   # #F97316 orange
    "SEVERE":   _argb(0xEF, 0x44, 0x44),   # #EF4444 red
}


def _cot_ts(dt: datetime) -> str:
    """Format datetime as CoT timestamp (millisecond precision, Z suffix)."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ── Event builder ─────────────────────────────────────────────────────────────

def build_cot_event(location: dict, stale_minutes: int = 10) -> str:
    """
    Build a single CoT XML event string for an IonShield-monitored location.

    The event UID (IONSHIELD-{id}) is stable across refreshes, so ATAK updates
    the existing marker rather than creating a duplicate.
    """
    now   = datetime.now(timezone.utc)
    stale = now + timedelta(minutes=stale_minutes)

    assessment = location.get("assessment") or {}
    a          = assessment.get("assessment") or {}
    risk_level = location.get("alert", {}).get("risk_level") or a.get("risk_level", "NOMINAL")
    risk_score = a.get("risk_score", 0)
    gps_error  = a.get("gps_error_m", 0.0)
    kp_val     = assessment.get("kp_current", "?")

    uid      = f"IONSHIELD-{location['id']}"
    callsign = f"IS-{location['name'][:16]}"   # ATAK callsign length limit ≈ 16-32
    argb     = _RISK_ARGB.get(risk_level, _RISK_ARGB["NOMINAL"])

    alert_flag = " ⚠ALERT" if location.get("alert", {}).get("active") else ""
    remarks = (
        f"IonShield{alert_flag} | Risk: {risk_level} ({risk_score}/100) | "
        f"GPS ±{gps_error:.1f}m | Kp: {kp_val} | "
        f"Asset: {location.get('asset_type', 'GPS_L1')}"
    )

    event = ET.Element("event", {
        "version": "2.0",
        "uid":     uid,
        "type":    _COT_TYPE,
        "time":    _cot_ts(now),
        "start":   _cot_ts(now),
        "stale":   _cot_ts(stale),
        "how":     "m-g",
    })
    ET.SubElement(event, "point", {
        "lat": str(round(location["lat"], 6)),
        "lon": str(round(location["lon"], 6)),
        "hae": "9999999.0",
        "ce":  "9999999.0",
        "le":  "9999999.0",
    })
    detail = ET.SubElement(event, "detail")
    ET.SubElement(detail, "contact", {"callsign": callsign})
    ET.SubElement(detail, "remarks").text = remarks
    ET.SubElement(detail, "color", {"argb": str(argb)})
    ET.SubElement(detail, "precisionlocation", {"geopointsrc": "??", "altsrc": "??"})

    return ET.tostring(event, encoding="unicode", xml_declaration=False)


# ── Feed builder ──────────────────────────────────────────────────────────────

def build_cot_feed(locations: list[dict], stale_minutes: int = 10) -> str:
    """
    Build a CoT XML feed of all monitored locations wrapped in <events>.

    <events> is a widely accepted extension used by TAK Server data feeds
    and ATAK data packages — it lets a single HTTP response carry multiple
    CoT events.
    """
    body = "\n".join(build_cot_event(loc, stale_minutes) for loc in locations)
    return (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>\n"
        "<events>\n"
        f"{body}\n"
        "</events>"
    )


# ── TCP push client ───────────────────────────────────────────────────────────

async def push_cot_to_server(
    host: str,
    port: int,
    locations: list[dict],
    stale_minutes: int = 10,
    connect_timeout: float = 5.0,
) -> None:
    """
    Push CoT events to a TAK server over TCP (standard port 8087).

    TAK Server expects newline-terminated CoT XML strings on the TCP stream.
    This function is best-effort — any exception is logged and swallowed so
    the caller's event loop is never disrupted.
    """
    if not locations:
        return

    xml_events = [build_cot_event(loc, stale_minutes) for loc in locations]

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=connect_timeout,
        )
        for xml in xml_events:
            writer.write((xml + "\n").encode("utf-8"))
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        logger.info(
            "CoT push: sent %d event(s) to %s:%d", len(xml_events), host, port,
        )
    except asyncio.TimeoutError:
        logger.warning("CoT push: connection to %s:%d timed out", host, port)
    except OSError as exc:
        logger.warning("CoT push: connection to %s:%d failed — %s", host, port, exc)
    except Exception as exc:
        logger.warning("CoT push: unexpected error — %s", exc)
