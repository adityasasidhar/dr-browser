"""Contract checking: decide whether a snapshot is trustworthy.

This module is the trigger for self-healing. A scraper rarely fails loudly when
a site changes — it keeps returning rows, but a field silently goes null or
starts holding the wrong text. Checking each snapshot against its contract turns
that silent drift into a specific, describable symptom that `bdata scraper heal`
can act on.
"""

from __future__ import annotations

from .config import FieldContract, Target
from .models import HealthReport, Snapshot, Violation, is_present

# These three thresholds are the published semantics of a contract: the
# dashboard renders them as the tick marks a measurement has to clear, so they
# are part of the module's interface rather than internal tuning.

# A field present on fewer than this fraction of rows is treated as structurally
# gone rather than merely patchy, which makes it a fatal violation.
VANISHED_THRESHOLD = 0.05

# Pattern mismatches are noisier than nulls, so they only count as drift once
# most values fail — a handful of odd entries is normal on a real page.
PATTERN_TOLERANCE = 0.5

# Two fields holding the same text on more than this share of rows have
# collapsed onto one selector rather than coincidentally matching.
DUPLICATE_TOLERANCE = 0.25


def check(snapshot: Snapshot, target: Target) -> HealthReport:
    """Evaluate a snapshot against its target's contract."""
    report = HealthReport(target=target.name, row_count=snapshot.row_count)

    if snapshot.row_count == 0:
        report.violations.append(
            Violation(
                field=None,
                symptom=(
                    "The scraper returned zero rows. It previously returned a list of "
                    "entries from this page, so the container or item selector no "
                    "longer matches anything."
                ),
                fatal=True,
            )
        )
        return report

    if snapshot.row_count < target.min_rows:
        report.violations.append(
            Violation(
                field=None,
                symptom=(
                    f"Only {snapshot.row_count} rows came back but at least "
                    f"{target.min_rows} are expected. The item selector is matching a "
                    "narrower set of elements than it used to."
                ),
                fatal=True,
            )
        )

    for contract in target.fields:
        report.violations.extend(_check_field(snapshot, contract))

    return report


def _check_field(snapshot: Snapshot, contract: FieldContract) -> list[Violation]:
    """Check one field's fill rate and value shape across every row."""
    violations: list[Violation] = []
    fill = snapshot.fill_rate(contract.name)

    if fill <= VANISHED_THRESHOLD:
        # Gating this on `required` would send an optional field down the
        # partial-fill branch, which reads as "populated on only 0% of rows".
        violations.append(
            Violation(
                field=contract.name,
                symptom=(
                    f"The `{contract.name}` field is empty on every row"
                    f"{_hint(contract)}. Its selector does not resolve."
                ),
                fatal=contract.required,
            )
        )
        return violations

    if fill < contract.min_fill_rate:
        violations.append(
            Violation(
                field=contract.name,
                symptom=(
                    f"The `{contract.name}` field is populated on only "
                    f"{fill:.0%} of rows, below the expected "
                    f"{contract.min_fill_rate:.0%}{_hint(contract)}. Its selector "
                    "matches some layouts on the page but not others."
                ),
                fatal=False,
            )
        )

    if contract.pattern is not None:
        violations.extend(_check_pattern(snapshot, contract))

    for other in contract.distinct_from:
        violations.extend(_check_distinct(snapshot, contract, other))

    return violations


def _check_distinct(snapshot: Snapshot, contract: FieldContract, other: str) -> list[Violation]:
    """Flag two fields that have collapsed into the same value.

    Neither field is null, so fill-rate and pattern checks both pass while the
    scraper is really capturing one value twice — a headline selector that
    swallowed the body, or a body selector that never moved off the heading.
    """
    pairs = [
        (row.get(contract.name), row.get(other))
        for row in snapshot.rows
        if is_present(row.get(contract.name)) and is_present(row.get(other))
    ]
    if not pairs:
        return []

    identical = [1 for a, b in pairs if _norm(a) == _norm(b)]
    rate = len(identical) / len(pairs)
    if rate <= DUPLICATE_TOLERANCE:
        return []

    return [
        Violation(
            field=contract.name,
            symptom=(
                f"The `{contract.name}` and `{other}` fields hold identical text on "
                f"{rate:.0%} of rows. They are resolving to the same element, so one "
                f"of them is not capturing its own value. `{contract.name}` should be "
                f"{contract.description or 'its own distinct value'}."
            ),
            fatal=False,
        )
    ]


def _norm(value: object) -> str:
    """Whitespace-insensitive comparison form."""
    return " ".join(str(value).split())


def _check_pattern(snapshot: Snapshot, contract: FieldContract) -> list[Violation]:
    """Flag a field whose values stopped looking like themselves.

    This catches the failure that null-checking misses: the selector still
    resolves, but to the wrong element, so the field fills with plausible-looking
    text that is not the data you asked for.
    """
    populated = [row.get(contract.name) for row in snapshot.rows]
    populated = [v for v in populated if is_present(v)]
    if not populated:
        return []

    mismatched = [v for v in populated if not contract.matches(v)]
    rate = len(mismatched) / len(populated)
    if rate <= PATTERN_TOLERANCE:
        return []

    sample = ", ".join(repr(str(v)[:40]) for v in mismatched[:3])
    return [
        Violation(
            field=contract.name,
            symptom=(
                f"The `{contract.name}` field no longer looks like "
                f"{contract.description or 'the expected value'} — {rate:.0%} of values "
                f"fail the expected pattern `{contract.pattern}`. Examples: {sample}. "
                "The selector is probably picking up a neighbouring element."
            ),
            fatal=False,
        )
    ]


def _hint(contract: FieldContract) -> str:
    """Weave the field's human description into a symptom sentence."""
    return f" (expected: {contract.description})" if contract.description else ""
