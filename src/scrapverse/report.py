"""Dashboard rendering and the starter configuration.

The pipeline's output is only useful if someone reads it, so `render` produces a
single self-contained HTML file: no build step, no server, publishable straight
to GitHub Pages from the same CI job that collects the data.

Everything above `render` is view models. They exist so the template can stay
declarative: a contract becomes a `Gauge` with a measurement and a line to
clear, and the page draws the two against each other.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

from .config import Config, FieldContract, Target
from .health import DUPLICATE_TOLERANCE, PATTERN_TOLERANCE, VANISHED_THRESHOLD, check
from .models import Diff, HealEvent, HealStatus, HealthReport, Snapshot, is_present, utcnow
from .store import CollectionRecord, Store

TEMPLATE_DIR = Path(__file__).parent / "templates"

# A pattern check fails once more than `PATTERN_TOLERANCE` of the populated
# values mismatch, so this is the same line read from the passing side — which
# is the direction a gauge has to be drawn in.
MIN_MATCH_RATE = 1.0 - PATTERN_TOLERANCE

# Likewise for the distinctness check. Read as "how many rows hold their own
# value", every contract on the page points the same way: higher is passing,
# and the floor is a line to clear.
MIN_DISTINCT_RATE = 1.0 - DUPLICATE_TOLERANCE

STARTER_CONFIG = '''# Dr Browser targets.
#
# Each target carries a contract: the shape its rows must have. The contract is
# what lets the pipeline notice that a site changed, and it is what gets turned
# into a heal prompt when it does. Tighten `min_fill_rate` and `pattern` once
# you have seen a few real collections.
#
# Fill `collector_id` by running `dr-browser provision`.

[[targets]]
name = "langchain"
url = "https://changelog.langchain.com/"
description = """For each changelog entry on the page, extract the entry title, \
the publication date, the short summary text, and the permalink URL to that entry."""
collector_id = ""
key_fields = ["title"]
min_rows = 5

  [[targets.fields]]
  name = "title"
  required = true
  min_fill_rate = 0.95
  description = "the headline of a changelog entry"

  [[targets.fields]]
  name = "date"
  required = true
  min_fill_rate = 0.9
  pattern = "\\\\d{4}"
  description = "the publication date, containing a four-digit year"

  [[targets.fields]]
  name = "url"
  required = false
  min_fill_rate = 0.8
  pattern = "^https?://"
  description = "an absolute permalink to the entry"

[[targets]]
name = "anthropic-api"
url = "https://docs.claude.com/en/release-notes/api"
description = """For each release-note entry, extract the release date, the entry \
title or headline, and the description of what changed."""
collector_id = ""
key_fields = ["title"]
min_rows = 3

  [[targets.fields]]
  name = "title"
  required = true
  min_fill_rate = 0.9
  description = "the headline of a release note"

  [[targets.fields]]
  name = "date"
  required = true
  min_fill_rate = 0.85
  pattern = "\\\\d{4}"
  description = "the release date, containing a four-digit year"

  [[targets.fields]]
  name = "description"
  required = false
  min_fill_rate = 0.7
  description = "what changed in this release"

[[targets]]
name = "runpod"
url = "https://docs.runpod.io/changelog"
description = """For each changelog entry, extract the entry title, the date it \
was published, and the body text describing the change."""
collector_id = ""
key_fields = ["title"]
min_rows = 3

  [[targets.fields]]
  name = "title"
  required = true
  min_fill_rate = 0.9
  description = "the headline of a changelog entry"

  [[targets.fields]]
  name = "date"
  required = true
  min_fill_rate = 0.8
  pattern = "\\\\d{4}"
  description = "the publication date, containing a four-digit year"

[[targets]]
name = "ollama"
url = "https://ollama.com/blog"
description = """For each blog post listed, extract the post title, the \
publication date, the summary or excerpt, and the link to the full post."""
collector_id = ""
key_fields = ["title"]
min_rows = 5

  [[targets.fields]]
  name = "title"
  required = true
  min_fill_rate = 0.95
  description = "the title of a blog post"

  [[targets.fields]]
  name = "date"
  required = true
  min_fill_rate = 0.85
  pattern = "\\\\d{4}"
  description = "the publication date, containing a four-digit year"

  [[targets.fields]]
  name = "url"
  required = false
  min_fill_rate = 0.8
  pattern = "^https?://"
  description = "an absolute link to the post"
'''


@dataclass(frozen=True)
class Gauge:
    """One field contract, measured against the collection that just landed.

    A contract is a claim with a number attached, so the dashboard renders it as
    a measurement against a line: `fill` and `match` are what the page observed,
    `min_fill` and `MIN_MATCH_RATE` are the lines they have to clear.
    """

    field: str
    description: str
    required: bool
    fill: float
    min_fill: float
    pattern: str | None = None
    match: float | None = None
    rival: str | None = None
    distinct: float | None = None

    @property
    def fill_ok(self) -> bool:
        return self.fill >= self.min_fill

    @property
    def match_ok(self) -> bool:
        return self.match is None or self.match >= MIN_MATCH_RATE

    @property
    def distinct_ok(self) -> bool:
        return self.distinct is None or self.distinct >= MIN_DISTINCT_RATE

    @property
    def vanished(self) -> bool:
        """The selector resolves to nothing at all, not merely to too little."""
        return self.fill <= VANISHED_THRESHOLD

    @property
    def ok(self) -> bool:
        return self.fill_ok and self.match_ok and self.distinct_ok

    @property
    def headroom(self) -> float:
        """How far the weakest of this field's measurements sits above its line."""
        slack = [self.fill - self.min_fill]
        if self.match is not None:
            slack.append(self.match - MIN_MATCH_RATE)
        if self.distinct is not None:
            slack.append(self.distinct - MIN_DISTINCT_RATE)
        return min(slack)


