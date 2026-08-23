"""Command-line interface.

The terminal is the primary UI for building and operating the pipeline; the
generated dashboard is for reading its output. Commands map one-to-one onto
pipeline stages so each can be run and inspected on its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import heal as healmod
from . import report as reportmod
from .brightdata import BrightDataClient, BrightDataError
from .config import Config, Target, load_config, save_collector_id
from .health import check
from .models import HealStatus, Health, HealthReport, Snapshot, dedupe_rows
from .pipeline import PullResult, pull
from .store import DEFAULT_DB_PATH, Store

app = typer.Typer(
    add_completion=False,
    help="Dr Browser: self-healing changelog intelligence on Bright Data Scraper Studio.",
    no_args_is_help=True,
)
console = Console()

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="Path to targets.toml")]
DbOpt = Annotated[Path, typer.Option("--db", help="Path to the SQLite store")]

_HEALTH_STYLE = {
    Health.HEALTHY: "bold green",
    Health.DEGRADED: "bold yellow",
    Health.BROKEN: "bold red",
}


def _load(config_path: Path) -> Config:
    try:
        return load_config(config_path)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


def _select(config: Config, name: str | None) -> list[Target]:
    return [config.target(name)] if name else config.targets


@app.command()
def init(
    config: ConfigOpt = Path("targets.toml"),
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing config")] = False,
) -> None:
    """Write a starter targets.toml with contracts already filled in."""
    if config.exists() and not force:
        console.print(f"[yellow]{config} already exists — pass --force to overwrite[/yellow]")
        raise typer.Exit(1)
    config.write_text(reportmod.STARTER_CONFIG)
    console.print(f"[green]wrote {config}[/green]")
    console.print("Next: [bold]bdata login[/bold], then [bold]dr-browser provision[/bold]")


@app.command()
def provision(
    target: Annotated[str | None, typer.Argument(help="Only provision this target")] = None,
    config: ConfigOpt = Path("targets.toml"),
) -> None:
    """Create Scraper Studio collectors for targets that do not have one yet.

    AI generation takes 5-10 minutes per scraper, so this is deliberately a
    separate step from the daily `pull`.
    """
    cfg = _load(config)
    client = BrightDataClient()
    pending = [t for t in _select(cfg, target) if not t.is_provisioned]

    if not pending:
        console.print("[green]every target already has a collector_id[/green]")
        return

    console.print(
        f"provisioning [bold]{len(pending)}[/bold] scraper(s). "
        "AI generation takes 5-10 minutes each and the CLI backs off through "
        "rate limits, so long pauses are expected."
    )

    # Server-side AI generation blocks with no intermediate output, so drive a
    # live elapsed-time spinner — without it a working run is indistinguishable
    # from a hang, which is exactly how the first attempt got cancelled.
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        for item in pending:
            task = progress.add_task(f"{item.name} — generating scraper", total=None)
            try:
                collector_id = client.create(
                    item.url, item.description, name=f"dr-browser-{item.name}"
                )
            except BrightDataError as exc:
                progress.remove_task(task)
                console.print(f"[red]{item.name}: {exc}[/red]")
                continue
            progress.remove_task(task)
            # Persist immediately so an interrupted run never loses a scraper
            # that has already been paid for and created upstream.
            save_collector_id(config, item.name, collector_id)
            console.print(f"[green]{item.name} -> {collector_id}[/green]")

    remaining = [t.name for t in load_config(config).targets if not t.is_provisioned]
    if remaining:
        console.print(
            f"[yellow]still unprovisioned: {', '.join(remaining)} — "
            "re-run `dr-browser provision` to retry just those[/yellow]"
        )


@app.command()
def inspect(
    target: Annotated[str, typer.Argument(help="Target to inspect")],
    config: ConfigOpt = Path("targets.toml"),
    raw: Annotated[
        bool, typer.Option("--raw", help="Ignore records_path and show the transport envelope")
    ] = False,
) -> None:
    """Run a collector and report the schema it actually returns.

    Contracts have to match the field names the AI chose, which are not known
    until a scraper exists. Run this after `provision` and before writing the
    contract — guessing produces a first run that fails its own contract and
    fires a heal at a scraper that is working fine.
    """
    cfg = _load(config)
    item = cfg.target(target)
    if not item.collector_id:
        console.print(f"[red]{target} has no collector_id — run `dr-browser provision`[/red]")
        raise typer.Exit(1)

    client = BrightDataClient()
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        progress.add_task(f"{item.name} — running collector", total=None)
        rows = client.run(
            item.collector_id,
            item.url,
            records_path=None if raw else item.records_path,
        )

    if not rows:
        console.print("[yellow]collector returned no rows[/yellow]")
        raise typer.Exit(1)

    deduped = dedupe_rows(rows, item.key_fields) if item.key_fields else rows
    console.print(
        f"[bold]{len(rows)}[/bold] rows returned"
        + (
            f", [bold]{len(deduped)}[/bold] after dedupe on {item.key_fields}"
            if item.key_fields
            else ""
        )
    )

    keys = sorted({k for row in rows for k in row})
    table = Table(title=f"{item.name} — observed schema", header_style="bold")
    table.add_column("field")
    table.add_column("type")
    table.add_column("fill", justify="right")
    table.add_column("example")

    for key in keys:
        values = [row.get(key) for row in rows]
        present = [v for v in values if v not in (None, "", [], {})]
        example = next((v for v in present), None)
        kind = type(example).__name__ if example is not None else "-"
        if isinstance(example, list):
            kind = f"list[{type(example[0]).__name__}]" if example else "list"
        contract = item.field(key)
        marker = "" if contract else " [yellow](no contract)[/yellow]"
        table.add_row(
            f"{key}{marker}",
            kind,
            f"{len(present) / len(rows):.0%}",
            str(example)[:56].replace("\n", " "),
        )
    console.print(table)

    missing = [f.name for f in item.fields if f.name not in keys]
    if missing:
        console.print(
            f"[red]contract references fields the collector never returns: "
            f"{', '.join(missing)}[/red]"
        )


@app.command(name="pull")
def pull_cmd(
    target: Annotated[str | None, typer.Argument(help="Only pull this target")] = None,
    config: ConfigOpt = Path("targets.toml"),
    db: DbOpt = DEFAULT_DB_PATH,
    auto_approve: Annotated[
        bool, typer.Option("--auto-approve", help="Approve proposed heals without review")
    ] = False,
    no_heal: Annotated[
        bool, typer.Option("--no-heal", help="Detect breakage but do not repair")
    ] = False,
) -> None:
    """Collect every target, healing any whose output has drifted."""
    cfg = _load(config)
    store = Store(db)
    client = BrightDataClient()
    policy = healmod.ApprovalPolicy.AUTO if auto_approve else healmod.ApprovalPolicy.MANUAL

    results = [
        pull(client, item, store, auto_heal=not no_heal, policy=policy)
        for item in _select(cfg, target)
    ]
    _render_results(results)
    if any(r.error or (r.report and r.report.needs_heal) for r in results):
        raise typer.Exit(1)


@app.command()
def status(
    config: ConfigOpt = Path("targets.toml"),
    db: DbOpt = DEFAULT_DB_PATH,
) -> None:
    """Show each target's collector, contract, and collection history."""
    cfg = _load(config)
    store = Store(db)

    table = Table(title="Targets", header_style="bold")
    table.add_column("target")
    table.add_column("collector")
    table.add_column("fields")
    table.add_column("snapshots", justify="right")
    table.add_column("last run")

    for item in cfg.targets:
        latest = store.latest_snapshot(item.name, healthy_only=False)
        table.add_row(
            item.name,
            item.collector_id or "[yellow]not provisioned[/yellow]",
            str(len(item.fields)),
            str(store.snapshot_count(item.name)),
            latest.fetched_at.strftime("%Y-%m-%d %H:%M") if latest else "-",
        )
    console.print(table)


