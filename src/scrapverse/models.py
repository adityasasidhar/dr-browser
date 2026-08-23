"""Domain models for snapshots, diffs, and heal events.

The pipeline is a straight line: a `Snapshot` is what a collector returned, a
`HealthReport` says whether that snapshot is trustworthy, a `Diff` says what
changed since the previous trustworthy snapshot, and a `HealEvent` records an
attempt to repair a collector that stopped producing trustworthy snapshots.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    """Timezone-aware now, so stored timestamps are never ambiguous."""
    return datetime.now(UTC)


class Health(StrEnum):
    """Verdict on whether a snapshot can be trusted downstream."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BROKEN = "broken"


class HealStatus(StrEnum):
    """Lifecycle of a single heal attempt."""

    PROPOSED = "proposed"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    VERIFIED = "verified"
    FAILED = "failed"


class Snapshot(BaseModel):
    """One collector run: the rows it returned plus provenance."""

    target: str
    collector_id: str
    fetched_at: datetime = Field(default_factory=utcnow)
    rows: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def fill_rate(self, field: str) -> float:
        """Fraction of rows where `field` holds a meaningful value.

        Empty strings, empty collections, and nulls all count as missing: a
        selector that drifted usually yields those rather than dropping the key.
        """
        if not self.rows:
            return 0.0
        present = sum(1 for row in self.rows if is_present(row.get(field)))
        return present / len(self.rows)


def is_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | dict | tuple | set):
        return bool(value)
    return True


def normalized(value: Any) -> str:
    """Whitespace-insensitive comparison form of a value.

    The one definition of "same text" used by diffing, distinctness checks, and
    the dashboard's rival measurement, so all three agree on what a change is.
    """
    return "" if value is None else " ".join(str(value).split())


class Violation(BaseModel):
    """A single way a snapshot failed its contract.

    `symptom` is written to be pasted into a heal prompt verbatim, so it names
    the field and describes the observed failure rather than an error code.
    """

    field: str | None
    symptom: str
    fatal: bool = False


class HealthReport(BaseModel):
    """Whether a snapshot satisfies its target's contract."""

    target: str
    checked_at: datetime = Field(default_factory=utcnow)
    row_count: int = 0
    violations: list[Violation] = Field(default_factory=list)

    @property
    def status(self) -> Health:
        if any(v.fatal for v in self.violations):
            return Health.BROKEN
        if self.violations:
            return Health.DEGRADED
        return Health.HEALTHY

    @property
    def needs_heal(self) -> bool:
        """Degraded counts too: a half-empty field is a drifted selector."""
        return self.status is not Health.HEALTHY

    def heal_prompt(self) -> str:
        """Render violations into a heal prompt for `bdata scraper heal`.

        Capped at the CLI's documented 1000-character limit.
        """
        if not self.violations:
            return ""
        lines = [f"- {v.symptom}" for v in self.violations]
        body = "\n".join(lines)
        prompt = (
            f"The scraper still returns rows but the extracted data is wrong:\n{body}\n"
            "The page structure has probably changed, or these selectors never "
            "matched correctly. Re-locate these fields and return them under the "
            "same field names as the current output schema."
        )
        return prompt[:1000]


class DiffEntry(BaseModel):
    """One added, removed, or changed row between two snapshots."""

    key: str
    kind: str
    fields_changed: list[str] = Field(default_factory=list)
    row: dict[str, Any] = Field(default_factory=dict)
    previous: dict[str, Any] | None = None


class Diff(BaseModel):
    """What changed on a target between two consecutive healthy snapshots."""

    target: str
    computed_at: datetime = Field(default_factory=utcnow)
    entries: list[DiffEntry] = Field(default_factory=list)

    @property
    def added(self) -> list[DiffEntry]:
        return [e for e in self.entries if e.kind == "added"]

    @property
    def changed(self) -> list[DiffEntry]:
        return [e for e in self.entries if e.kind == "changed"]

    @property
    def removed(self) -> list[DiffEntry]:
        return [e for e in self.entries if e.kind == "removed"]

    @property
    def is_empty(self) -> bool:
        return not self.entries


class HealEvent(BaseModel):
    """An attempt to repair a collector, from detection through verification.

    This is the record the dashboard timeline renders; it is deliberately
    self-contained so a demo can show detection, prompt, and outcome together.
    """

    target: str
    collector_id: str
    detected_at: datetime = Field(default_factory=utcnow)
    prompt: str
    violations: list[Violation] = Field(default_factory=list)
    status: HealStatus = HealStatus.PROPOSED
    resolved_at: datetime | None = None
    notes: str = ""

    @property
    def duration_seconds(self) -> float | None:
        if self.resolved_at is None:
            return None
        return (self.resolved_at - self.detected_at).total_seconds()


def row_key(row: dict[str, Any], key_fields: list[str]) -> str:
    """Stable identity for a row, used to align two snapshots.

    Falls back to hashing the whole row when the configured key fields are
    missing, so a drifted schema degrades into add/remove churn rather than
    crashing the diff.
    """
    values = [str(row.get(f, "")).strip() for f in key_fields]
    if any(values):
        return "|".join(values)
    blob = json.dumps(row, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def dedupe_rows(rows: list[dict[str, Any]], key_fields: list[str]) -> list[dict[str, Any]]:
    """Drop repeated records, keeping first occurrence.

    A discovery-style collector can return the same list once per page it
    visited, so the same entry legitimately arrives several times in one run.
    Without this the row count is inflated and the diff churns on duplicates.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = row_key(row, key_fields)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out