def measure(snapshot: Snapshot, contract: FieldContract) -> Gauge:
    """Measure one field of a snapshot against every clause of its contract."""
    populated = [v for v in (row.get(contract.name) for row in snapshot.rows) if is_present(v)]
    match: float | None = None
    if contract.pattern is not None and populated:
        match = sum(1 for value in populated if contract.matches(value)) / len(populated)

    rival, distinct = _closest_rival(snapshot, contract)
    return Gauge(
        field=contract.name,
        description=contract.description,
        required=contract.required,
        fill=snapshot.fill_rate(contract.name),
        min_fill=contract.min_fill_rate,
        pattern=contract.pattern,
        match=match,
        rival=rival,
        distinct=distinct,
    )


def _closest_rival(snapshot: Snapshot, contract: FieldContract) -> tuple[str | None, float | None]:
    """The field this one most nearly duplicates, and how often it stays its own.

    Reported for the worst pairing only: one field collapsing onto another is
    one fault, and naming the pair it collapsed onto is what makes it fixable.
    """
    worst: tuple[str, float] | None = None
    for other in contract.distinct_from:
        pairs = [
            (row.get(contract.name), row.get(other))
            for row in snapshot.rows
            if is_present(row.get(contract.name)) and is_present(row.get(other))
        ]
        if not pairs:
            continue
        same = sum(1 for a, b in pairs if _flatten(a) == _flatten(b))
        rate = 1.0 - same / len(pairs)
        if worst is None or rate < worst[1]:
            worst = (other, rate)
    return worst if worst else (None, None)


def _flatten(value: Any) -> str:
    """Whitespace-insensitive comparison form, matching the health check."""
    return " ".join(str(value).split())


