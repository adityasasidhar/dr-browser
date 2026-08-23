"""The self-healing loop.

When a snapshot fails its contract, the violations are already phrased as
symptoms, so they compose directly into a heal prompt. This module runs the
full repair cycle — describe, heal, approve, re-run, re-check — and records what
happened either way, because a heal that did not work is as important to show as
one that did.
"""

from __future__ import annotations

from enum import StrEnum

from .brightdata import BrightDataClient, BrightDataError
from .config import Target
from .health import check
from .models import HealEvent, HealStatus, HealthReport, Snapshot, dedupe_rows, utcnow

# A template can take a moment to propagate after Bright Data accepts an
# approval.  Two independently healthy runs make the repair evidence much
# stronger than accepting the first non-empty response, while a third attempt
# gives one propagation-lagged collection room to settle.
MAX_APPROVAL_ATTEMPTS = 3
MAX_VERIFICATION_RUNS = 3
REQUIRED_HEALTHY_VERIFICATIONS = 2


class ApprovalPolicy(StrEnum):
    """Who decides whether a proposed fix is accepted."""

    MANUAL = "manual"
    AUTO = "auto"


def attempt(
    client: BrightDataClient,
    target: Target,
    report: HealthReport,
    policy: ApprovalPolicy = ApprovalPolicy.MANUAL,
) -> tuple[HealEvent, Snapshot | None]:
    """Run one repair cycle for a broken target.

    Returns the event describing what happened, plus the verification snapshot
    if the heal was approved and re-run. The caller persists both.
    """
    if not target.collector_id:
        raise ValueError(f"target {target.name!r} has no collector_id to heal")

    prompt = report.heal_prompt()
    event = HealEvent(
        target=target.name,
        collector_id=target.collector_id,
        prompt=prompt,
        violations=report.violations,
    )

    try:
        outcome = client.heal(target.collector_id, prompt, url=target.url)
    except BrightDataError as exc:
        return _fail(event, f"heal call failed: {exc}"), None

    event.status = (
        HealStatus.AWAITING_APPROVAL if outcome.awaiting_approval else HealStatus.PROPOSED
    )
    event.notes = outcome.next_step

    if policy is ApprovalPolicy.MANUAL:
        # Stop at the gate. `dr-browser approve` resumes from here.
        return event, None

    if not outcome.awaiting_approval and not outcome.succeeded:
        return _fail(event, f"heal returned status {outcome.status!r}"), None

    approval_gates = 0
    while outcome.awaiting_approval and approval_gates < MAX_APPROVAL_ATTEMPTS:
        approval_gates += 1
        try:
            outcome = client.approve(target.collector_id, url=target.url)
        except BrightDataError as exc:
            return _fail(event, f"approve call failed: {exc}"), None
        if not outcome.awaiting_approval and not outcome.succeeded:
            return _fail(event, f"approve returned status {outcome.status!r}"), None

    if outcome.awaiting_approval:
        return _fail(
            event,
            f"still awaiting approval after {MAX_APPROVAL_ATTEMPTS} approval attempts",
        ), None

    event.status = HealStatus.APPROVED
    return verify(client, target, event, approval_gates=approval_gates)


def verify(
    client: BrightDataClient, target: Target, event: HealEvent, *, approval_gates: int = 0
) -> tuple[HealEvent, Snapshot | None]:
    """Re-run the healed collector and confirm it now satisfies its contract.

    A heal is only 'verified' when fresh output passes the same check that
    flagged it, which is what stops an accepted-but-wrong fix from silently
    poisoning the dataset.
    """
    if not target.collector_id:
        raise ValueError(f"target {target.name!r} has no collector_id to verify")

    healthy_in_a_row = 0
    last_snapshot: Snapshot | None = None
    last_report: HealthReport | None = None
    errors: list[str] = []

    for _ in range(MAX_VERIFICATION_RUNS):
        try:
            rows = client.run(target.collector_id, target.url, records_path=target.records_path)
        except BrightDataError as exc:
            healthy_in_a_row = 0
            errors.append(str(exc))
            continue

        rows = dedupe_rows(rows, target.key_fields)
        snapshot = Snapshot(target=target.name, collector_id=target.collector_id, rows=rows)
        recheck = check(snapshot, target)
        last_snapshot, last_report = snapshot, recheck

        if recheck.needs_heal:
            healthy_in_a_row = 0
            continue

        healthy_in_a_row += 1
        if healthy_in_a_row == REQUIRED_HEALTHY_VERIFICATIONS:
            event.resolved_at = utcnow()
            event.status = HealStatus.VERIFIED
            gate_note = f" after {approval_gates} approval gate(s)" if approval_gates else ""
            event.notes = (
                f"verified{gate_note}: {REQUIRED_HEALTHY_VERIFICATIONS} independent "
                f"post-heal collections satisfy the contract ({snapshot.row_count} rows)"
            )
            return event, snapshot

    event.resolved_at = utcnow()
    if last_report is not None and last_report.needs_heal:
        remaining = "; ".join(v.symptom for v in last_report.violations)
        return _fail(event, f"still failing after heal: {remaining[:400]}"), last_snapshot
    if errors:
        return _fail(event, f"verification runs failed: {' | '.join(errors)[:400]}"), last_snapshot
    return _fail(
        event,
        "verification did not produce the required consecutive healthy collections",
    ), last_snapshot


def _fail(event: HealEvent, note: str) -> HealEvent:
    event.status = HealStatus.FAILED
    event.notes = note
    event.resolved_at = utcnow()
    return event
