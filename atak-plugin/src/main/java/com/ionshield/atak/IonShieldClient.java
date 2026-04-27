package com.ionshield.atak;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.ResponseBody;

import java.util.concurrent.TimeUnit;

/**
 * IonShield HTTP client — wraps the public API endpoints the plugin needs.
 *
 * Auth: Authorization: Bearer iks_<token>. The token is minted via
 * /api/v3/admin/keys and supplied by the operator in plugin settings.
 *
 * All methods return raw response bodies; the Android engineer parses
 * them in the plugin (Moshi for JSON, ATAK's KMLParser for KMZ).
 */
public class IonShieldClient {

    private static final long TIMEOUT_S = 15;

    private final OkHttpClient http;
    private final String baseUrl;
    private final String authHeader;

    public IonShieldClient(String baseUrl, String apiKey) {
        this.baseUrl = baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length() - 1) : baseUrl;
        this.authHeader = "Bearer " + apiKey;
        this.http = new OkHttpClient.Builder()
                .connectTimeout(TIMEOUT_S, TimeUnit.SECONDS)
                .readTimeout(TIMEOUT_S, TimeUnit.SECONDS)
                .build();
    }

    public String getRiskGeoJson() throws Exception {
        return get("/overlay/risk.geojson");
    }

    public String getKpForecast() throws Exception {
        return get("/api/v3/forecast/kp");
    }

    public byte[] downloadOfflinePack() throws Exception {
        return getBytes("/atak/offline-pack.kmz");
    }

    private String get(String path) throws Exception {
        try (Response r = http.newCall(new Request.Builder()
                .url(baseUrl + path)
                .header("Authorization", authHeader)
                .header("Accept", "application/json")
                .build()).execute()) {
            if (!r.isSuccessful()) throw new RuntimeException("HTTP " + r.code() + " on " + path);
            ResponseBody body = r.body();
            return body == null ? "" : body.string();
        }
    }

    private byte[] getBytes(String path) throws Exception {
        try (Response r = http.newCall(new Request.Builder()
                .url(baseUrl + path)
                .header("Authorization", authHeader)
                .build()).execute()) {
            if (!r.isSuccessful()) throw new RuntimeException("HTTP " + r.code() + " on " + path);
            ResponseBody body = r.body();
            return body == null ? new byte[0] : body.bytes();
        }
    }
}