@app.command(name="heal")
def heal_cmd(
    target: Annotated[str, typer.Argument(help="Target to repair")],
    config: ConfigOpt = Path("targets.toml"),
    db: DbOpt = DEFAULT_DB_PATH,
    auto_approve: Annotated[
        bool, typer.Option("--auto-approve", help="Approve and verify in one step")
    ] = False,
) -> None:
    """Re-check a target and, if it is broken, ask Scraper Studio to fix it."""
    cfg = _load(config)
    item = cfg.target(target)
    store = Store(db)
    client = BrightDataClient()

    if not item.collector_id:
        console.print(f"[red]{target} has no collector_id[/red]")
        raise typer.Exit(1)

    rows = client.run(item.collector_id, item.url, records_path=item.records_path)
    rows = dedupe_rows(rows, item.key_fields)
    report = check(Snapshot(target=item.name, collector_id=item.collector_id, rows=rows), item)
    _render_report(report)

    if not report.needs_heal:
        console.print("[green]contract satisfied — nothing to heal[/green]")
        return

    policy = healmod.ApprovalPolicy.AUTO if auto_approve else healmod.ApprovalPolicy.MANUAL
    event, verified = healmod.attempt(client, item, report, policy=policy)
    store.save_heal_event(event)
    if verified is not None:
        store.save_snapshot(verified, healthy=event.status == "verified")
    console.print(Panel(event.prompt, title="heal prompt sent", border_style="cyan"))
    console.print(f"status: [bold]{event.status}[/bold] {event.notes}")