@dataclass(frozen=True)
class TargetView:
    """Everything the dashboard shows about one watched source."""

    target: Target
    snapshot: Snapshot | None
    report: HealthReport | None
    gauges: list[Gauge]
    history: list[CollectionRecord]
    heals: list[HealEvent]

    @property
    def name(self) -> str:
        return self.target.name

    @property
    def host(self) -> str:
        """The source site, as a reader would name it."""
        return urlparse(self.target.url).netloc.removeprefix("www.")

    @property
    def state(self) -> str:
        """Status slug, with a fourth value for a target that has never run."""
        if self.report is None:
            return "waiting"
        return str(self.report.status.value)

    @property
    def row_count(self) -> int:
        return self.snapshot.row_count if self.snapshot else 0

    @property
    def collected_at(self) -> datetime | None:
        return self.snapshot.fetched_at if self.snapshot else None

    @property
    def recovered(self) -> bool:
        """This target has come back from a break on its own at least once."""
        return any(h.status is HealStatus.VERIFIED for h in self.heals)

    @property
    def recovery_seconds(self) -> float | None:
        """End-to-end time of the most recent verified repair, if any.

        `heals` arrives newest first from the store, so the first verified
        event with a measurable duration is the latest recovery to display.
        """
        verified = (h for h in self.heals if h.status is HealStatus.VERIFIED)
        return next((h.duration_seconds for h in verified if h.duration_seconds), None)

    @property
    def failing(self) -> list[Gauge]:
        return [g for g in self.gauges if not g.ok]

    @property
    def verdict(self) -> str:
        """One line naming the target's current condition, in the page's voice."""
        if self.report is None:
            return "Waiting for its first collection"
        if self.state == "broken":
            return "Contract broken — output is not trustworthy"
        failing = self.failing
        if failing:
            names = ", ".join(g.field for g in failing)
            return f"Drifting on {names}"
        if not self.gauges:
            return "Collected, no field contract declared"
        return f"All {len(self.gauges)} field contracts holding"


@dataclass(frozen=True)
class FeedItem:
    """One row that moved on a source, flattened out of its diff."""

    target: str
    kind: str
    when: datetime
    title: str
    url: str | None
    date: str | None
    summary: str | None
    fields_changed: list[str]


@dataclass(frozen=True)
class Hero:
    """The page's opening statement, in the pipeline's own words.

    Split in two because a symptom leads with its finding and then justifies it;
    set whole, the justification is as loud as the finding.
    """

    eyebrow: str
    lede: str
    detail: str = ""
    heal: HealEvent | None = None
    target: str | None = None


def split_statement(text: str) -> tuple[str, str]:
    """Break a sentence off the front of a symptom, keeping its full stop."""
    head, stop, tail = text.partition(". ")
    if not stop:
        return text.strip(), ""
    return f"{head}.", tail.strip()


@dataclass(frozen=True)
class Stats:
    """The numbers the four pipeline stages have produced so far."""

    targets: int
    collections: int
    entries: int
    contracts: int
    changes: int
    heals: int
    heals_verified: int
    recovery_seconds: float | None

    @property
    def heal_rate(self) -> int:
        if not self.heals:
            return 0
        return round(100 * self.heals_verified / self.heals)


def build_views(config: Config, store: Store) -> list[TargetView]:
    """Assemble the per-target panels, newest measurement first."""
    heals = store.recent_heal_events(limit=200)
    views: list[TargetView] = []
    for target in config.targets:
        snapshot = store.latest_snapshot(target.name, healthy_only=False)
        views.append(
            TargetView(
                target=target,
                snapshot=snapshot,
                report=check(snapshot, target) if snapshot else None,
                gauges=[measure(snapshot, f) for f in target.fields] if snapshot else [],
                history=store.collections(target.name, limit=32),
                heals=[h for h in heals if h.target == target.name],
            )
        )
    return views


def build_stats(views: list[TargetView], diffs: list[Diff], heals: list[HealEvent]) -> Stats:
    """Summarize the store into the counters shown across the pipeline rail."""
    verified = [h for h in heals if h.status is HealStatus.VERIFIED]
    durations = [h.duration_seconds for h in verified if h.duration_seconds]
    return Stats(
        targets=len(views),
        collections=sum(len(v.history) for v in views),
        entries=sum(v.row_count for v in views),
        contracts=sum(len(v.gauges) for v in views),
        changes=sum(len(d.entries) for d in diffs),
        heals=len(heals),
        heals_verified=len(verified),
        recovery_seconds=sum(durations) / len(durations) if durations else None,
    )


def build_feed(diffs: list[Diff], limit: int = 60) -> list[FeedItem]:
    """Flatten every diff into one reverse-chronological stream."""
    items = [
        FeedItem(
            target=diff.target,
            kind=entry.kind,
            when=diff.computed_at,
            title=headline(entry.row),
            url=link(entry.row),
            date=row_date(entry.row),
            summary=row_summary(entry.row),
            fields_changed=entry.fields_changed,
        )
        for diff in diffs
        for entry in diff.entries
    ]
    items.sort(key=lambda i: i.when, reverse=True)
    return items[:limit]


