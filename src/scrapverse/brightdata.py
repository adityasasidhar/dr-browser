"""Typed wrapper around the `bdata` CLI.

Scraper Studio is driven from the terminal, so this client shells out rather
than reimplementing the HTTP API. Every call goes through an injectable
`CommandRunner`, which keeps the rest of the pipeline testable without a
network, an account, or the 5-to-10-minute AI generation wait.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Protocol

# `scraper create` and `scraper heal` run an AI flow server-side; the CLI's own
# default polling timeout is 600s, so allow headroom above it before we give up.
CREATE_TIMEOUT = 900
HEAL_TIMEOUT = 900
# A normal run polls for ten minutes.  A long or paginated page can make the
# CLI fall back to Bright Data's batch path, whose documented default is one
# hour.  Killing the subprocess after 700 seconds would turn a healthy batch
# recovery into a false failed heal, so keep the two timeouts aligned.
RUN_POLL_TIMEOUT = 3_600
RUN_TIMEOUT = RUN_POLL_TIMEOUT + 300


class BrightDataError(RuntimeError):
    """A `bdata` invocation failed or returned something unusable."""


@dataclass(frozen=True)
class CommandResult:
    """Raw result of one CLI invocation."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandRunner(Protocol):
    """Anything that can execute an argv and return its output."""

    def __call__(self, argv: list[str], timeout: int) -> CommandResult: ...


