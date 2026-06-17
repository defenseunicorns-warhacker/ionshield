"""
Build the one-page IonShield "Data Sources" sheet (.pdf).

Audience: WarHacker judges / weather officers / program staff who ask
"where does the data come from?" Print-friendly, single page, every source
named with its endpoint and cadence. Mirrors the science backgrounder style.

Run:    python3 scripts/build_data_sources_sheet.py
Output: docs/IonShield_Data_Sources.pdf
"""

from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

INK = HexColor("#10181f")
MUTED = HexColor("#5a6672")
ACCENT = HexColor("#0e7490")
RULE = HexColor("#c9d2da")
HEADBG = HexColor("#eaf2f5")
ZEBRA = HexColor("#f5f8fa")

S_TITLE = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=17, textColor=INK, spaceAfter=1)
S_SUB = ParagraphStyle("s", fontName="Helvetica", fontSize=8.5, textColor=MUTED, spaceAfter=6)
S_H = ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=10, textColor=ACCENT, spaceBefore=8, spaceAfter=3)
S_HDR = ParagraphStyle("hd", fontName="Helvetica-Bold", fontSize=7.4, textColor=INK)
S_CELL = ParagraphStyle("c", fontName="Helvetica", fontSize=7.3, leading=9.0, textColor=INK)
S_MONO = ParagraphStyle("m", fontName="Courier", fontSize=6.6, leading=8.2, textColor=MUTED)
S_SMALL = ParagraphStyle("sm", fontName="Helvetica", fontSize=7.2, leading=9.2, textColor=MUTED)

OUT = Path(__file__).resolve().parent.parent / "docs" / "IonShield_Data_Sources.pdf"
OUT.parent.mkdir(exist_ok=True)

doc = SimpleDocTemplate(
    str(OUT),
    pagesize=letter,
    leftMargin=0.5 * inch,
    rightMargin=0.5 * inch,
    topMargin=0.45 * inch,
    bottomMargin=0.4 * inch,
    title="IonShield Data Sources",
    author="IonShield",
)

story = []
story.append(Paragraph("IonShield — Data Sources", S_TITLE))
story.append(
    Paragraph(
        "Every input is measured (NOAA / NASA / GFZ), forecaster-issued (NOAA), or explicitly "
        "operator-entered — and each output labels which. Nothing is synthetic. Base URLs are "
        "configurable (SWPC_BASE_URL, HAPI_BASE_URL) so an enclave can point at an internal mirror "
        "across a one-way diode instead of the public internet.",
        S_SUB,
    )
)
story.append(HRFlowable(width="100%", thickness=1.5, color=ACCENT, spaceAfter=4))


def table(rows, col_w):
    data = [
        [Paragraph(c, S_HDR if i == 0 else (S_MONO if "/" in str(c) and c.startswith("/") else S_CELL)) for c in r]
        for i, r in enumerate(rows)
    ]
    # rebuild with proper styles per cell
    data = []
    for ri, r in enumerate(rows):
        cells = []
        for ci, c in enumerate(r):
            if ri == 0:
                cells.append(Paragraph(c, S_HDR))
            elif isinstance(c, str) and c.startswith("/"):
                cells.append(Paragraph(c, S_MONO))
            else:
                cells.append(Paragraph(c, S_CELL))
        data.append(cells)
    t = Table(data, colWidths=col_w, repeatRows=1)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, 0), HEADBG),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, RULE),
        ("LINEBELOW", (0, 1), (-1, -1), 0.3, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]
    for ri in range(2, len(data), 2):
        style.append(("BACKGROUND", (0, ri), (-1, ri), ZEBRA))
    t.setStyle(TableStyle(style))
    return t


