# IonShield Replay System

Replay lets you rerun any past decision against its original NOAA observation
snapshot and get the exact same recommendation the engine would have produced
at that moment.  The `input_hash` in every response is a SHA-256 fingerprint of
the environment inputs; the same snapshot always produces the same hash.

---

## How it works

1. **Archiving** — after every NOAA fetch (startup + each refresh interval),
   `archive_snapshot()` persists the live NOAA cache to the `noaa_snapshots`
   table in SQLite (or PostgreSQL).
2. **Reconstruction** — `snapshot_row_to_env()` converts a stored row back into
   a fully typed `EnvironmentSnapshot` — bit-for-bit equivalent to the one used
   at fetch time.
3. **Replay** — the replay endpoints feed that reconstructed snapshot through the
   same stateless `DecisionEngine` used by the live endpoints.  No re-fetch, no
   approximation.

---

## Snapshot locator

All replay endpoints accept an optional snapshot selector.  Resolution order:

| Parameter | How it resolves |
|-----------|----------------|
| `snapshot_id=<int>` | Exact primary-key lookup — 404 if not found |
| `at=<ISO-8601>` | Most-recent snapshot at-or-before that UTC timestamp — 404 if none exist before that time |
| *(neither)* | Most-recent snapshot in the DB (equivalent to `at=now`) |

If both `snapshot_id` and `at` are supplied, `snapshot_id` wins.

---

## Endpoints

### `GET /api/v2/replay`

Replay a comms-decision for a single observer position.

**Query parameters** (same as `/api/v2/comms-decision` plus snapshot locator):

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `lat` | float | yes | Observer latitude (−90 … 90) |
| `lon` | float | yes | Observer longitude (−180 … 180) |
| `dest_lat` | float | no | Destination latitude (enables link geometry) |
| `dest_lon` | float | no | Destination longitude |
| `snapshot_id` | int | no | Snapshot selector |
| `at` | string | no | ISO-8601 UTC timestamp selector |

**Response** — identical schema to `GET /api/v2/comms-decision` plus a
`replay` block:

```json
{
  "replay": {
    "snapshot_id": 42,
    "fetched_at": "2024-05-11T17:00:00+00:00",
    "fetch_source": "live",
    "kp_at_snapshot": 8.3,
    "replay_note": "Decision replayed from archived snapshot id=42 (fetched 2024-05-11T17:00:00+00:00, source=live)"
  },
  "recommendation": { ... },
  "provenance": {
    "input_hash": "a3f9...",
    ...
  }
}
```

**Example — replay the G5 storm on 2024-05-11:**
```bash
curl "http://localhost:8000/api/v2/replay?lat=38.9&lon=-77.0&at=2024-05-11T18:00:00Z"
```

**Example — replay by exact snapshot ID:**
```bash
curl "http://localhost:8000/api/v2/replay?lat=38.9&lon=-77.0&snapshot_id=42"
```

---

### `POST /api/v2/replay/route`

Replay a route-decision for a multi-waypoint path.

**Request body:**
```json
{
  "snapshot_id": 42,
  "waypoints": [
    {"lat": 38.9, "lon": -77.0},
    {"lat": 39.1, "lon": -76.8}
  ],
  "platform": "hmmwv"
}
```

`at` may be supplied instead of `snapshot_id`; omit both for latest.

**Response** — identical schema to `POST /api/v2/route-decision` plus a
`replay` block (same structure as above).

---

## Listing snapshots

### `GET /api/v2/snapshots`

Returns a paginated list of stored snapshots, most-recent first.

| Query param | Default | Description |
|-------------|---------|-------------|
| `limit` | 20 | Max rows to return (1–100) |
| `offset` | 0 | Rows to skip |

### `GET /api/v2/snapshots/{snapshot_id}`

Returns a single snapshot row by primary key.  404 if not found.

---

## Running locally

SQLite is the default — no extra config needed:

```bash
# Start the server (DB created automatically at ./ionshield.db)
uvicorn app.main:app --reload

# List recent snapshots
curl http://localhost:8000/api/v2/snapshots

# Replay the most recent snapshot
curl "http://localhost:8000/api/v2/replay?lat=38.9&lon=-77.0"
```

### Switching to PostgreSQL

```bash
export DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/ionshield"
# Apply the schema once
psql $DATABASE_URL < migrations/0001_initial.sql
uvicorn app.main:app --reload
```

### Disabling archiving

```bash
export ARCHIVE_ENABLED=false
uvicorn app.main:app --reload
```

When `ARCHIVE_ENABLED=false`, `archive_snapshot()` returns immediately without
writing to the DB.  Replay endpoints will return 404 (no snapshots stored).

---

## Determinism guarantee

The `provenance.input_hash` is a SHA-256 digest of the serialised
`EnvironmentSnapshot` fields.  For any given `snapshot_id`, the hash is
invariant across: server restarts, Python version upgrades, and concurrent
requests.  The test `test_replay_comms_hash_matches_live_decision` in
`tests/test_replay.py` encodes this contract: it calls both the HTTP endpoint
and `DecisionEngine.comms_fallback()` directly and asserts the hashes match.
