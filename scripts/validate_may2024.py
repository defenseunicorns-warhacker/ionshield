"""
IonShield — May 2024 G5 Storm Validation Script

Replays the May 10–13 2024 geomagnetic superstorm (G5, peak Kp=9) through
the IonShield risk model and HF link engine using hardcoded historical values.

Route validated: LAX → LHR polar track
  MODOG: 62.1°N, 28.4°W  (North Atlantic, sub-auroral)
  MIMKU: 67.3°N, 18.2°W  (sub-auroral/polar boundary)
  GUNSO: 71.8°N,  8.1°W  (deep polar, Kp-sensitive)

Historical G5 documented impacts (reference):
  GPS errors: 10–50 m reported at high latitudes (FAA AASR)
  HF blackouts: 24+ hours on polar cap routes
  PCA events: S3–S4 proton storm, polar HF blackout

Usage:
  cd /path/to/ionshield-backend
  python scripts/validate_may2024.py
"""

import csv
import os
import sys
from datetime import datetime, timezone

# Ensure the repo root is on sys.path so `app` is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Patch the NOAA cache before importing risk functions so there's no
# live network call at import time
from app.data import noaa as _noaa  # noqa: E402

_noaa._cache["fetch_source"] = "hardcoded_validation"
_noaa._cache["last_fetch"] = datetime.now(timezone.utc).isoformat()

from app.models.risk import compute_risk, compute_hf_link  # noqa: E402

# ── Historical storm sequence ─────────────────────────────────────────────────
# Sources: NOAA SWPC event archive; Kp from GFZ Potsdam 1-minute data;
#          Bz from DSCOVR L1 magnetometer (ACE backup); X-ray from GOES-16;
#          Proton from GOES-18 ≥10 MeV integral channel.
#
# May 2024 was driven by AR3664, one of the most active regions of Solar Cycle 25.
# The event produced X-class flares (X2.2, X1.0, X3.9) and a complex full-halo CME.

STORM_STEPS = [
    # timestamp            kp    bz(nT)  xray(W/m²)  proton(pfu)  notes
    {
        "time": "2024-05-10 18:00Z",
        "kp": 5.3,
        "bz": -10.0,
        "xray": 5e-5,
        "proton": 15.0,
        "note": "Storm onset",
    },
    {
        "time": "2024-05-10 21:00Z",
        "kp": 7.0,
        "bz": -10.0,
        "xray": 5e-5,
        "proton": 50.0,
        "note": "G3 storm",
    },
    {
        "time": "2024-05-11 00:00Z",
        "kp": 8.3,
        "bz": -25.0,
        "xray": 1e-4,
        "proton": 1000.0,
        "note": "G5 peak — X1 flare, S3 proton",
    },
    {
        "time": "2024-05-11 03:00Z",
        "kp": 8.0,
        "bz": -25.0,
        "xray": 1e-4,
        "proton": 1000.0,
        "note": "G5 sustained",
    },
    {
        "time": "2024-05-11 06:00Z",
        "kp": 7.3,
        "bz": -10.0,
        "xray": 5e-5,
        "proton": 200.0,
        "note": "G4 declining",
    },
    {
        "time": "2024-05-11 09:00Z",
        "kp": 6.3,
        "bz": -10.0,
        "xray": 1e-5,
        "proton": 50.0,
        "note": "G2 recovery",
    },
    {
        "time": "2024-05-11 12:00Z",
        "kp": 5.0,
        "bz": -10.0,
        "xray": 1e-6,
        "proton": 10.0,
        "note": "G1 late recovery",
    },
    {
        "time": "2024-05-12 00:00Z",
        "kp": 4.3,
        "bz": 0.0,
        "xray": 1e-6,
        "proton": 1.0,
        "note": "Post-storm quiet",
    },
    {
        "time": "2024-05-13 00:00Z",
        "kp": 2.7,
        "bz": 0.0,
        "xray": 1e-7,
        "proton": 0.1,
        "note": "Background quiet",
    },
]

# ── LAX→LHR polar route waypoints ────────────────────────────────────────────
WAYPOINTS = [
    {"name": "MODOG", "lat": 62.1, "lon": -28.4},
    {"name": "MIMKU", "lat": 67.3, "lon": -18.2},
    {"name": "GUNSO", "lat": 71.8, "lon": -8.1},
]

# ── Helpers ───────────────────────────────────────────────────────────────────


def _inject_noaa(step: dict) -> None:
    """Patch the NOAA in-memory cache with hardcoded historical values."""
    _noaa._cache["kp"] = [{"kp_index": step["kp"]}]
    _noaa._cache["mag"] = [{"bz_gsm": step["bz"]}]
    # "0.1-0.8nm" satisfies the get_xray_flux() filter: "0.8" in energy string
    _noaa._cache["xray"] = [{"energy": "0.1-0.8nm", "flux": step["xray"]}]
    # ">=10 MeV" satisfies: "10" in energy and "mev" in energy.lower()
    _noaa._cache["proton"] = [{"energy": ">=10 MeV", "flux": step["proton"]}]


