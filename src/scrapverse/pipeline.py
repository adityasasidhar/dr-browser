"""End-to-end collection for one target: run, check, heal, diff, persist.

This is the whole product in one function. Everything else in the package is a
seam that `pull` composes, which keeps the interesting control flow — what
happens when a scraper breaks mid-pipeline — readable in one place.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import diff as diffmod
from . import heal as healmod
from .brightdata import BrightDataClient, BrightDataError
from .config import Target
from .health import check
from .models import Diff, HealEvent, HealthReport, Snapshot, dedupe_rows
from .store import Store


@dataclass
class PullResult:
    """What one collection produced, including any repair it had to do."""

    target: str
    snapshot: Snapshot | None = None
    report: HealthReport | None = None
    diff: Diff | None = None
    heal_event: HealEvent | None = None
    error: str | None = None

    @property
    def healed(self) -> bool:
        """Whether this pull recovered from a break without human help."""
        return self.heal_event is not None and self.heal_event.status == "verified"

    @property
    def ok(self) -> bool:
        return (
            self.error is None
            and self.report is not None
            and not self.report.needs_heal
            and (self.heal_event is None or self.healed)
        )


def pull(
    client: BrightDataClient,
    target: Target,
    store: Store,
    *,
    auto_heal: bool = True,
    policy: healmod.ApprovalPolicy = healmod.ApprovalPolicy.MANUAL,
) -> PullResult:
    """Collect a target once, healing it in place if its output has drifted."""
    if not target.collector_id:
        return PullResult(
            target=target.name,
            error="no collector_id — run `dr-browser provision` first",
        )

    result = PullResult(target=target.name)

    # Capture the baseline before this cycle writes anything, otherwise the
    # snapshot we are about to save becomes its own comparison point.
    previous = store.latest_snapshot(target.name, healthy_only=True)

    try:
        rows = client.run(target.collector_id, target.url, records_path=target.records_path)
    except BrightDataError as exc:
        result.error = str(exc)
        return result

    rows = dedupe_rows(rows, target.key_fields)
    snapshot = Snapshot(target=target.name, collector_id=target.collector_id, rows=rows)
    report = check(snapshot, target)
    result.snapshot, result.report = snapshot, report

    if report.needs_heal and auto_heal:
        event, verified = healmod.attempt(client, target, report, policy=policy)
        result.heal_event = event
        store.save_heal_event(event)
        if verified is not None:
            # The post-heal run is the authoritative snapshot for this cycle.
            snapshot = verified
            report = check(snapshot, target)
            result.snapshot, result.report = snapshot, report

    # A post-heal result is not trusted until the repair itself is verified.
    # This also protects against a transient healthy response followed by a
    # failed second verification collection.
    healthy = not report.needs_heal and (result.heal_event is None or result.healed)
    store.save_snapshot(snapshot, healthy=healthy)

    if healthy:
        # Only diff against a baseline we trusted, so a broken run never
        # manufactures a wall of false "removed" entries in the digest.
        result.diff = diffmod.compute(previous, snapshot, target)
        store.save_diff(result.diff)

    return result