@app.command(name="approve")
def approve_cmd(
    target: Annotated[str, typer.Argument(help="Target whose heal is awaiting approval")],
    config: ConfigOpt = Path("targets.toml"),
    db: DbOpt = DEFAULT_DB_PATH,
    reject: Annotated[bool, typer.Option("--reject", help="Discard the proposed fix")] = False,
) -> None:
    """Approve (or reject) a heal that stopped at the approval gate."""
    cfg = _load(config)
    item = cfg.target(target)
    store = Store(db)
    client = BrightDataClient()

    if not item.collector_id:
        console.print(f"[red]{target} has no collector_id[/red]")
        raise typer.Exit(1)

    outcome = client.approve(item.collector_id, reject=reject, url=item.url)
    console.print(f"{'rejected' if reject else 'approved'}: [bold]{outcome.status}[/bold]")
    if reject or not outcome.succeeded:
        return

    events = [
        e
        for e in store.recent_heal_events(limit=20)
        if e.target == target and e.status is HealStatus.AWAITING_APPROVAL
    ]
    if not events:
        console.print("[yellow]no pending heal event to verify[/yellow]")
        return
    event, snapshot = healmod.verify(client, item, events[0])
    store.save_heal_event(event)
    if snapshot is not None:
        store.save_snapshot(snapshot, healthy=event.status == "verified")
    console.print(f"verification: [bold]{event.status}[/bold] {event.notes}")


@app.command(name="diff")
def diff_cmd(
    target: Annotated[str | None, typer.Argument(help="Only show this target")] = None,
    db: DbOpt = DEFAULT_DB_PATH,
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many diffs to show")] = 5,
) -> None:
    """Show what changed on recent collections."""
    store = Store(db)
    diffs = [d for d in store.recent_diffs(limit=limit * 4, target=target) if not d.is_empty]

    if not diffs:
        console.print("[dim]no changes recorded yet[/dim]")
        return

    for entry in diffs[:limit]:
        table = Table(
            title=f"{entry.target} — {entry.computed_at:%Y-%m-%d %H:%M}", header_style="bold"
        )
        table.add_column("change")
        table.add_column("item")
        table.add_column("detail")
        for item in entry.entries[:25]:
            label = {"added": "[green]new[/green]", "changed": "[yellow]changed[/yellow]"}.get(
                item.kind, "[red]removed[/red]"
            )
            headline = str(item.row.get("title") or item.row.get("version") or item.key)
            table.add_row(label, headline[:60], ", ".join(item.fields_changed)[:40])
        console.print(table)


@app.command(name="report")
def report_cmd(
    config: ConfigOpt = Path("targets.toml"),
    db: DbOpt = DEFAULT_DB_PATH,
    output: Annotated[Path, typer.Option("--output", "-o", help="Where to write the HTML")] = Path(
        "site/index.html"
    ),
) -> None:
    """Render the dashboard: change feed plus the self-healing timeline."""
    cfg = _load(config)
    store = Store(db)
    path = reportmod.render(cfg, store, output)
    console.print(f"[green]wrote {path}[/green]")


def _render_results(results: list[PullResult]) -> None:
    table = Table(title="Pull", header_style="bold")
    table.add_column("target")
    table.add_column("rows", justify="right")
    table.add_column("health")
    table.add_column("changes", justify="right")
    table.add_column("note")

    for result in results:
        if result.error:
            table.add_row(result.target, "-", "[red]error[/red]", "-", result.error[:60])
            continue
        report = result.report
        assert report is not None
        status_text = report.status.value
        style = _HEALTH_STYLE[report.status]
        note = ""
        if result.heal_event:
            note = f"heal {result.heal_event.status}"
            if result.healed:
                status_text = "healed"
                style = "bold cyan"
        table.add_row(
            result.target,
            str(report.row_count),
            f"[{style}]{status_text}[/{style}]",
            str(len(result.diff.entries)) if result.diff else "-",
            note,
        )
    console.print(table)


def _render_report(report: HealthReport) -> None:
    style = _HEALTH_STYLE[report.status]
    console.print(f"health: [{style}]{report.status.value}[/{style}] ({report.row_count} rows)")
    for violation in report.violations:
        marker = "[red]fatal[/red]" if violation.fatal else "[yellow]drift[/yellow]"
        console.print(f"  {marker} {violation.symptom}")


if __name__ == "__main__":
    app()
