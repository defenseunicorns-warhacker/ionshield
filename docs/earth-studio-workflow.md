# IonShield Earth Studio export workflow

End-to-end: pick scenario → render mp4 → publish back into the Simulation
page. Steps 1–3 and 7 are scripted. Steps 4–6 are unavoidable manual UI
work in Earth Studio (Google's web app has no public automation surface).

> **Prerequisites**
>   - A Google account with [Earth Studio](https://earth.google.com/studio/) access
>     (currently invite-only — request via the link)
>   - Cloud-hosted IonShield deployment with the v3 API + scenarios
>     precomputed (run `scripts/backfill_production.sh` then
>     `scripts/precompute_scenarios.sh` once after deploy)
>   - `curl`, `jq`, and a writable storage bucket / static host for the
>     rendered mp4 (S3, R2, GCS, even GitHub Releases — anywhere
>     publicly fetchable)

---

## 1. Pick a scenario and pull the artifacts

```bash
SID=may-2024-g5
HOST=https://ionshield-demo-mvp.onrender.com
./scripts/prepare_scenario.sh "$SID" "$HOST" /tmp/ionshield-build
```

That produces:

```
/tmp/ionshield-build/
├── scenario.kmz             ← layered overlay (HF/GPS/SATCOM folders + legend)
├── keyframes.csv            ← Earth Studio Tracks input
├── recipe.json              ← suggested camera path + render settings
└── README.txt               ← step-by-step pointers to this doc
```

`prepare_scenario.sh` is idempotent — re-running fetches the latest
content-hashed assets via the cache-busted URLs from
`/api/v3/scenarios`.

---

## 2. Open Earth Studio and create the project

1. Open [Earth Studio](https://earth.google.com/studio/) in Chrome.
2. **New Project** → Standard Quality (1080p) — this matches the
   `recipe.json.render` block. For 4K renders, pick the `4K` preset and
   bump the recipe's `width/height` later.
3. Set the **Total Duration** from `recipe.json.duration_seconds` (range
   22–45 s across the 7 catalog scenarios).
4. Set **Frame rate** from `recipe.json.frame_rate` (default 30).

---

## 3. Import the KMZ overlay

1. Project panel → **Import KML** → select the `scenario.kmz` from
   step 1.
2. Earth Studio reads the `<TimeSpan>` blocks and auto-creates
   keyframes — polygons recolor across the storm timeline.
3. Toggle the three folders (HF Risk / GPS Risk / SATCOM Risk) to pick
   which layer is the visual focus. The **legend.png** ScreenOverlay
   appears in the upper-left corner.
4. *(If the import opens to a non-Earth view)*: right-click the layer →
   "Frame to Earth" — Earth Studio occasionally bounces the camera to
   the centroid of the polygons, which is mid-Atlantic.

---

## 4. Drive the camera from the recipe

The `recipe.camera` array is a list of camera waypoints with `t` (seconds
from start), `lat / lon / altitude_m`, and `heading / tilt`. Two ways to
apply them:

**Option A — manual keyframes** (more control)
1. Camera panel → set time slider to `t = 0` → set view from waypoint 0.
2. Right-click the camera property → **Add Keyframe**.
3. Repeat for each waypoint in the recipe.
4. Earth Studio auto-interpolates between them; tweak the easing if the
   default smoothing looks too aggressive.

**Option B — drag-drop the keyframes CSV** (faster)
1. Tracks panel → **+ Add Track** → **Import CSV**.
2. Select `keyframes.csv` from step 1.
3. Earth Studio reads the time / lat / lon / kp / hf_absorption columns
   and creates one numeric track per metric. The simulation uses these
   to drive the camera POI follow-target and to display per-frame
   metrics as text overlays.

Either path is valid; B is faster but A produces cleaner motion for
publication-quality renders.

---

## 5. Optional polish

- **Text overlay** showing the live Kp and HF absorption: bind a Text
  layer to the `kp` and `hf_absorption_db` tracks. The CSV's column
  headers map directly.
- **Logo / lower-third** with the IonShield brand: drop a PNG into a
  layer above the Earth view, anchor lower-right.
- **Date readout** ticking through the storm: bind a Time layer to the
  `time_tag` column.

---

## 6. Render

1. **Render** → **mp4** at the resolution from `recipe.render`.
2. Earth Studio queues the render in Google's cloud (Earth Studio's
   client doesn't render locally). Wait time: typically 5–15 min for a
   30 s 1080p render.
3. Download the mp4 when it lands in your Earth Studio inbox.

---

## 7. Publish back into IonShield

Upload the mp4 to your storage host of choice, then register the URL:

```bash
./scripts/publish_scenario_video.sh \
  may-2024-g5 \
  https://cdn.example.com/ionshield/may-2024-g5.mp4 \
  --duration 30
```

The script POSTs to `/api/v3/scenarios/{id}/video` which writes a
`video.json` sidecar next to the precomputed assets. The catalog
endpoint merges this into the scenario's `video_url` field on every
read, so the Simulation Mode page's right panel auto-displays the video
without any code change.

To remove a video later:

```bash
curl -X DELETE -H "X-API-Key: $IONSHIELD_API_KEY" \
  "$HOST/api/v3/scenarios/may-2024-g5/video"
```

---

## File-format references

| Format | Built by | Consumed by |
|---|---|---|
| `scenario.geojson` | B1 export | Simulation Mode polygon layer |
| `scenario.kmz` | B2 converter | Earth Studio (camera + overlay) |
| `keyframes.csv` | B2 converter | Earth Studio Tracks |
| `recipe.json` | This doc + scenarios.json | Operator following the runbook |
| `legend.png` | B2 stdlib renderer | Earth Studio ScreenOverlay |
| `manifest.json` | B3 precompute | Catalog endpoint cache-busting |
| `video.json` | B4 publish | Catalog endpoint video-url merge |
