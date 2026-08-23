"""Snapshot diffing."""

from __future__ import annotations

from conftest import rows
from scrapverse.config import Target
from scrapverse.diff import compute
from scrapverse.models import Snapshot


def snap(payload: list[dict[str, object]], target: Target) -> Snapshot:
    return Snapshot(target=target.name, collector_id="c_demo123", rows=payload)


def test_first_collection_reports_no_changes(target: Target) -> None:
    """Otherwise the first run would dump the site's whole backlog into the digest."""
    assert compute(None, snap(rows(5), target), target).is_empty


def test_new_entry_is_added(target: Target) -> None:
    before = rows(3)
    after = [*before, {"title": "Release 9", "date": "2026-08-17", "url": "https://x.test/9"}]
    diff = compute(snap(before, target), snap(after, target), target)
    assert [e.row["title"] for e in diff.added] == ["Release 9"]
    assert not diff.removed


def test_edited_entry_reports_which_fields_moved(target: Target) -> None:
    before = rows(3)
    after = [dict(r) for r in before]
    after[1]["date"] = "2026-09-09"
    diff = compute(snap(before, target), snap(after, target), target)
    assert len(diff.changed) == 1
    assert diff.changed[0].fields_changed == ["date"]
    assert diff.changed[0].previous is not None


def test_dropped_entry_is_removed(target: Target) -> None:
    before = rows(4)
    diff = compute(snap(before, target), snap(before[:3], target), target)
    assert [e.row["title"] for e in diff.removed] == ["Release 3"]


def test_whitespace_reformatting_is_not_a_change(target: Target) -> None:
    before = rows(2)
    after = [{**r, "title": f"  {r['title']}  "} for r in before]
    assert compute(snap(before, target), snap(after, target), target).is_empty


def test_identity_falls_back_to_row_hash_when_key_fields_are_gone(target: Target) -> None:
    """A drifted schema should degrade into churn, not crash the diff."""
    before = [{"headline": "a"}, {"headline": "b"}]
    after = [{"headline": "a"}, {"headline": "c"}]
    diff = compute(snap(before, target), snap(after, target), target)
    assert len(diff.added) == 1
    assert len(diff.removed) == 1
