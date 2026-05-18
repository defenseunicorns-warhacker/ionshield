# IonShield ATAK Backend — Complete Setup Guide

## File Tree
```
ionshield-backend/
├── main.py              ← Full backend (530 lines, all-in-one)
├── requirements.txt     ← Python dependencies  
├── Procfile             ← Railway/Render start command
└── README.md            ← This file
```

## What This Does
- Fetches live NOAA SWPC data every 5 minutes (Kp, X-ray, solar wind, protons)
- Computes per-location ionospheric risk using physics-based models
- Serves a KML overlay that ATAK-CIV loads as a network link
- Provides location and route analysis APIs

## Endpoints
| Endpoint | Method | What It Does |
|----------|--------|-------------|
| `/overlay/risk.kml` | GET | KML for ATAK network link (risk zones + military bases) |
| `/overlay/risk.geojson` | GET | Same data as GeoJSON |
| `/api/risk/location?lat=X&lon=Y` | GET | Risk assessment at a coordinate |
| `/api/risk/route` | POST | Per-waypoint route analysis |
| `/api/status` | GET | System health + current Kp |

---

## DEPLOYMENT — OPTION A: RAILWAY (Recommended)

### Step 1: Create a Railway account
Go to [railway.app](https://railway.app) and sign in with GitHub.

### Step 2: Push code to GitHub
```bash
cd ionshield-backend
git init
git add .
git commit -m "IonShield ATAK backend v2"
```
Create a repo on GitHub called `ionshield-backend`. Push:
```bash
git remote add origin https://github.com/YOUR_USERNAME/ionshield-backend.git
git branch -M main
git push -u origin main
```

### Step 3: Deploy on Railway
1. In Railway dashboard, click "New Project" → "Deploy from GitHub Repo"
2. Select `ionshield-backend`
3. Railway auto-detects Python + Procfile
4. Set start command if needed: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Railway assigns a URL like: `https://ionshield-backend-production.up.railway.app`
6. Done. It auto-deploys on every git push.

### Step 4: Verify
Open in browser:
```
https://YOUR-RAILWAY-URL/api/status
```
Should return JSON with `kp_current`, `fetch_source: "live"`, etc.

---

## DEPLOYMENT — OPTION B: RENDER (Free Tier)

### Step 1: Create account at [render.com](https://render.com)

### Step 2: New → Web Service → Connect GitHub repo

### Step 3: Configure:
- **Build command:** `pip install -r requirements.txt`
- **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
- **Instance type:** Free

### Step 4: Deploy → get URL like `https://ionshield-backend.onrender.com`

Note: Render free tier sleeps after 15 min of inactivity. First request after sleep takes ~30 seconds.

---

## TESTING COMMANDS

```bash
# Status
curl https://YOUR-URL/api/status

# Location — Thule AFB (polar, high risk)
curl "https://YOUR-URL/api/risk/location?lat=76.5&lon=-68.7"

# Location — Schriever SFB (mid-lat, low risk)  
curl "https://YOUR-URL/api/risk/location?lat=38.8&lon=-104.5"

# KML overlay (open in browser)
https://YOUR-URL/overlay/risk.kml

# Route analysis
curl -X POST https://YOUR-URL/api/risk/route \
  -H "Content-Type: application/json" \
  -d '{"waypoints":[
    {"lat":49.4,"lon":7.6,"name":"Ramstein AB"},
    {"lat":55,"lon":10,"name":"Baltic"},
    {"lat":64.3,"lon":20,"name":"Sweden"},
    {"lat":69,"lon":25,"name":"Arctic Norway"}
  ]}'
```

---

## ATAK-CIV SETUP (CRITICAL)

### Step 1: Install ATAK-CIV
- Open Google Play Store on your Android device
- Search "ATAK-CIV" (developer: TAK Product Center)
- Install (free, ~200MB)
- Open ATAK, go through initial setup (callsign, team color — doesn't matter what you pick)

### Step 2: Import IonShield KML Network Link
1. Open ATAK
2. Tap the hamburger menu (☰) or the layers icon
3. Go to: **Import** → **Import Manager**
4. Tap **Network Link** (or "KML Network Link")
5. Enter the URL:
   ```
   https://YOUR-RAILWAY-URL/overlay/risk.kml
   ```
6. Set **Refresh Interval**: 600 seconds (10 minutes)
7. Tap **Add** or **Import**

### Step 3: Verify
- Colored latitude bands should appear on the map (green at mid-latitudes, amber at poles)
- Military base markers should appear (Thule, Clear, Schriever, etc.)
- Tap any zone → popup shows GPS error, HF absorption, SATCOM loss, risk level
- Tap any base marker → popup shows full location assessment

### Step 4: Zoom and Explore
- Pinch to zoom into different regions
- Tap different latitude zones to see how risk changes
- Tap Thule (76°N) vs Schriever (38°N) to see the difference

---

## TROUBLESHOOTING

| Issue | Cause | Fix |
|-------|-------|-----|
| KML doesn't load in ATAK | URL not HTTPS | Railway/Render provide HTTPS by default. Verify URL starts with https:// |
| Zones don't appear | KML parsing error | Open URL in browser first — should show XML. If it shows error, backend isn't running |
| "Network error" in ATAK | Device not on WiFi/cellular | Ensure internet connection on the Android device |
| Stale data | Refresh interval too long | Set to 600 seconds. Or manually re-import the network link |
| Placemarks not tappable | ATAK zoom level | Zoom in closer to the base markers. They may be clustered at global zoom |
| Backend sleeping (Render) | Free tier spins down | First load takes 30s. Or upgrade to paid ($7/mo) |
| All values show Kp=2 | NOAA fetch failed | Check `/api/status` — if `fetch_source` is "fallback", NOAA is temporarily down |

---

## FINAL ACCEPTANCE CHECKLIST

- [ ] Backend is deployed and accessible via HTTPS URL
- [ ] `https://YOUR-URL/api/status` returns JSON with live Kp value
- [ ] `https://YOUR-URL/overlay/risk.kml` opens in browser and shows XML
- [ ] ATAK-CIV is installed on Android device
- [ ] KML network link imported into ATAK
- [ ] Colored risk zones visible on ATAK map
- [ ] Military base placemarks visible
- [ ] Tapping a zone shows GPS error, HF absorption, risk level
- [ ] Tapping a base shows full assessment
- [ ] `/api/risk/location` endpoint returns data when called
- [ ] `/api/risk/route` endpoint returns per-waypoint analysis
- [ ] Auto-refresh works (zones update after 10 minutes)

---

## WHAT WAS BUILT FOR YOU vs WHAT YOU MUST DO

### Built for you (ready to use):
- ✅ Complete FastAPI backend (main.py, 530 lines)
- ✅ NOAA data fetcher with per-feed resilience
- ✅ Physics-based risk computation engine
- ✅ KML generator (ATAK-compatible)
- ✅ GeoJSON generator
- ✅ Location analysis endpoint
- ✅ Route analysis endpoint
- ✅ Status/health endpoint
- ✅ requirements.txt + Procfile for deployment

### You must do:
1. **Create a GitHub repo** and push the code
2. **Deploy to Railway or Render** (10 minutes, free)
3. **Install ATAK-CIV** on an Android device (Google Play, free)
4. **Import the KML network link** into ATAK with your deployed URL
5. **Verify** everything works per the checklist above
6. **Record a 90-second demo video** (Loom or screen recording)
