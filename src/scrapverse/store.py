"""SQLite persistence for snapshots, diffs, and heal events.

A file-backed store keeps the pipeline stateful across CI runs (the DB is the
job's cache artifact) without standing up a database service for a scraper that
runs once a day.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Diff, HealEvent, Snapshot

DEFAULT_DB_PATH = Path("data/scrapverse.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target      TEXT NOT NULL,
    collector_id TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    healthy     INTEGER NOT NULL DEFAULT 1,
    rows        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_target ON snapshots(target, fetched_at DESC);

CREATE TABLE IF NOT EXISTS diffs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target      TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_diffs_target ON diffs(target, computed_at DESC);

CREATE TABLE IF NOT EXISTS heal_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target      TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_heal_target ON heal_events(target, detected_at DESC);
"""


@dataclass(frozen=True)
class CollectionRecord:
    """One stored collection, without its payload.

    The dashboard draws a bar per collection to show a target's record over
    time, which needs the verdict and the size but never the rows themselves.
    """

    target: str
    fetched_at: datetime
    healthy: bool
    row_count: int


class Store:
    """Append-only history of everything the pipeline has observed."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def save_snapshot(self, snapshot: Snapshot, healthy: bool = True) -> None:
        """Record a run. Unhealthy snapshots are kept but never diffed against."""
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO snapshots (target, collector_id, fetched_at, healthy, rows)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    snapshot.target,
                    snapshot.collector_id,
                    snapshot.fetched_at.isoformat(),
                    int(healthy),
                    json.dumps(snapshot.rows, default=str),
                ),
            )
            conn.commit()

    def latest_snapshot(self, target: str, healthy_only: bool = True) -> Snapshot | None:
        """Most recent snapshot for a target, used as the diff baseline."""
        query = "SELECT * FROM snapshots WHERE target = ?"
        if healthy_only:
            query += " AND healthy = 1"
        query += " ORDER BY fetched_at DESC, id DESC LIMIT 1"
        with closing(self._connect()) as conn:
            row = conn.execute(query, (target,)).fetchone()
        return _to_snapshot(row) if row else None

    def save_diff(self, diff: Diff) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO diffs (target, computed_at, payload) VALUES (?, ?, ?)",
                (diff.target, diff.computed_at.isoformat(), diff.model_dump_json()),
            )
            conn.commit()

    def recent_diffs(self, limit: int = 50, target: str | None = None) -> list[Diff]:
        query = "SELECT payload FROM diffs"
        params: list[Any] = []
        if target:
            query += " WHERE target = ?"
            params.append(target)
        query += " ORDER BY computed_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with closing(self._connect()) as conn:
            rows = conn.execute(query, params).fetchall()
        return [Diff.model_validate_json(r["payload"]) for r in rows]

    def save_heal_event(self, event: HealEvent) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO heal_events (target, detected_at, payload) VALUES (?, ?, ?)",
                (event.target, event.detected_at.isoformat(), event.model_dump_json()),
            )
            conn.commit()

    def recent_heal_events(self, limit: int = 50) -> list[HealEvent]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT payload FROM heal_events ORDER BY detected_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [HealEvent.model_validate_json(r["payload"]) for r in rows]

    def collections(self, target: str, limit: int = 24) -> list[CollectionRecord]:
        """The target's most recent collections, oldest first.

        Ordered for reading left to right on a timeline, which is the reverse
        of how every other query here returns rows.
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT target, fetched_at, healthy, json_array_length(rows) AS n"
                " FROM snapshots WHERE target = ?"
                " ORDER BY fetched_at DESC, id DESC LIMIT ?",
                (target, limit),
            ).fetchall()
        return [
            CollectionRecord(
                target=row["target"],
                fetched_at=datetime.fromisoformat(row["fetched_at"]),
                healthy=bool(row["healthy"]),
                row_count=int(row["n"]),
            )
            for row in reversed(rows)
        ]

    def snapshot_count(self, target: str) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM snapshots WHERE target = ?", (target,)
            ).fetchone()
        return int(row["n"])


def _to_snapshot(row: sqlite3.Row) -> Snapshot:
    return Snapshot(
        target=row["target"],
        collector_id=row["collector_id"],
        fetched_at=datetime.fromisoformat(row["fetched_at"]),
        rows=json.loads(row["rows"]),
    )
