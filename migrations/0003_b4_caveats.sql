-- B4 caveat fixes — adds DB-backed video registration so registrations
-- survive ephemeral filesystems on free-tier deploys.

CREATE TABLE IF NOT EXISTS scenario_videos (
    scenario_id      TEXT     NOT NULL PRIMARY KEY,
    video_url        TEXT     NOT NULL,
    duration_seconds REAL,
    rendered_at      TIMESTAMP,
    notes            TEXT     NOT NULL DEFAULT '',
    created_at       TIMESTAMP NOT NULL
);
