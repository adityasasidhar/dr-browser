"""Populate a store with synthetic history so the dashboard can be previewed.

The data this writes is FABRICATED — it exists to exercise the renderer and to
give the UI something to show before a real collection has run twice. Never
point it at the production store.

    uv run python scripts/seed_demo.py --db data/demo.db
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

from scrapverse.config import Target, load_config
from scrapverse.diff import compute
from scrapverse.health import check
from scrapverse.models import HealEvent, HealStatus, Snapshot, utcnow
from scrapverse.report import render
from scrapverse.store import Store

# Filler: the back catalogue each target already had before the demo window.
# It repeats on purpose and carries a "(demo N)" marker, because it only ever
# reaches the page as a row count and a fill rate, never as a headline.
ENTRIES = {
    "langchain": [
        ("LangGraph 1.2 checkpointing", "2026-08-16", "Durable execution for long-running graphs."),
        ("Structured output on streaming", "2026-08-12", "Partial parses now emit typed deltas."),
        ("Tracing export to OTLP", "2026-08-05", "Send traces to any OpenTelemetry collector."),
        ("Retriever cache invalidation", "2026-07-29", "TTL and manual purge for cached lookups."),
        ("Python 3.13 support", "2026-07-21", "Wheels published for 3.13 across the stack."),
    ],
    "anthropic-api": [
        ("Batch API concurrency raised", "2026-08-15", "Higher per-org concurrent batch limits."),
        ("Citations in tool results", "2026-08-09", "Tool outputs carry source spans."),
        ("Extended prompt caching TTL", "2026-08-01", "One-hour cache retention available."),
        ("Token counting endpoint", "2026-07-24", "Count tokens without sending a request."),
    ],
    "runpod-docs": [
        ("Network volume snapshots", "2026-08-14", "Point-in-time restore for volumes."),
        ("Serverless cold-start cuts", "2026-08-07", "Median cold start down 40% on H100."),
        ("Per-endpoint GPU pinning", "2026-07-30", "Pin an endpoint to a GPU generation."),
    ],
    "ollama": [
        ("Cloud models in the CLI", "2026-08-13", "Run :cloud models without local weights."),
        ("Structured outputs", "2026-08-06", "JSON schema constrained decoding."),
        ("Flash attention by default", "2026-07-28", "Enabled where the GPU supports it."),
        ("Model library refresh", "2026-07-19", "New quantizations for small models."),
        ("Multi-GPU scheduling", "2026-07-11", "Split large models across devices."),
    ],
}

# Arrivals: the one new entry each day brings. These are the rows the change
# feed actually renders, so each is distinct — a feed of repeated headlines
# would say nothing about how the diff engine aligns two collections.
ARRIVALS = {
    "langchain": [
        ("Streaming tool calls in LangGraph", "Tool invocations emit deltas as they resolve."),
        ("Postgres checkpointer", "Durable graph state without a bespoke backend."),
        ("Per-node retry policies", "Back-off and attempt limits declared on the node."),
        ("Typed state reducers", "State merges are checked against the graph schema."),
        ("Interrupt and resume", "Pause a run for review, resume from the checkpoint."),
        ("Trace sampling controls", "Sample by run, by node, or by tag."),
        ("Async batch embeddings", "Embed a corpus without blocking the event loop."),
    ],
    "anthropic-api": [
        ("Strict schemas for tool use", "Tool inputs are validated before the call is made."),
        ("Message batches past 100k requests", "Larger jobs submit without being split."),
        ("Cache metrics on every response", "Read and write counts per cache breakpoint."),
        ("PDF input on the Messages API", "Documents are read without a conversion step."),
        ("Fine-grained tool streaming", "Arguments stream as they are produced."),
        ("Per-key usage reporting", "Attribute spend to the key that incurred it."),
        ("Long-context pricing tiers", "Separate rates above the 200k token boundary."),
    ],
    "runpod-docs": [
        ("Instant clusters", "Multi-node jobs schedule as a single unit."),
        ("S3-compatible network volumes", "Read and write volume data over the S3 API."),
        ("Endpoint request tracing", "Follow one request across queue, worker, and response."),
        ("GPU availability webhooks", "Get told when a GPU type frees up in a region."),
        ("Per-worker log retention", "Keep worker logs past the life of the worker."),
        ("Bring your own registry", "Pull images from a private registry with delegated auth."),
        ("Serverless queue priorities", "Move short requests ahead of long ones."),
    ],
    "ollama": [
        ("Tool calling in the REST API", "Models return structured tool calls over HTTP."),
        ("Per-model context overrides", "Set a context window per model, not per host."),
        ("Vulkan backend preview", "Run on GPUs without a vendor compute runtime."),
        ("Signed model manifests", "Verify a model's provenance before it loads."),
        ("Concurrent request scheduling", "Serve several requests from one loaded model."),
        ("Embedding model library", "Pull embedding models the way you pull chat models."),
        ("Windows ARM builds", "Native binaries for ARM64 Windows devices."),
    ],
}

# How often the demo leaves an optional field empty, matching the floors in
# targets.toml. A preview where every gauge reads 100% teaches nothing about
# what a gauge is for.
FILL_RATE = {
    ("langchain", "permalink_url"): 0.875,
    ("runpod-docs", "summary_text"): 0.8,
    ("ollama", "summary"): 0.34,
}

# Eight daily collections per target: enough history for the collection strip
# to have a shape, and for a break to sit in the middle of it.
DAYS = 8


def _entries(target: str, count: int) -> list[tuple[str, str, str]]:
    """Return enough distinct canned entries to satisfy a target's row contract."""
    base = ENTRIES[target]
    entries: list[tuple[str, str, str]] = []
    for index in range(count):
        title, date, body = base[index % len(base)]
        entries.append((f"{title} (demo {index + 1})", date, body))
    return entries


