"""Shared fixtures: a scriptable stand-in for the `bdata` CLI.

Injecting a fake `CommandRunner` rather than mocking `BrightDataClient` means
tests exercise the real argv construction, JSON parsing, and envelope handling —
everything except the subprocess itself.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from scrapverse.brightdata import BrightDataClient, CommandResult
from scrapverse.config import Config, FieldContract, Target
from scrapverse.store import Store


class FakeRunner:
    """Answers `bdata` invocations from queued, per-subcommand responses."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._queues: dict[str, deque[Any]] = {}

    def queue(self, subcommand: str, *payloads: Any) -> None:
        """Queue responses for a subcommand, consumed in order."""
        self._queues.setdefault(subcommand, deque()).extend(payloads)

    def _subcommand(self, argv: list[str]) -> str:
        for candidate in ("create", "run", "heal", "approve"):
            if candidate in argv:
                return candidate
        raise AssertionError(f"unrecognised bdata invocation: {argv}")

    def __call__(self, argv: list[str], timeout: int) -> CommandResult:
        self.calls.append(argv)
        sub = self._subcommand(argv)
        queue = self._queues.get(sub)
        if not queue:
            raise AssertionError(f"no queued response for `bdata scraper {sub}`")
        payload = queue.popleft()
        if isinstance(payload, CommandResult):
            return payload
        return CommandResult(0, json.dumps(payload), "")

    def calls_for(self, subcommand: str) -> list[list[str]]:
        return [c for c in self.calls if subcommand in c]


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def client(runner: FakeRunner) -> BrightDataClient:
    return BrightDataClient(runner=runner, base_command=["bdata"], api_key=None)


@pytest.fixture
def target() -> Target:
    return Target(
        name="demo",
        url="https://example.test/changelog",
        description="Extract each changelog entry: title, date, url.",
        collector_id="c_demo123",
        key_fields=["title"],
        min_rows=3,
        fields=[
            FieldContract(name="title", required=True, min_fill_rate=0.9, description="the title"),
            FieldContract(
                name="date",
                required=True,
                min_fill_rate=0.8,
                pattern=r"\d{4}",
                description="a date containing a year",
            ),
            FieldContract(name="url", required=False, min_fill_rate=0.8, pattern=r"^https?://"),
        ],
    )


@pytest.fixture
def config(target: Target) -> Config:
    return Config(targets=[target])


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "test.db")


def rows(count: int = 5, **overrides: Any) -> list[dict[str, Any]]:
    """Build a batch of well-formed rows, with optional per-field overrides."""
    out: list[dict[str, Any]] = []
    for i in range(count):
        row: dict[str, Any] = {
            "title": f"Release {i}",
            "date": f"2026-08-{i + 1:02d}",
            "url": f"https://example.test/r/{i}",
        }
        row.update(overrides)
        out.append(row)
    return out