def build_hero(views: list[TargetView], heals: list[HealEvent]) -> Hero:
    """Open the page with the most consequential thing the pipeline has said.

    A recovery outranks a clean bill of health, because a recovery is the claim
    the whole project is making; a clean bill is only evidence the tripwire is
    armed.
    """
    if heals:
        heal = heals[0]
        symptom = heal.violations[0].symptom if heal.violations else heal.prompt
        lede, detail = split_statement(symptom)
        return Hero(
            eyebrow="Latest break, in the pipeline's own words",
            lede=lede,
            detail=detail,
            heal=heal,
            target=heal.target,
        )

    measured = [(v, g) for v in views for g in v.gauges]
    if measured:
        view, gauge = min(measured, key=lambda pair: pair[1].headroom)
        return Hero(
            eyebrow="No break yet — this is the closest call",
            lede="Every declared contract is holding.",
            detail=(
                f"The narrowest margin is `{gauge.field}` on {view.name}, populated on "
                f"{gauge.fill:.0%} of rows against its {gauge.min_fill:.0%} floor."
            ),
            target=view.name,
        )

    return Hero(
        eyebrow="Nothing collected yet",
        lede="No collection has run against these contracts.",
        detail="Run `dr-browser pull` to take the first measurement.",
    )


def headline(row: dict[str, Any]) -> str:
    """Best available human label for a diff row."""
    for key in ("title", "entry_title", "post_title", "name", "version", "tag", "headline"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return next(
        (str(v)[:80] for v in row.values() if isinstance(v, str) and v.strip()),
        "(untitled)",
    )


def link(row: dict[str, Any]) -> str | None:
    """A row's outbound URL, if it captured one."""
    for key in ("url", "permalink_url", "post_url", "link", "permalink", "href"):
        value = row.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    return None


def row_date(row: dict[str, Any]) -> str | None:
    """The row's own published date, whatever the collector called the field."""
    for key in ("date", "publication_date", "release_date", "published_at", "posted"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def row_summary(row: dict[str, Any]) -> str | None:
    """The row's body text, trimmed to a single line of preview."""
    for key in ("summary_text", "description", "summary", "body", "excerpt", "content"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            return text if len(text) <= 180 else text[:177].rstrip() + "…"
    return None


def as_symptom(text: str) -> Markup:
    """Set a machine-written sentence, with its backticked identifiers as code.

    The symptom is the one place the pipeline writes English about itself, so it
    is worth typesetting rather than dumping — the field names inside it are the
    part a reader scans for.
    """
    out: list[str] = []
    for index, part in enumerate(str(text).split("`")):
        if not part:
            continue
        out.append(f"<code>{escape(part)}</code>" if index % 2 else str(escape(part)))
    return Markup("".join(out))


def as_percent(value: float | None) -> str:
    """Percentages are read as labels here, never recomputed, so round hard."""
    return "—" if value is None else f"{round(value * 100)}%"


def as_duration(seconds: float | None) -> str:
    """Human-length recovery times, at the precision a reader can act on."""
    if seconds is None:
        return "—"
    total = round(seconds)
    if total < 60:
        return f"{total}s"
    minutes, remainder = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def render(config: Config, store: Store, output: Path) -> Path:
    """Write the dashboard to `output` and return the path."""
    diffs = [d for d in store.recent_diffs(limit=200) if not d.is_empty]
    heals = store.recent_heal_events(limit=100)
    views = build_views(config, store)

    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["headline"] = headline
    env.filters["link"] = link
    env.filters["symptom"] = as_symptom
    env.filters["percent"] = as_percent
    env.filters["duration"] = as_duration

    feed = build_feed(diffs)
    html = env.get_template("dashboard.html.jinja").render(
        config=config,
        views=views,
        stats=build_stats(views, diffs, heals),
        hero=build_hero(views, heals),
        feed=feed,
        feed_total=sum(len(d.entries) for d in diffs),
        heals=heals,
        min_match_rate=MIN_MATCH_RATE,
        min_distinct_rate=MIN_DISTINCT_RATE,
        generated_at=utcnow(),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html)
    return output
