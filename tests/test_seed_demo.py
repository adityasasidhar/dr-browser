"""The synthetic demo must obey the same contracts the product enforces."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scrapverse.config import load_config
from scrapverse.health import check
from scrapverse.models import HealStatus
from scrapverse.store import Store


def test_seed_demo_renders_every_configured_target_and_verified_recovery(tmp_path: Path) -> None:
    """The standalone preview command produces a trustworthy synthetic dashboard."""
    root = Path(__file__).parents[1]
    config_path = root / "targets.toml"
    database = tmp_path / "demo.db"
    dashboard = tmp_path / "site" / "index.html"

    completed = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "seed_demo.py"),
            "--config",
            str(config_path),
            "--db",
            str(database),
            "--output",
            str(dashboard),
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )

    config = load_config(config_path)
    html = dashboard.read_text()
    store = Store(database)

    assert "SYNTHETIC data" in completed.stdout
    assert dashboard.is_file()
    assert "SYNTHETIC DEMO DATA" in html
    assert all(target.name in html for target in config.targets)
    for target in config.targets:
        snapshot = store.latest_snapshot(target.name)
        assert snapshot is not None
        assert not check(snapshot, target).needs_heal

    verified = next(
        event for event in store.recent_heal_events() if event.status is HealStatus.VERIFIED
    )
    assert verified.target == "runpod-docs"
    assert any(violation.field == "publication_date" for violation in verified.violations)