def as_rows(target: str, baseline: int, day: int, opened: datetime) -> list[dict[str, str]]:
    """The rows a collection returns on `day`: the back catalogue plus arrivals."""
    entries = _entries(target, baseline)
    for index in range(day):
        title, body = ARRIVALS[target][index % len(ARRIVALS[target])]
        published = (opened + timedelta(days=index + 1)).date().isoformat()
        entries.append((title, published, body))
    return [_row(target, index, *entry) for index, entry in enumerate(entries)]


def _row(target: str, index: int, title: str, date: str, body: str) -> dict[str, str]:
    """Render one entry with the exact field names in ``targets.toml``."""
    url = f"https://example.invalid/{target}/{index}"
    if target == "langchain":
        row = {
            "entry_title": title,
            "publication_date": date,
            "summary_text": body,
            "permalink_url": url,
        }
    elif target == "anthropic-api":
        row = {"title": title, "release_date": date, "description": body}
    elif target == "runpod-docs":
        row = {"entry_title": title, "publication_date": date, "summary_text": body}
    elif target == "ollama":
        row = {"post_title": title, "publication_date": date, "post_url": url, "summary": body}
    else:  # pragma: no cover - configuration controls the fixture keys.
        raise ValueError(f"no fixture schema for target {target!r}")
    return _thin(target, row, index)


def _thin(target: str, row: dict[str, str], index: int) -> dict[str, str]:
    """Empty an optional field on the share of rows its contract expects.

    Spread evenly rather than randomly so the same day always produces the same
    rows, which keeps the diff between two days down to the entry that is
    genuinely new.
    """
    thinned = dict(row)
    for (name, field), rate in FILL_RATE.items():
        if name != target or field not in thinned:
            continue
        if int(index * rate) == int((index + 1) * rate):
            thinned[field] = ""
    return thinned


def mutate(target: str, rows: list[dict[str, str]], day: int) -> list[dict[str, str]]:
    """Apply the edits and withdrawals a real changelog page accumulates.

    Both changes persist once they happen, so the feed shows one edit and one
    withdrawal rather than a row flapping between two states every day.
    """
    if target == "ollama" and day >= 4:
        rows = list(rows)
        rows[1] = {**rows[1], "summary": "Rewritten: scheduling now spans devices automatically."}
    if target == "anthropic-api" and day >= 6:
        rows = [row for index, row in enumerate(rows) if index != 2]
    return rows


def mark_synthetic_dashboard(path: Path) -> None:
    """Make sure a preview cannot be mistaken for evidence from a live collection.

    Inserted after the opening `<body>` tag whatever attributes it carries, so
    a change to the template cannot quietly drop the notice.
    """
    notice = (
        '<p style="margin:0;padding:11px 24px;background:#12161c;color:#ffd166;'
        "font:500 12px/1.4 ui-monospace,'JetBrains Mono',monospace;text-align:center;"
        'letter-spacing:.06em;position:relative;z-index:30">'
        "SYNTHETIC DEMO DATA — preview only, not a live Bright Data collection."
        "</p>"
    )
    html = path.read_text()
    before, tag, after = html.partition("<body")
    open_tag, close, body = after.partition(">")
    if not tag or not close:
        raise ValueError(f"{path} has no <body> tag to mark as synthetic")
    path.write_text(f"{before}{tag}{open_tag}{close}{notice}{body}")


def _break_and_heal(
    store: Store,
    target: Target,
    collector: str,
    rows: list[dict[str, str]],
    stamp: datetime,
) -> datetime:
    """Record one break and its verified repair, and return the re-run's time.

    The date column started returning link text instead of dates — the failure
    that fill-rate checking alone would miss, because the field stays populated.
    """
    broken = Snapshot(
        target=target.name,
        collector_id=collector,
        rows=[{**row, "publication_date": "Read more"} for row in rows],
        fetched_at=stamp,
    )
    report = check(broken, target)
    store.save_snapshot(broken, healthy=False)

    recovered_at = stamp + timedelta(seconds=214)
    store.save_heal_event(
        HealEvent(
            target=target.name,
            collector_id=collector,
            prompt=report.heal_prompt(),
            violations=report.violations,
            status=HealStatus.VERIFIED,
            detected_at=stamp,
            resolved_at=recovered_at,
            notes=f"verified: {len(rows)} rows now satisfy the contract",
        )
    )
    return recovered_at


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/demo.db"))
    parser.add_argument("--config", type=Path, default=Path("targets.toml"))
    parser.add_argument("--output", type=Path, default=Path("site/index.html"))
    args = parser.parse_args()

    config = load_config(args.config)
    store = Store(args.db)
    opened = utcnow() - timedelta(days=DAYS - 1)

    for target in config.targets:
        collector = target.collector_id or f"c_demo{target.name}"
        previous: Snapshot | None = None

        for day in range(DAYS):
            stamp = opened + timedelta(days=day)
            rows = mutate(target.name, as_rows(target.name, target.min_rows, day, opened), day)

            if target.name == "runpod-docs" and day == 4:
                stamp = _break_and_heal(store, target, collector, rows, stamp)

            snapshot = Snapshot(
                target=target.name, collector_id=collector, rows=rows, fetched_at=stamp
            )
            store.save_snapshot(snapshot)

            diff = compute(previous, snapshot, target)
            # Date the diff by the collection that produced it, not by the wall
            # clock, or every target's changes arrive in one indistinguishable
            # burst at the top of the feed.
            diff.computed_at = stamp
            store.save_diff(diff)
            previous = snapshot

    path = render(config, store, args.output)
    mark_synthetic_dashboard(path)
    print(f"seeded {args.db} with SYNTHETIC data and wrote {path}")


if __name__ == "__main__":
    main()
