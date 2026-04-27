package com.ionshield.atak;

/**
 * IonShield ATAK plugin entry point — scaffolding only.
 *
 * The Android engineer extends this from the actual ATAK plugin base class
 * (com.atakmap.android.maps.MapComponent or the newer plugin SDK base) once
 * they import the signed ATAK jar from tak.gov.
 *
 * Lifecycle the plugin needs to wire:
 *   onCreate(MapView)        — register IonShield overlay layer with ATAK
 *   onConfigChanged()        — settings activity changed (API key, refresh interval)
 *   onDestroyImpl()          — cancel HTTP polling, persist last-known overlay
 *
 * Polling cadence: every 5 min by default. Backs off to 15 min when the
 * device reports degraded comms (DDIL); reads from the last good
 * offline-pack.kmz when fully disconnected.
 */
public class IonShieldPlugin {

    private static final String TAG = "IonShield";
    private final IonShieldClient client;

    public IonShieldPlugin(String baseUrl, String apiKey) {
        this.client = new IonShieldClient(baseUrl, apiKey);
    }

    /** Called by ATAK when the plugin loads. Returns null on success, error message on failure. */
    public String onCreate() {
        // TODO(android): hook to ATAK MapView, register overlay, start polling thread
        return null;
    }

    /** Pull current risk overlay (GeoJSON) and the 24h Kp forecast. */
    public void refresh() throws Exception {
        // TODO(android): client.getRiskGeoJson() → render polygons via ATAK MapView API
        // TODO(android): client.getKpForecast() → push amber/red banner if peak severity ≥ G3
        client.getRiskGeoJson();
        client.getKpForecast();
    }

    /** Best-effort DDIL fallback: pull a snapshot KMZ to local cache. */
    public void cacheOfflinePack() throws Exception {
        // TODO(android): client.downloadOfflinePack() → write to plugin sandbox dir
        client.downloadOfflinePack();
    }
}
