"""Row identity and deduplication."""

from __future__ import annotations

from scrapverse.models import dedupe_rows, row_key

ROWS = [
    {"entry_title": "August 10-17, 2026", "body": "a"},
    {"entry_title": "July 13-17, 2026", "body": "b"},
    {"entry_title": "August 10-17, 2026", "body": "a"},
]


def test_dedupe_keeps_first_occurrence_in_order() -> None:
    """A discovery collector returns the same list once per page it visited."""
    out = dedupe_rows(ROWS, ["entry_title"])
    assert [r["entry_title"] for r in out] == ["August 10-17, 2026", "July 13-17, 2026"]


def test_dedupe_without_key_fields_falls_back_to_whole_row_identity() -> None:
    assert len(dedupe_rows(ROWS, [])) == 2


def test_rows_differing_outside_the_key_are_still_one_entry() -> None:
    rows = [{"entry_title": "x", "body": "1"}, {"entry_title": "x", "body": "2"}]
    assert len(dedupe_rows(rows, ["entry_title"])) == 1


def test_row_key_is_stable_across_key_ordering() -> None:
    assert row_key({"a": "1", "b": "2"}, ["a", "b"]) == row_key({"b": "2", "a": "1"}, ["a", "b"])


def test_row_key_hashes_when_key_fields_are_absent() -> None:
    key = row_key({"unexpected": "shape"}, ["title"])
    assert len(key) == 16 and key.isalnum()
