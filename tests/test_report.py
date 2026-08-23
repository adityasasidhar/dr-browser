"""The dashboard's view models: does the page state what the store holds?

These cover the layer between the store and the template, because that is where
a measurement could quietly disagree with the contract check that produced it —
a gauge reading "passing" on a snapshot the pipeline called broken would make
the whole page untrustworthy.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from conftest import rows
from scrapverse.config import Config, FieldContract, Target
from scrapverse.health import VANISHED_THRESHOLD
from scrapverse.models import (
    Diff,
    DiffEntry,
    HealEvent,
    HealStatus,
    Snapshot,
    Violation,
    utcnow,
)
from scrapverse.report import (
    MIN_DISTINCT_RATE,
    MIN_MATCH_RATE,
    as_duration,
    as_percent,
    as_symptom,
    build_feed,
    build_hero,
    build_stats,
    build_views,
    measure,
    render,
    split_statement,
)
from scrapverse.store import Store


def snap(target: Target, payload: list[dict[str, object]]) -> Snapshot:
    return Snapshot(target=target.name, collector_id=target.collector_id or "c_x", rows=payload)


# --------------------------------------------------------------- the gauge


def test_gauge_reports_fill_against_the_contract_floor(target: Target) -> None:
    gauge = measure(snap(target, rows(4)), target.field("title"))
    assert (gauge.fill, gauge.min_fill) == (1.0, 0.9)
    assert gauge.fill_ok and gauge.ok


def test_gauge_fails_when_fill_drops_below_the_floor(target: Target) -> None:
    payload = rows(4)
    payload[0]["title"] = ""
    payload[1]["title"] = ""
    gauge = measure(snap(target, payload), target.field("title"))
    assert gauge.fill == 0.5
    assert not gauge.fill_ok and not gauge.ok


def test_gauge_measures_pattern_conformance_separately_from_fill(target: Target) -> None:
    """The drift the dashboard exists to show: populated, but wrong."""
    gauge = measure(snap(target, rows(4, date="Read more")), target.field("date"))
    assert gauge.fill == 1.0 and gauge.fill_ok
    assert gauge.match == 0.0
    assert not gauge.match_ok
    assert not gauge.ok


def test_gauge_leaves_match_unmeasured_when_no_pattern_is_declared(target: Target) -> None:
    gauge = measure(snap(target, rows(3)), target.field("title"))
    assert gauge.match is None
    assert gauge.match_ok


def test_gauge_reports_distinctness_from_the_field_it_collapsed_onto() -> None:
    contract = FieldContract(name="title", distinct_from=["description"], min_fill_rate=0.9)
    subject = Target(
        name="dup", url="https://example.test", description="x", fields=[contract], min_rows=1
    )
    payload: list[dict[str, object]] = [
        {"title": "same text", "description": "same text"},
        {"title": "same text", "description": "same text"},
        {"title": "its own", "description": "different"},
    ]
    gauge = measure(snap(subject, payload), contract)
    assert gauge.rival == "description"
    assert gauge.distinct is not None and round(gauge.distinct, 3) == 0.333
    assert not gauge.distinct_ok and not gauge.ok


def test_headroom_reports_the_weakest_clause_not_the_first(target: Target) -> None:
    """Fill is perfect and the pattern is marginal; the hero must find the pattern."""
    payload = rows(4)
    payload[0]["date"] = "no year here"
    payload[1]["date"] = "also none"
    gauge = measure(snap(target, payload), target.field("date"))
    assert gauge.fill == 1.0
    assert gauge.match == 0.5
    assert gauge.headroom == 0.5 - MIN_MATCH_RATE


# ------------------------------------------------------------- target views


def test_views_carry_health_history_and_heals(config: Config, store: Store) -> None:
    target = config.targets[0]
    store.save_snapshot(snap(target, rows(5)))
    store.save_snapshot(snap(target, rows(2)), healthy=False)
    store.save_heal_event(
        HealEvent(
            target=target.name, collector_id="c_demo123", prompt="p", status=HealStatus.FAILED
        )
    )

    view = build_views(config, store)[0]
    assert view.name == "demo"
    assert view.host == "example.test"
    assert [record.healthy for record in view.history] == [True, False]
    assert len(view.heals) == 1
    assert not view.recovered


def test_view_state_matches_the_contract_check_that_gates_the_pipeline(
    config: Config, store: Store
) -> None:
    target = config.targets[0]
    store.save_snapshot(snap(target, rows(1)), healthy=False)
    view = build_views(config, store)[0]
    assert view.state == "broken"
    assert "not trustworthy" in view.verdict


def test_view_without_a_collection_says_so_rather_than_reading_zero(
    config: Config, store: Store
) -> None:
    view = build_views(config, store)[0]
    assert view.state == "waiting"
    assert view.gauges == [] and view.row_count == 0
    assert view.collected_at is None
    assert "first collection" in view.verdict


def test_view_marks_a_target_that_recovered_on_its_own(config: Config, store: Store) -> None:
    target = config.targets[0]
    store.save_snapshot(snap(target, rows(5)))
    store.save_heal_event(
        HealEvent(
            target=target.name,
            collector_id="c_demo123",
            prompt="p",
            status=HealStatus.VERIFIED,
        )
    )
    assert build_views(config, store)[0].recovered


# -------------------------------------------------------------------- stats


def test_stats_average_only_the_repairs_that_were_verified(config: Config, store: Store) -> None:
    detected = utcnow()
    store.save_snapshot(snap(config.targets[0], rows(4)))
    verified = HealEvent(
        target="demo",
        collector_id="c",
        prompt="p",
        status=HealStatus.VERIFIED,
        detected_at=detected,
        resolved_at=detected + timedelta(seconds=120),
    )
    failed = HealEvent(
        target="demo",
        collector_id="c",
        prompt="p",
        status=HealStatus.FAILED,
        detected_at=detected,
        resolved_at=detected + timedelta(seconds=9000),
    )
    stats = build_stats(build_views(config, store), [], [verified, failed])
    assert (stats.heals, stats.heals_verified, stats.heal_rate) == (2, 1, 50)
    assert stats.recovery_seconds == 120
    assert stats.entries == 4 and stats.contracts == 3


def test_stats_report_no_recovery_time_before_the_first_repair(
    config: Config, store: Store
) -> None:
    stats = build_stats(build_views(config, store), [], [])
    assert stats.recovery_seconds is None and stats.heal_rate == 0


# --------------------------------------------------------------------- feed


def test_feed_merges_targets_into_one_reverse_chronological_stream() -> None:
    now = utcnow()
    older = Diff(
        target="alpha",
        computed_at=now - timedelta(days=1),
        entries=[DiffEntry(key="a", kind="added", row={"title": "Older", "date": "2026-01-01"})],
    )
    newer = Diff(
        target="beta",
        computed_at=now,
        entries=[
            DiffEntry(
                key="b",
                kind="changed",
                fields_changed=["summary"],
                row={"title": "Newer", "summary": "  body   text ", "url": "https://x.test/1"},
            )
        ],
    )
    feed = build_feed([older, newer])
    assert [item.target for item in feed] == ["beta", "alpha"]
    assert feed[0].kind == "changed" and feed[0].fields_changed == ["summary"]
    assert feed[0].summary == "body text"
    assert feed[0].url == "https://x.test/1"
    assert feed[1].date == "2026-01-01"


def test_feed_is_capped_but_the_page_can_still_say_how_many_there_were() -> None:
    diff = Diff(
        target="alpha",
        entries=[DiffEntry(key=str(i), kind="added", row={"title": f"e{i}"}) for i in range(90)],
    )
    assert len(build_feed([diff])) == 60
    assert len(build_feed([diff], limit=5)) == 5


# --------------------------------------------------------------------- hero


def test_hero_leads_with_the_most_recent_break(config: Config, store: Store) -> None:
    heal = HealEvent(
        target="demo",
        collector_id="c",
        prompt="p",
        violations=[Violation(field="date", symptom="The `date` field drifted. It is wrong now.")],
    )
    hero = build_hero(build_views(config, store), [heal])
    assert hero.heal is heal
    assert hero.lede == "The `date` field drifted."
    assert hero.detail == "It is wrong now."


def test_hero_falls_back_to_the_narrowest_passing_margin(config: Config, store: Store) -> None:
    target = config.targets[0]
    payload = rows(10)
    # `url` lands exactly on its 80% floor: passing, but with no room left.
    payload[0]["url"] = ""
    payload[1]["url"] = ""
    store.save_snapshot(snap(target, payload))

    view = build_views(config, store)[0]
    assert view.state == "healthy"

    hero = build_hero([view], [])
    assert hero.heal is None
    assert hero.lede == "Every declared contract is holding."
    assert "`url`" in hero.detail and "80% of rows against its 80% floor" in hero.detail


def test_hero_invites_a_first_collection_when_nothing_has_run(config: Config, store: Store) -> None:
    hero = build_hero(build_views(config, store), [])
    assert "No collection has run" in hero.lede
    assert "dr-browser pull" in hero.detail


# ------------------------------------------------------------------ filters


def test_split_statement_keeps_a_single_sentence_whole() -> None:
    assert split_statement("Only one sentence here.") == ("Only one sentence here.", "")


def test_symptom_sets_backticked_identifiers_as_code_and_escapes_the_rest() -> None:
    out = str(as_symptom("The `date` field met <script>alert(1)</script>."))
    assert "<code>date</code>" in out
    assert "<script>" not in out and "&lt;script&gt;" in out


def test_percent_and_duration_render_the_absent_case_as_a_dash() -> None:
    assert (as_percent(None), as_duration(None)) == ("—", "—")
    assert as_percent(0.875) == "88%"
    assert (as_duration(45), as_duration(214), as_duration(3900)) == ("45s", "3m 34s", "1h 05m")


# -------------------------------------------------------------------- render


def test_render_writes_a_page_that_names_every_target_and_its_reading(
    config: Config, store: Store, tmp_path: Path
) -> None:
    target = config.targets[0]
    store.save_snapshot(snap(target, rows(6)))
    store.save_diff(
        Diff(target=target.name, entries=[DiffEntry(key="a", kind="added", row={"title": "New"})])
    )

    output = render(config, store, tmp_path / "site" / "index.html")
    html = output.read_text()

    assert output.is_file()
    assert "<body" in html  # the synthetic-data marker is inserted after this
    assert target.name in html and target.collector_id in html
    assert "New" in html
    # the gauge draws the measurement against the declared floor
    assert "--t: 90.0%" in html
    assert f"{MIN_DISTINCT_RATE * 100:.1f}%" not in html  # no distinct_from declared here


def test_render_labels_a_vanished_field_vanished(
    config: Config, store: Store, tmp_path: Path
) -> None:
    target = config.targets[0]
    vanished_rows = [
        {"title": "", "date": "2026-01-01", "url": "https://x.test/1"}
        for _ in range(int(1 / VANISHED_THRESHOLD) + 1)
    ]
    store.save_snapshot(snap(target, vanished_rows))

    html = render(config, store, tmp_path / "site" / "index.html").read_text()
    assert 'data-vanished="true"' in html
    assert ">vanished<" in html