def _xray_class(flux: float) -> str:
    if flux >= 1e-4:
        return "X"
    if flux >= 1e-5:
        return "M"
    if flux >= 1e-6:
        return "C"
    if flux >= 1e-7:
        return "B"
    return "A"


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    print()
    print("=" * 72)
    print("  IonShield May 2024 G5 Storm Validation — LAX→LHR Polar Route")
    print("=" * 72)
    print()

    # Column header
    hdr = (
        f"{'Timestamp':<22} {'Kp':>4} {'Xray':>4}  "
        f"{'MODOG GPS':>9} {'MIMKU GPS':>9} {'GUNSO GPS':>9}  "
        f"{'MIMKU HF':>8} {'GUNSO HF?':>9}"
    )
    sep = "-" * len(hdr)
    print(hdr)
    print(sep)

    # Collect rows for CSV + summary stats
    rows: list[dict] = []
    peak_gunso_gps = 0.0
    gunso_blackout_steps = 0

    for step in STORM_STEPS:
        _inject_noaa(step)

        gps_results = {}
        hf_results = {}

        for wp in WAYPOINTS:
            risk = compute_risk(wp["lat"], wp["lon"], kp=step["kp"])
            gps_results[wp["name"]] = risk["assessment"]["gps_error_m"]

            hf = compute_hf_link(
                wp["lat"],
                wp["lon"],
                kp=step["kp"],
                bz=step["bz"],
                xray_flux=step["xray"],
                proton_flux=step["proton"],
            )
            hf_results[wp["name"]] = hf["link_summary"]

        # Track summary stats for GUNSO (most polar, most affected)
        gunso_gps = gps_results["GUNSO"]
        if gunso_gps > peak_gunso_gps:
            peak_gunso_gps = gunso_gps

        gunso_hf = hf_results["GUNSO"]
        gunso_reliable = gunso_hf["viable_count"] > 0
        if not gunso_reliable:
            gunso_blackout_steps += 1

        mimku_hf = hf_results["MIMKU"]
        mimku_best = (
            f"{mimku_hf['best_frequency_mhz']} MHz"
            if mimku_hf["best_frequency_mhz"]
            else "NONE"
        )
        gunso_ok = "YES" if gunso_reliable else "NO ⚠"

        line = (
            f"{step['time']:<22} {step['kp']:>4.1f} {_xray_class(step['xray']):>4}  "
            f"{gps_results['MODOG']:>8.1f}m "
            f"{gps_results['MIMKU']:>8.1f}m "
            f"{gps_results['GUNSO']:>8.1f}m  "
            f"{mimku_best:>8} {gunso_ok:>9}"
        )
        print(line)

        rows.append(
            {
                "timestamp": step["time"],
                "kp": step["kp"],
                "bz_nt": step["bz"],
                "xray_class": _xray_class(step["xray"]),
                "proton_pfu": step["proton"],
                "note": step["note"],
                "modog_gps_m": gps_results["MODOG"],
                "mimku_gps_m": gps_results["MIMKU"],
                "gunso_gps_m": gps_results["GUNSO"],
                "mimku_hf_best_mhz": mimku_hf["best_frequency_mhz"],
                "mimku_hf_reliability_pct": mimku_hf["best_reliability_pct"],
                "mimku_pca_active": mimku_hf["pca_active"],
                "gunso_hf_viable_count": gunso_hf["viable_count"],
                "gunso_hf_best_mhz": gunso_hf["best_frequency_mhz"],
                "gunso_hf_reliability_pct": gunso_hf["best_reliability_pct"],
                "gunso_pca_active": gunso_hf["pca_active"],
                "gunso_hf_blackout": not gunso_reliable,
            }
        )

    print(sep)
    print()

    # ── Save CSV ──────────────────────────────────────────────────────────────
    out_dir = os.path.dirname(os.path.abspath(__file__))
    csv_path = os.path.join(out_dir, "validation_results_may2024.csv")
    fieldnames = list(rows[0].keys())
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Results saved → {csv_path}")
    print()

    # ── Summary block ─────────────────────────────────────────────────────────
    peak_kp = max(s["kp"] for s in STORM_STEPS)
    peak_step = next(s for s in STORM_STEPS if s["kp"] == peak_kp)

    # Blackout duration: each step represents a ~3h window
    blackout_hours = gunso_blackout_steps * 3

    # Documented G5 range for GPS error at high latitude: 10–50 m
    within_range = 10.0 <= peak_gunso_gps <= 50.0
    above_range = peak_gunso_gps > 50.0
    if within_range:
        assessment = "WITHIN documented G5 impact range (10–50 m)"
    elif above_range:
        assessment = "ABOVE documented range — conservative model (expected for worst-case polar lat)"
    else:
        assessment = "BELOW documented range — model may underestimate at this latitude"

    print("=" * 56)
    print("  IonShield May 2024 G5 Storm Validation")
    print("=" * 56)
    print(f"  Peak Kp modeled:              {peak_kp:.1f}  ({peak_step['note']})")
    print(f"  Peak GPS error at GUNSO:      {peak_gunso_gps:.1f} m")
    print("  (Documented G5 range:         10–50 m at high lat)")
    print(
        f"  HF blackout at GUNSO (peak):  {'YES' if gunso_blackout_steps > 0 else 'NO'}"
    )
    print(f"  HF blackout duration sim:     ~{blackout_hours} hours")
    print(f"  Model assessment:             {assessment}")
    print("=" * 56)
    print()


if __name__ == "__main__":
    main()
