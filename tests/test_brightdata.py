"""The CLI wrapper: argv construction, envelope parsing, error surfacing."""

from __future__ import annotations

import pytest

from conftest import FakeRunner, rows
from scrapverse.brightdata import (
    BrightDataClient,
    BrightDataError,
    CommandResult,
    extract_rows,
)


def test_create_returns_the_collector_id(client: BrightDataClient, runner: FakeRunner) -> None:
    runner.queue("create", {"collector_id": "c_new1", "name": "demo", "status": "done"})
    assert client.create("https://x.test", "extract things", name="demo") == "c_new1"
    argv = runner.calls_for("create")[0]
    assert "--json" in argv and "--name" in argv


def test_create_without_a_collector_id_is_an_error(
    client: BrightDataClient, runner: FakeRunner
) -> None:
    runner.queue("create", {"status": "failed"})
    with pytest.raises(BrightDataError, match="no collector_id"):
        client.create("https://x.test", "extract things", name="demo")


def test_run_extracts_rows_and_passes_sync_flag(
    client: BrightDataClient, runner: FakeRunner
) -> None:
    runner.queue("run", {"data": rows(3)})
    assert len(client.run("c_1", "https://x.test", sync=True)) == 3
    assert "--sync" in runner.calls_for("run")[0]


def test_run_uses_a_batch_safe_poll_timeout(client: BrightDataClient, runner: FakeRunner) -> None:
    runner.queue("run", {"data": rows(3)})
    client.run("c_1", "https://x.test")
    argv = runner.calls_for("run")[0]
    timeout_index = argv.index("--timeout")
    assert int(argv[timeout_index + 1]) >= 3600


def test_heal_stops_at_the_approval_gate_by_default(
    client: BrightDataClient, runner: FakeRunner
) -> None:
    """Collapsing the gate would hide the step the whole project is about."""
    runner.queue("heal", {"collector_id": "c_1", "status": "awaiting_approval", "next_step": "run"})
    outcome = client.heal("c_1", "the title field is empty", url="https://x.test")
    assert outcome.awaiting_approval
    argv = runner.calls_for("heal")[0]
    assert "--auto-approve" not in argv
    assert "--url" in argv


def test_heal_prompt_is_truncated_to_the_cli_limit(
    client: BrightDataClient, runner: FakeRunner
) -> None:
    runner.queue("heal", {"collector_id": "c_1", "status": "awaiting_approval"})
    client.heal("c_1", "x" * 2000)
    assert len(runner.calls_for("heal")[0][4]) == 1000


def test_approve_saves_the_template_but_reject_does_not(
    client: BrightDataClient, runner: FakeRunner
) -> None:
    runner.queue("approve", {"status": "done"}, {"status": "done"})
    client.approve("c_1")
    assert "--auto-save" in runner.calls_for("approve")[0]
    client.approve("c_1", reject=True)
    reject_argv = runner.calls_for("approve")[1]
    assert "--reject" in reject_argv and "--auto-save" not in reject_argv


def test_api_key_is_appended_for_non_interactive_auth(runner: FakeRunner) -> None:
    keyed = BrightDataClient(runner=runner, base_command=["bdata"], api_key="secret")
    runner.queue("run", {"data": []})
    keyed.run("c_1", "https://x.test")
    argv = runner.calls_for("run")[0]
    assert argv[-2:] == ["--api-key", "secret"]


def test_failed_invocation_surfaces_stderr(client: BrightDataClient, runner: FakeRunner) -> None:
    runner.queue("run", CommandResult(1, "", "401 unauthorized"))
    with pytest.raises(BrightDataError, match="401 unauthorized"):
        client.run("c_1", "https://x.test")


def test_progress_chatter_before_the_json_is_tolerated(
    client: BrightDataClient, runner: FakeRunner
) -> None:
    noisy = CommandResult(0, 'polling job...\ndone\n{"data": [{"title": "a"}]}', "")
    runner.queue("run", noisy)
    assert client.run("c_1", "https://x.test") == [{"title": "a"}]


def test_unparseable_output_is_an_error(client: BrightDataClient, runner: FakeRunner) -> None:
    runner.queue("run", CommandResult(0, "not json at all", ""))
    with pytest.raises(BrightDataError, match="could not parse"):
        client.run("c_1", "https://x.test")


@pytest.mark.parametrize(
    "payload",
    [
        [{"title": "a"}],
        {"data": [{"title": "a"}]},
        {"rows": [{"title": "a"}]},
        {"result": {"data": [{"title": "a"}]}},
    ],
    ids=["bare-array", "data", "rows", "nested"],
)
def test_row_extraction_accepts_every_known_envelope(payload: object) -> None:
    assert extract_rows(payload) == [{"title": "a"}]


def test_row_extraction_of_an_unknown_shape_is_empty_not_an_exception() -> None:
    assert extract_rows({"unexpected": "shape"}) == []


# --- nested record envelopes -------------------------------------------------
# A generated scraper returns one wrapper per page it visited, with the real
# records nested under a key it chose itself. These shapes are taken from live
# `bdata scraper run` output.

WRAPPED = [
    {
        "changelog_entries": [
            {"entry_title": "August 10-17, 2026", "publication_date": "August 10-17, 2026"},
            {"entry_title": "July 13-17, 2026", "publication_date": "July 13-17, 2026"},
        ],
        "product_page_url": "https://docs.example.test/changelog#a",
        "input": {"url": "https://changelog.example.test/"},
    },
    {
        "changelog_entries": [
            {"entry_title": "June 8-12, 2026", "publication_date": "June 8-12, 2026"},
        ],
        "product_page_url": "https://docs.example.test/changelog#b",
        "input": {"url": "https://changelog.example.test/"},
    },
]


def test_records_path_unwraps_and_concatenates_every_wrapper() -> None:
    rows = extract_rows(WRAPPED, records_path="changelog_entries")
    assert [r["entry_title"] for r in rows] == [
        "August 10-17, 2026",
        "July 13-17, 2026",
        "June 8-12, 2026",
    ]


def test_wrapper_shape_is_unwrapped_even_without_records_path() -> None:
    """Otherwise every field contract fails on a scraper that works perfectly."""
    assert len(extract_rows(WRAPPED)) == 3


def test_records_path_survives_a_transport_envelope() -> None:
    assert len(extract_rows({"data": WRAPPED}, records_path="changelog_entries")) == 3


def test_a_missing_records_path_falls_back_rather_than_returning_nothing() -> None:
    rows = extract_rows(WRAPPED, records_path="nonexistent_key")
    assert len(rows) == 3  # fell through to wrapper detection


def test_flat_rows_are_left_alone() -> None:
    """Only uniform single-list wrappers are unwrapped; real data passes through."""
    flat = [{"title": "a", "tags": [{"x": 1}]}, {"title": "b"}]
    assert extract_rows(flat) == flat


def test_run_passes_records_path_through(client: BrightDataClient, runner: FakeRunner) -> None:
    runner.queue("run", WRAPPED)
    rows = client.run("c_1", "https://x.test", records_path="changelog_entries")
    assert len(rows) == 3
