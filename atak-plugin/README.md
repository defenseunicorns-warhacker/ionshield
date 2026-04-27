# IonShield ATAK Plugin — Scaffolding

This directory is a starter for a native ATAK plugin (`.apk`) that wraps
IonShield's HTTP/KML/CoT endpoints into a first-class ATAK overlay with:

- Proactive alerts when a region crosses a severity threshold
- Persistent DDIL cache (last good `offline-pack.kmz` survives reboots)
- Route-overlay mode: paint route segments by predicted Kp at flight time
- Per-asset settings (ATAK callsign → IonShield tenant key)

It is **not built** here — IonShield's main repo is Python. Hand this
directory to an Android engineer; they fork it into the official ATAK
plugin SDK (https://tak.gov/products/atak-civ) and finish the wiring.

---

## What's here

| File | Purpose |
|---|---|
| `AndroidManifest.xml` | ATAK plugin manifest with required permissions |
| `build.gradle` | Plugin module gradle config (template) |
| `src/main/java/com/ionshield/atak/IonShieldPlugin.java` | Plugin entry point — wires the lifecycle hooks |
| `src/main/java/com/ionshield/atak/IonShieldClient.java` | HTTP client wrapping IonShield endpoints |
| `src/main/res/values/strings.xml` | UI strings |

## What's not here (Android dev to add)

- ATAK SDK dependency (proprietary, signed jar from tak.gov)
- Map overlay rendering (uses ATAK's MapView API)
- Alert dispatch (ATAK Notification API)
- Settings activity (ATAK Settings preference category)
- Code signing for distribution

## Endpoints the plugin consumes

The plugin only needs IonShield's public HTTP API. No local state required.

```
GET  /overlay/risk.geojson           — current risk grid
GET  /atak/offline-pack.kmz          — DDIL cache snapshot
GET  /api/v3/forecast/kp             — 24h Kp prediction (auth: Bearer)
POST /api/risk/route                 — per-waypoint route analysis
```

Auth: `Authorization: Bearer iks_<key>` (mint via `/api/v3/admin/keys`).

## Build (placeholder)

```bash
# After importing into the official ATAK plugin SDK:
./gradlew :ionshield-plugin:assembleCivRelease
# Output: build/outputs/apk/civ/release/ionshield-plugin-civ-release.apk
```

## Operator install

1. Sideload the signed APK to the ATAK device
2. Open ATAK → Settings → Tool Preferences → IonShield
3. Paste the API key minted from `/api/v3/admin/keys`
4. The IonShield overlay layer appears and refreshes every 5 min