def subprocess_runner(argv: list[str], timeout: int) -> CommandResult:
    """Execute a command, capturing output and never raising on non-zero exit."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BrightDataError(f"`{' '.join(argv[:4])} ...` exceeded {timeout}s") from exc
    except FileNotFoundError as exc:
        raise BrightDataError(f"command not found: {argv[0]}") from exc
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def default_base_command() -> list[str]:
    """Resolve how to invoke the CLI on this machine.

    Prefers a globally installed `bdata`, falls back to `npx`, which is what the
    hackathon guide documents and what CI uses.
    """
    override = os.environ.get("DR_BROWSER_BDATA_CMD") or os.environ.get("SCRAPVERSE_BDATA_CMD")
    if override:
        return override.split()
    if shutil.which("bdata"):
        return ["bdata"]
    if shutil.which("brightdata"):
        return ["brightdata"]
    return ["npx", "-y", "-p", "@brightdata/cli", "bdata"]


@dataclass
class HealOutcome:
    """Result of a heal attempt, as reported by the CLI envelope."""

    collector_id: str
    status: str
    prompt: str = ""
    next_step: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def awaiting_approval(self) -> bool:
        return self.status == "awaiting_approval"

    @property
    def succeeded(self) -> bool:
        return self.status in {"done", "completed", "approved", "saved", "success"}


class BrightDataClient:
    """Drives `bdata scraper create | run | heal | approve`."""

    def __init__(
        self,
        runner: CommandRunner | None = None,
        base_command: list[str] | None = None,
        api_key: str | None = None,
    ) -> None:
        self._run_command: CommandRunner = runner or subprocess_runner
        self._base = base_command or default_base_command()
        self._api_key = api_key or os.environ.get("BRIGHTDATA_API_KEY")

    def _argv(self, *args: str) -> list[str]:
        argv = [*self._base, *args]
        # An explicit key lets CI authenticate without the interactive `bdata login`.
        if self._api_key:
            argv += ["--api-key", self._api_key]
        return argv

    def _invoke(self, args: list[str], timeout: int) -> Any:
        result = self._run_command(self._argv(*args), timeout)
        if not result.ok:
            detail = (result.stderr or result.stdout or "").strip()
            raise BrightDataError(f"`bdata {' '.join(args[:3])}` failed: {detail[:400]}")
        return _parse_json(result.stdout)

    def create(self, url: str, description: str, name: str) -> str:
        """Generate a scraper with AI and return its Collector ID."""
        payload = self._invoke(
            ["scraper", "create", url, description, "--name", name, "--json"],
            CREATE_TIMEOUT,
        )
        collector_id = _dig(payload, "collector_id") or _dig(payload, "collectorId")
        if not isinstance(collector_id, str) or not collector_id:
            raise BrightDataError(f"no collector_id in create response: {str(payload)[:300]}")
        return collector_id

    def run(
        self,
        collector_id: str,
        url: str,
        sync: bool = False,
        records_path: str | None = None,
        poll_timeout: int = RUN_POLL_TIMEOUT,
    ) -> list[dict[str, Any]]:
        """Run a collector against a URL and return its rows."""
        args = ["scraper", "run", collector_id, url, "--json", "--timeout", str(poll_timeout)]
        if sync:
            args.append("--sync")
        return extract_rows(self._invoke(args, RUN_TIMEOUT), records_path=records_path)

    def heal(self, collector_id: str, prompt: str, url: str | None = None) -> HealOutcome:
        """Ask the AI to repair a collector, stopping at the approval gate.

        Deliberately does not pass `--auto-approve`: the approval gate is where
        a human (or an explicit policy) decides whether the proposed fix is
        acceptable, and collapsing it would hide the most interesting step.
        """
        args = ["scraper", "heal", collector_id, prompt[:1000], "--json"]
        if url:
            args += ["--url", url]
        return _heal_outcome(self._invoke(args, HEAL_TIMEOUT), collector_id)

    def approve(
        self, collector_id: str, reject: bool = False, url: str | None = None
    ) -> HealOutcome:
        """Approve or reject a heal that is awaiting approval."""
        args = ["scraper", "approve", collector_id, "--json"]
        if reject:
            args.append("--reject")
        else:
            # Persist the healed template so the next run picks it up.
            args.append("--auto-save")
        if url:
            args += ["--url", url]
        return _heal_outcome(self._invoke(args, HEAL_TIMEOUT), collector_id)


def _heal_outcome(payload: Any, collector_id: str) -> HealOutcome:
    raw = payload if isinstance(payload, dict) else {"payload": payload}
    return HealOutcome(
        collector_id=str(_dig(raw, "collector_id") or collector_id),
        status=str(_dig(raw, "status") or "unknown"),
        prompt=str(_dig(raw, "prompt") or ""),
        next_step=str(_dig(raw, "next_step") or ""),
        raw=raw,
    )


def _parse_json(stdout: str) -> Any:
    """Parse CLI stdout as JSON, tolerating leading progress chatter.

    The CLI prints human-readable progress before the JSON envelope in some
    modes, so fall back to scanning for the first balanced JSON value.
    """
    text = stdout.strip()
    if not text:
        raise BrightDataError("bdata returned no output")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise BrightDataError(f"could not parse bdata output as JSON: {text[:300]}")


def _dig(payload: Any, key: str) -> Any:
    """Find `key` at the top level or one level down in a response envelope."""
    if not isinstance(payload, dict):
        return None
    if key in payload:
        return payload[key]
    for value in payload.values():
        if isinstance(value, dict) and key in value:
            return value[key]
    return None


def extract_rows(payload: Any, records_path: str | None = None) -> list[dict[str, Any]]:
    """Pull the row list out of whatever envelope the CLI returned.

    Run output has appeared as a bare array, as `{"data": [...]}`, and as
    `{"result": {"data": [...]}}` across CLI versions. A generated scraper adds
    another layer: it commonly returns one wrapper per page visited, with the
    real records nested under a scraper-chosen key. `records_path` names that
    key explicitly; without it we fall back to detecting the wrapper shape.
    """
    if records_path:
        nested = _collect_at(payload, records_path)
        if nested:
            return nested
    rows = _envelope_rows(payload)
    return _flatten_wrappers(rows)


def _collect_at(payload: Any, key: str) -> list[dict[str, Any]]:
    """Gather and concatenate every list found at `key`, at any depth."""
    found: list[dict[str, Any]] = []
    if isinstance(payload, list):
        for item in payload:
            found.extend(_collect_at(item, key))
    elif isinstance(payload, dict):
        value = payload.get(key)
        if isinstance(value, list):
            found.extend(row for row in value if isinstance(row, dict))
        for nested in payload.values():
            if isinstance(nested, list | dict) and nested is not value:
                found.extend(_collect_at(nested, key))
    return found


def _envelope_rows(payload: Any) -> list[dict[str, Any]]:
    """Unwrap the transport envelope down to a list of dicts."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("data", "rows", "results", "items", "output"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        # Fall back to descending through wrapper keys we do not know by name.
        for value in payload.values():
            if isinstance(value, dict):
                nested = _envelope_rows(value)
                if nested:
                    return nested
    return []


def _flatten_wrappers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten page-wrapper rows into the records they contain.

    A wrapper looks like `{"changelog_entries": [...], "input": {...}}` — it
    carries exactly one list-of-dicts and no useful scalar data of its own.
    Returning those wrappers unchanged would make every field contract fail on
    a scraper that is working perfectly, so detect and unwrap them.
    """
    if not rows:
        return rows
    flattened: list[dict[str, Any]] = []
    for row in rows:
        lists = [v for v in row.values() if isinstance(v, list) and v and _all_dicts(v)]
        if len(lists) != 1:
            return rows  # not a uniform wrapper shape; leave the rows alone
        flattened.extend(lists[0])
    return flattened


def _all_dicts(value: list[Any]) -> bool:
    return all(isinstance(item, dict) for item in value)
