"""
Build the one-page IonShield science backgrounder PDF.

Audience: technical judges, USSF weather officers, anyone at WarHacker who
asks "what's actually under the hood?" Print-friendly (light background),
single page, every model cited.

Run:    python3 scripts/build_science_backgrounder.py
Output: docs/IonShield_Science_Backgrounder.pdf
"""

from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

INK = HexColor("#10181f")
MUTED = HexColor("#5a6672")
ACCENT = HexColor("#0e7490")  # deep cyan, prints well
RULE = HexColor("#c9d2da")

S_TITLE = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=17, textColor=INK, spaceAfter=1)
S_SUB = ParagraphStyle("s", fontName="Helvetica", fontSize=8.5, textColor=MUTED, spaceAfter=6)
S_H = ParagraphStyle(
    "h", fontName="Helvetica-Bold", fontSize=9.5, textColor=ACCENT, spaceBefore=7, spaceAfter=2
)
S_BODY = ParagraphStyle("b", fontName="Helvetica", fontSize=8.2, leading=10.6, textColor=INK)
S_SMALL = ParagraphStyle("sm", fontName="Helvetica", fontSize=7.4, leading=9.4, textColor=MUTED)
S_CELL = ParagraphStyle("c", fontName="Helvetica", fontSize=7.6, leading=9.6, textColor=INK)
S_CELL_B = ParagraphStyle("cb", fontName="Helvetica-Bold", fontSize=7.6, leading=9.6, textColor=INK)

OUT = Path(__file__).resolve().parent.parent / "docs" / "IonShield_Science_Backgrounder.pdf"
OUT.parent.mkdir(exist_ok=True)

doc = SimpleDocTemplate(
    str(OUT),
    pagesize=letter,
    leftMargin=0.55 * inch,
    rightMargin=0.55 * inch,
    topMargin=0.45 * inch,
    bottomMargin=0.4 * inch,
    title="IonShield Science Backgrounder",
    author="IonShield",
)

story = []
story.append(Paragraph("IonShield — Science Backgrounder", S_TITLE))
story.append(
    Paragraph(
        "How measured space weather becomes a mission decision. One page; every model cited; "
        "every output labeled measured / modeled / doctrine.",
        S_SUB,
    )
)
story.append(HRFlowable(width="100%", thickness=1.5, color=ACCENT, spaceAfter=5))

story.append(Paragraph("The physical problem", S_H))
story.append(
    Paragraph(
        "The ionosphere (60–1,000 km altitude) is plasma created by solar radiation. Every RF signal "
        "that crosses it (GPS, SATCOM) or refracts off it (HF skywave) is affected by its free-electron "
        "content. Solar flares, coronal mass ejections, and energetic-particle events change that content "
        "on timescales of minutes (flares: 8-minute light-travel warning) to days (CMEs: 1–2 day transit). "
        "Four physically distinct effects follow; IonShield models each separately, per location, because "
        "geomagnetic latitude, local solar time, and equipment class control who gets hit.",
        S_BODY,
    )
)

story.append(Paragraph("Measured inputs (5-minute ingest cadence, SHA-256 provenance on every value)", S_H))
inputs = Table(
    [
        [
            Paragraph("<b>Kp index</b> — global geomagnetic disturbance, 0–9 (NOAA/GFZ magnetometers)", S_CELL),
            Paragraph("<b>IMF B<sub>z</sub></b> — solar-wind field N–S component, DSCOVR @ L1. Sustained southward (&lt; −10 nT) reconnects with the geomagnetic field: the storm trigger", S_CELL),
        ],
        [
            Paragraph("<b>GOES X-ray flux</b> (1–8 Å) — flare class A/B/C/M/X is log-scale flux", S_CELL),
            Paragraph("<b>GOES ≥10 MeV protons</b> — energetic particles funneled into the polar caps", S_CELL),
        ],
        [
            Paragraph("<b>GloTEC</b> — measured global Total Electron Content map", S_CELL),
            Paragraph("<b>Solar wind speed/density, F10.7</b>, NOAA 3-day Kp forecast + forecaster R/S/G probabilities", S_CELL),
        ],
    ],
    colWidths=[3.65 * inch, 3.75 * inch],
)
inputs.setStyle(
    TableStyle(
        [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]
    )
)
story.append(inputs)