# ── Live feeds ────────────────────────────────────────────────────────────────
story.append(Paragraph("Live operational feeds — NOAA SWPC, polled every 5 minutes", S_H))
live = [
    ["Dataset", "Endpoint (services.swpc.noaa.gov)", "Drives"],
    [
        "Planetary Kp index (1-min)",
        "/json/planetary_k_index_1m.json",
        "Geomagnetic storm level — GPS, HF, scintillation",
    ],
    [
        "GOES X-ray flux 1–8 Å (6-hr)",
        "/json/goes/primary/xrays-6-hour.json",
        "Flare class — HF blackout (D-layer absorption)",
    ],
    ["Solar wind plasma (2-hr)", "/products/solar-wind/plasma-2-hour.json", "Speed / density — storm driver context"],
    ["IMF magnetometer, Bz GSM (2-hr)", "/products/solar-wind/mag-2-hour.json", "Southward Bz — the storm trigger"],
    [
        "GOES proton flux ≥10 MeV (3-day)",
        "/json/goes/primary/integral-protons-3-day.json",
        "Polar cap absorption — poleward HF blackout",
    ],
    [
        "NOAA 3-day Kp forecast",
        "/products/noaa-planetary-k-index-forecast.json",
        "Forward risk + ATAK overlay time windows",
    ],
    ["NOAA R/S/G scales + probabilities", "/products/noaa-scales.json", "Forecaster-issued R/S/G probabilities"],
    ["F10.7 cm solar radio flux", "/json/f107_cm_flux.json", "Ionospheric baseline (solar-activity proxy)"],
    [
        "GloTEC global Total Electron Content",
        "/products/glotec/geojson_2d_urt.json",
        "Measured TEC — GPS error & radar range bias",
    ],
]
story.append(table(live, [1.7 * inch, 3.0 * inch, 2.8 * inch]))
story.append(
    Paragraph(
        "Health checks report these as <b>noaa_swpc</b> (7 feeds) + <b>ionosphere</b> (2 feeds). Each feed "
        "fails independently behind a circuit breaker; SHA-256 provenance is recorded on every value.",
        S_SMALL,
    )
)

# ── Historical / backfill ─────────────────────────────────────────────────────
story.append(Paragraph("Historical / backfill — on-demand, not live", S_H))
hist = [
    ["Dataset", "Source", "Use"],
    [
        "NASA OMNI hourly merged (OMNI2_H0_MRG1HR)",
        "NASA CDAWeb HAPI (cdaweb.gsfc.nasa.gov/hapi) — Bz GSM, density, velocity, proton flux, Kp",
        "Backfills the local DB so the Kp ML forecaster has training history and storm scenarios can be precomputed",
    ],
]
story.append(table(hist, [2.2 * inch, 3.0 * inch, 2.3 * inch]))

# ── Replay ────────────────────────────────────────────────────────────────────
story.append(Paragraph("Replay datasets — recorded, for demos & validation", S_H))
replay = [
    ["Dataset", "Source", "Use"],
    [
        "May 2024 “Gannon” G5 storm — measured peak (Kp 9, Bz −50 nT, X5.8, 208 pfu) + GFZ 48-hr Kp timeline",
        "NOAA SWPC G5 event reports; GOES-16 XRS + DSCOVR; GFZ Potsdam (kp.gfz.de, CC BY 4.0)",
        "Labeled “REPLAY” demo scenarios — one real storm shown through different mission lenses",
    ],
]
story.append(table(replay, [2.6 * inch, 2.7 * inch, 2.2 * inch]))

# ── Operator-supplied ─────────────────────────────────────────────────────────
story.append(Paragraph("Operator-supplied — last-resort, disconnected ops", S_H))
manual = [
    ["Input", "Source", "Use"],
    [
        "Manual Kp entry (+ optional flare class, proton flux)",
        "Operator's S2 weather brief or space weather officer",
        "When no live feed and no cache — runs the same doctrine rules, labeled MANUAL; expires after 3 h",
    ],
]
story.append(table(manual, [2.4 * inch, 2.6 * inch, 2.5 * inch]))

story.append(Spacer(1, 4))
story.append(HRFlowable(width="100%", thickness=0.75, color=RULE, spaceAfter=3))
story.append(
    Paragraph(
        "<b>Disconnected operation:</b> a pre-mission sync caches the full feed state (incl. the 3-day "
        "forecast); IonShield then serves that carried state in ADVISORY mode with no internet. Data "
        "quality degrades honestly with age — it never fabricates freshness.",
        S_SMALL,
    )
)
story.append(
    Paragraph(
        "IonShield · PNT &amp; communications mission assurance · live NOAA SWPC ingest · UDS/Zarf air-gap deployable",
        ParagraphStyle("f", parent=S_SMALL, textColor=ACCENT),
    )
)

doc.build(story)
print(f"Wrote {OUT}")
