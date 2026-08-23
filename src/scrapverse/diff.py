"""Snapshot diffing: what actually changed on a target since last time.

Collecting the same page every day is only useful if you can see the delta, so
this reduces two snapshots to added, removed, and changed rows keyed by a
stable identity.
"""

from __future__ import annotations

from typing import Any

from .config import Target
from .models import Diff, DiffEntry, Snapshot, row_key


def compute(previous: Snapshot | None, current: Snapshot, target: Target) -> Diff:
    """Diff two snapshots of the same target.

    A missing `previous` means this is the first collection, which is reported
    as an empty diff rather than as every row being new — otherwise the first
    run would spam a week's worth of releases into the digest.
    """
    diff = Diff(target=target.name)
    if previous is None:
        return diff

    keys = target.key_fields or _infer_keys(current)
    old = {row_key(row, keys): row for row in previous.rows}
    new = {row_key(row, keys): row for row in current.rows}

    for key, row in new.items():
        if key not in old:
            diff.entries.append(DiffEntry(key=key, kind="added", row=row))
            continue
        changed = _changed_fields(old[key], row)
        if changed:
            diff.entries.append(
                DiffEntry(
                    key=key,
                    kind="changed",
                    fields_changed=changed,
                    row=row,
                    previous=old[key],
                )
            )

    for key, row in old.items():
        if key not in new:
            diff.entries.append(DiffEntry(key=key, kind="removed", row=row))

    return diff


def _changed_fields(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Fields whose values differ, ignoring whitespace-only churn."""
    changed: list[str] = []
    for field_name in sorted(set(old) | set(new)):
        if _norm(old.get(field_name)) != _norm(new.get(field_name)):
            changed.append(field_name)
    return changed


def _norm(value: Any) -> str:
    """Normalize a value for comparison so reformatting is not a change."""
    if value is None:
        return ""
    return " ".join(str(value).split())


def _infer_keys(snapshot: Snapshot) -> list[str]:
    """Guess an identity field when the target did not configure one."""
    if not snapshot.rows:
        return []
    for candidate in ("url", "link", "id", "version", "tag", "title", "name"):
        if candidate in snapshot.rows[0]:
            return [candidate]
    return []