story.append(Paragraph("Four effect models", S_H))
effects = [
    (
        "GPS error — ionospheric group delay",
        "Klobuchar 1996; Mannucci 2005 (storm enhancement)",
        "Free electrons delay the ranging code: ≈0.162 m of error per TECU at L1, × obliquity factor 2.5 "
        "(slant geometry), × storm scaling. Measured GloTEC used where available. Delay scales as 1/f², so "
        "dual-frequency receivers (L1/L2, L1/L5) solve for and cancel it — single-frequency receivers "
        "(DAGR, Group 1 UAS) take the full error. That receiver-class split is why doctrine records "
        "100 m horizontal / 200 m vertical on exactly that equipment.",
    ),
    (
        "HF blackout — D-layer absorption",
        "CCIR-888 / Spencer 1971 (SID); Rose & Ziauddin 1962 (storm); Bailey 1964 (PCA)",
        "HF works by refracting off the F-layer; it dies when the lower D-layer absorbs it first. Three "
        "additive sources (dB): flare X-rays ionize the sunlit D-layer in minutes (solar-zenith-angle "
        "scaled); Kp-driven precipitation at high geomagnetic latitude; and polar cap absorption — solar "
        "protons (≥10 pfu, NOAA S1) blanketing HF poleward of ~65° for days. Total absorption vs ~25 dB "
        "fade margin → blackout probability. SINCGARS FM (30–88 MHz line-of-sight) never transits the "
        "ionosphere — immune; EHF passes through at f high enough that 1/f² effects are negligible.",
    ),
    (
        "SATCOM dropout — scintillation",
        "Basu 1988 / Aquino 2005 (S4 regimes); ITU-R P.531 Nakagami-m fading",
        "Storms make the ionosphere patchy; signals self-interfere and received power flickers — the S4 "
        "index. S4 estimated per regime (equatorial post-sunset plasma bubbles; Kp-driven auroral; "
        "proton-driven polar), then Nakagami-m statistics (m ≈ 1/S4²) convert S4 to fade depth and outage "
        "probability against the link margin. The physics behind UHF TACSAT 'broken and unreadable.'",
    ),
    (
        "Radar degradation — range bias + clutter",
        "Skolnik (group delay 40.3·TEC/f²); CCIR Faraday rotation",
        "Radar ranges by echo timing; ionospheric group delay adds 40.3·TEC/f² metres of apparent range "
        "plus polarization rotation. Scales as 1/f²: L-band counter-battery radar (AN/TPQ-53) hit hardest, "
        "X-band barely affected — plus auroral clutter producing false/missed tracks.",
    ),
]
for title, cite, body in effects:
    story.append(
        Paragraph(
            f"<b>{title}</b> &nbsp;<font size=6.8 color='#5a6672'>[{cite}]</font><br/>{body}",
            ParagraphStyle("e", parent=S_BODY, spaceBefore=2, spaceAfter=2),
        )
    )

story.append(Paragraph("From physics to verdict", S_H))
story.append(
    Paragraph(
        "Per-waypoint metrics (GPS error in metres, HF absorption in dB, S4, fade probability) are computed "
        "at each location — geomagnetic latitude, dayside/nightside, polar cap membership all matter. A "
        "mission layer applies the operator's tolerances (0.5 m breaks RTK survey; 25 m is fine for a "
        "patrol) to produce CLEAR / CAUTION / HIGH RISK / DELAY. On top, a doctrine rule library (5 "
        "equipment classes × 3 weather states, each rule citing ALSSA 'True Impacts of Space Weather on a "
        "Ground Force' or NOAA scale definitions) speaks in equipment names — the physics carries the "
        "numbers underneath. ML (ridge-regression Kp forecaster, event classifier) forecasts drivers only; "
        "it never overrides the physics. Recommendations are probabilistic windows, never binary go/no-go.",
        S_BODY,
    )
)

story.append(HRFlowable(width="100%", thickness=0.75, color=RULE, spaceBefore=6, spaceAfter=3))
story.append(
    Paragraph(
        "<b>Honest limitations:</b> propagation models are published simplified analytical forms, not "
        "ray-tracing or assimilative ionosphere models. S4 lacks solar-cycle/seasonal terms; HF output is "
        "not yet frequency-specific; GPS error awaits validation against CORS receiver data during storms "
        "(planned Phase II with USSF weather officers). Every output is labeled measured / modeled / "
        "doctrine-derived, and historical replay (e.g. May 2024 Gannon G5 storm, GFZ definitive Kp) lets "
        "the engine be checked against documented events.",
        S_SMALL,
    )
)
story.append(Spacer(1, 3))
story.append(
    Paragraph(
        "IonShield · operational space weather intelligence · live NOAA SWPC ingest · UDS/Zarf air-gap "
        "deployable · ATAK KML/CoT outputs",
        ParagraphStyle("f", parent=S_SMALL, textColor=ACCENT),
    )
)

doc.build(story)
print(f"Wrote {OUT}")
