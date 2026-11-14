"""SQLite storage for hourly PM2.5 observations and publish previews."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS published_records (
    date        TEXT    NOT NULL,
    station     TEXT    NOT NULL,
    hour        TEXT    NOT NULL,
    revision    INTEGER NOT NULL CHECK (revision >= 0),
    value       TEXT    NOT NULL,
    batch_id    TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    source_station TEXT,
    PRIMARY KEY (date, station, hour)
);

CREATE INDEX IF NOT EXISTS idx_published_date_hour
    ON published_records (date, hour, station);

CREATE TABLE IF NOT EXISTS day_versions (
    date       TEXT    PRIMARY KEY,
    version    INTEGER NOT NULL CHECK (version >= 0),
    updated_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS previews (
    id          TEXT PRIMARY KEY,
    date        TEXT NOT NULL,
    day_version INTEGER NOT NULL,
    status      TEXT NOT NULL,
    preview_json TEXT NOT NULL,
    plan_json   TEXT NOT NULL,
    result_json TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_previews_date_status
    ON previews (date, status);

-- A station that has been renamed is linked to its current identity.  Both the
-- retired and the current identifier get a row, so the primary key guarantees
-- that an identifier can take part in at most one merge.
CREATE TABLE IF NOT EXISTS station_identities (
    station           TEXT PRIMARY KEY,
    canonical_station TEXT NOT NULL,
    merge_id          TEXT NOT NULL,
    merged_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_station_identities_canonical
    ON station_identities (canonical_station);

CREATE TABLE IF NOT EXISTS merge_previews (
    id            TEXT PRIMARY KEY,
    old_station   TEXT NOT NULL,
    new_station   TEXT NOT NULL,
    status        TEXT NOT NULL,
    preview_json  TEXT NOT NULL,
    guard_json    TEXT NOT NULL,
    result_json   TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_merge_previews_status
    ON merge_previews (status);
"""


def default_db_path() -> Path:
    configured = os.environ.get("PM25_DB_PATH")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent / "data" / "pm25.db"


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open one short-lived connection with explicit transaction control."""
    connection = sqlite3.connect(
        str(db_path),
        timeout=15,
        isolation_level=None,  # transactions are started explicitly
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


def initialize(db_path: str | Path) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect(path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.executescript(SCHEMA)
        _migrate(connection)
    finally:
        connection.close()


def _migrate(connection: sqlite3.Connection) -> None:
    """Apply additive column migrations to databases created by older builds."""
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(published_records)")
    }
    if "source_station" not in columns:
        connection.execute(
            "ALTER TABLE published_records ADD COLUMN source_station TEXT"
        )
