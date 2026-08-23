"""Contract checking: does a snapshot get flagged when the site drifts?"""

from __future__ import annotations

from conftest import rows
from scrapverse.config import Target
from scrapverse.health import check
from scrapverse.models import Health, Snapshot


def snap(payload: list[dict[str, object]], target: Target) -> Snapshot:
    return Snapshot(target=target.name, collector_id=target.collector_id or "c_x", rows=payload)


def test_well_formed_snapshot_is_healthy(target: Target) -> None:
    report = check(snap(rows(6), target), target)
    assert report.status is Health.HEALTHY
    assert not report.needs_heal
    assert report.heal_prompt() == ""


def test_zero_rows_is_fatal(target: Target) -> None:
    report = check(snap([], target), target)
    assert report.status is Health.BROKEN
    assert "zero rows" in report.violations[0].symptom


def test_row_count_collapse_is_fatal(target: Target) -> None:
    report = check(snap(rows(1), target), target)
    assert report.status is Health.BROKEN
    assert "at least 3" in report.violations[0].symptom


def test_vanished_required_field_is_fatal(target: Target) -> None:
    report = check(snap(rows(5, title=""), target), target)
    assert report.status is Health.BROKEN
    assert any(v.field == "title" and v.fatal for v in report.violations)


def test_partial_fill_is_degraded_not_fatal(target: Target) -> None:
    payload = rows(10)
    for row in payload[:5]:
        row["date"] = ""
    report = check(snap(payload, target), target)
    assert report.status is Health.DEGRADED
    assert report.needs_heal
    assert any("50%" in v.symptom for v in report.violations)


def test_pattern_drift_is_caught_even_when_field_is_populated(target: Target) -> None:
    """The failure null-checks miss: selector resolves, but to the wrong element."""
    report = check(snap(rows(6, date="Read more"), target), target)
    assert report.needs_heal
    drift = next(v for v in report.violations if v.field == "date")
    assert not drift.fatal
    assert "'Read more'" in drift.symptom


def test_a_few_odd_values_do_not_trip_the_pattern_check(target: Target) -> None:
    payload = rows(10)
    payload[0]["date"] = "unreleased"
    assert check(snap(payload, target), target).status is Health.HEALTHY


def test_heal_prompt_names_every_violation_and_respects_the_cli_cap(target: Target) -> None:
    report = check(snap(rows(4, title="", date="soon"), target), target)
    prompt = report.heal_prompt()
    assert "`title`" in prompt
    assert "`date`" in prompt
    assert len(prompt) <= 1000


def test_optional_field_at_zero_fill_reads_as_vanished_not_partial(target: Target) -> None:
    """`min_fill_rate` wording must not claim 'populated on only 0% of rows'."""
    optional = target.field("url")
    assert optional is not None and not optional.required

    report = check(snap(rows(5, url=""), target), target)
    violation = next(v for v in report.violations if v.field == "url")
    assert "empty on every row" in violation.symptom
    assert not violation.fatal  # optional: degraded, not broken
    assert report.status is Health.DEGRADED


def test_duplicate_fields_are_caught_though_both_are_populated(target: Target) -> None:
    """The failure both fill-rate and pattern checks miss: one value, two fields."""
    contract = target.field("title")
    assert contract is not None
    contract.distinct_from = ["summary"]

    payload = [{**row, "summary": row["title"]} for row in rows(8)]
    report = check(snap(payload, target), target)

    violation = next(v for v in report.violations if "identical text" in v.symptom)
    assert "100%" in violation.symptom
    assert not violation.fatal


def test_genuinely_different_fields_do_not_trip_the_duplicate_check(target: Target) -> None:
    contract = target.field("title")
    assert contract is not None
    contract.distinct_from = ["summary"]

    payload = [{**row, "summary": f"body for {row['title']}"} for row in rows(8)]
    assert check(snap(payload, target), target).status is Health.HEALTHY


def test_duplicate_check_ignores_whitespace_differences(target: Target) -> None:
    contract = target.field("title")
    assert contract is not None
    contract.distinct_from = ["summary"]

    payload = [{**row, "summary": f"  {row['title']}  "} for row in rows(8)]
    assert check(snap(payload, target), target).needs_heal


def test_duplicate_check_is_skipped_when_the_other_field_is_absent(target: Target) -> None:
    contract = target.field("title")
    assert contract is not None
    contract.distinct_from = ["nonexistent"]
    assert check(snap(rows(5), target), target).status is Health.HEALTHY
