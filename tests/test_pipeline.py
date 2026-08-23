"""End-to-end pipeline behaviour, including the break-and-recover path."""

from __future__ import annotations

from conftest import FakeRunner, rows
from scrapverse.brightdata import BrightDataClient
from scrapverse.config import Target
from scrapverse.heal import ApprovalPolicy
from scrapverse.models import HealStatus
from scrapverse.pipeline import pull
from scrapverse.store import Store

GATE = {"collector_id": "c_demo123", "status": "awaiting_approval", "next_step": "verify"}
DONE = {"collector_id": "c_demo123", "status": "done"}


def test_healthy_first_pull_stores_a_snapshot_and_no_changes(
    client: BrightDataClient, runner: FakeRunner, target: Target, store: Store
) -> None:
    runner.queue("run", {"data": rows(5)})
    result = pull(client, target, store, auto_heal=True)

    assert result.ok
    assert result.diff is not None and result.diff.is_empty
    assert store.snapshot_count(target.name) == 1


def test_second_pull_reports_the_new_entry(
    client: BrightDataClient, runner: FakeRunner, target: Target, store: Store
) -> None:
    runner.queue("run", {"data": rows(4)}, {"data": rows(5)})
    pull(client, target, store)
    result = pull(client, target, store)

    assert result.diff is not None
    assert [e.row["title"] for e in result.diff.added] == ["Release 4"]


def test_break_is_detected_healed_and_verified_in_one_pull(
    client: BrightDataClient, runner: FakeRunner, target: Target, store: Store
) -> None:
    """The hero path: drifted output, heal, approve, re-run, contract satisfied."""
    runner.queue("run", {"data": rows(5, title="")}, {"data": rows(5)}, {"data": rows(5)})
    runner.queue("heal", GATE)
    runner.queue("approve", DONE)

    result = pull(client, target, store, policy=ApprovalPolicy.AUTO)

    assert result.healed
    assert result.ok
    event = result.heal_event
    assert event is not None
    assert event.status is HealStatus.VERIFIED
    assert "`title`" in event.prompt
    assert event.duration_seconds is not None
    assert store.recent_heal_events()[0].status is HealStatus.VERIFIED


def test_heal_requires_two_healthy_post_heal_collections(
    client: BrightDataClient, runner: FakeRunner, target: Target, store: Store
) -> None:
    """One healthy response after a repair cannot make a flaky collector trusted."""
    runner.queue(
        "run",
        {"data": rows(5, title="")},
        {"data": rows(5)},
        {"data": rows(5, title="")},
        {"data": rows(5, title="")},
    )
    runner.queue("heal", GATE)
    runner.queue("approve", DONE)

    result = pull(client, target, store, policy=ApprovalPolicy.AUTO)

    assert not result.ok
    assert result.heal_event is not None
    assert result.heal_event.status is HealStatus.FAILED
    assert store.latest_snapshot(target.name, healthy_only=True) is None


def test_auto_policy_handles_multiple_approval_gates(
    client: BrightDataClient, runner: FakeRunner, target: Target, store: Store
) -> None:
    runner.queue("run", {"data": rows(5, title="")}, {"data": rows(5)}, {"data": rows(5)})
    runner.queue("heal", GATE)
    runner.queue("approve", GATE, DONE)

    result = pull(client, target, store, policy=ApprovalPolicy.AUTO)

    assert result.healed
    assert len(runner.calls_for("approve")) == 2


def test_manual_policy_stops_at_the_approval_gate(
    client: BrightDataClient, runner: FakeRunner, target: Target, store: Store
) -> None:
    runner.queue("run", {"data": rows(5, title="")})
    runner.queue("heal", GATE)

    result = pull(client, target, store, policy=ApprovalPolicy.MANUAL)

    assert result.heal_event is not None
    assert result.heal_event.status is HealStatus.AWAITING_APPROVAL
    assert not runner.calls_for("approve")
    assert len(runner.calls_for("run")) == 1  # no verification run without approval


def test_a_heal_that_does_not_fix_anything_is_recorded_as_failed(
    client: BrightDataClient, runner: FakeRunner, target: Target, store: Store
) -> None:
    """An accepted-but-wrong fix must not be allowed to poison the dataset."""
    runner.queue(
        "run",
        {"data": rows(5, title="")},
        {"data": rows(5, title="")},
        {"data": rows(5, title="")},
        {"data": rows(5, title="")},
    )
    runner.queue("heal", GATE)
    runner.queue("approve", DONE)

    result = pull(client, target, store, policy=ApprovalPolicy.AUTO)

    assert not result.ok
    assert result.heal_event is not None
    assert result.heal_event.status is HealStatus.FAILED
    assert "still failing" in result.heal_event.notes
    assert result.diff is None


def test_a_broken_collection_is_never_diffed(
    client: BrightDataClient, runner: FakeRunner, target: Target, store: Store
) -> None:
    """Otherwise a scraper failure looks like the site deleting every entry."""
    runner.queue("run", {"data": rows(5)}, {"data": []})
    pull(client, target, store, auto_heal=False)
    result = pull(client, target, store, auto_heal=False)

    assert result.diff is None
    assert store.recent_diffs() and all(d.is_empty for d in store.recent_diffs())
    # The healthy baseline survives, so the next good run diffs against it.
    baseline = store.latest_snapshot(target.name, healthy_only=True)
    assert baseline is not None and baseline.row_count == 5


def test_no_heal_flag_detects_without_repairing(
    client: BrightDataClient, runner: FakeRunner, target: Target, store: Store
) -> None:
    runner.queue("run", {"data": []})
    result = pull(client, target, store, auto_heal=False)

    assert result.heal_event is None
    assert not runner.calls_for("heal")
    assert result.report is not None and result.report.needs_heal


def test_unprovisioned_target_fails_with_a_pointer_to_the_fix(
    client: BrightDataClient, target: Target, store: Store
) -> None:
    target.collector_id = None
    result = pull(client, target, store)
    assert result.error is not None and "provision" in result.error


def test_cli_failure_is_captured_rather_than_raised(
    client: BrightDataClient, runner: FakeRunner, target: Target, store: Store
) -> None:
    from scrapverse.brightdata import CommandResult

    runner.queue("run", CommandResult(1, "", "rate limited"))
    result = pull(client, target, store)

    assert not result.ok
    assert result.error is not None and "rate limited" in result.error
