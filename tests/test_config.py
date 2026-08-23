"""Config loading and collector-ID write-back."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from scrapverse.config import Config, Target, load_config, save_collector_id
from scrapverse.report import STARTER_CONFIG

SAMPLE = """
[[targets]]
name = "alpha"
url = "https://a.test"
description = "extract stuff"
collector_id = ""

  [[targets.fields]]
  name = "title"

# a comment that must survive a rewrite
[[targets]]
name = "beta"
url = "https://b.test"
description = "extract other stuff"

  [[targets.fields]]
  name = "headline"
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "targets.toml"
    path.write_text(SAMPLE)
    return path


def test_starter_config_parses_and_every_target_has_a_contract(tmp_path: Path) -> None:
    path = tmp_path / "targets.toml"
    path.write_text(STARTER_CONFIG)
    config = load_config(path)

    assert len(config.targets) == 4
    assert all(t.fields for t in config.targets)
    assert all(t.url.startswith("https://") for t in config.targets)
    assert not config.provisioned  # ships unprovisioned by design


def test_key_fields_default_to_the_first_declared_field(config_file: Path) -> None:
    assert load_config(config_file).target("beta").key_fields == ["headline"]


def test_missing_config_points_at_init(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="dr-browser init"):
        load_config(tmp_path / "nope.toml")


def test_unknown_target_lists_the_known_ones(config_file: Path) -> None:
    with pytest.raises(KeyError, match="alpha, beta"):
        load_config(config_file).target("gamma")


def test_collector_id_is_written_into_an_existing_empty_field(config_file: Path) -> None:
    save_collector_id(config_file, "alpha", "c_aaa")
    config = load_config(config_file)
    assert config.target("alpha").collector_id == "c_aaa"
    assert config.target("beta").collector_id is None


def test_collector_id_is_inserted_when_the_field_is_absent(config_file: Path) -> None:
    save_collector_id(config_file, "beta", "c_bbb")
    assert load_config(config_file).target("beta").collector_id == "c_bbb"


def test_write_back_preserves_comments(config_file: Path) -> None:
    """`targets.toml` is hand-edited, so provisioning must not reformat it."""
    save_collector_id(config_file, "alpha", "c_aaa")
    assert "# a comment that must survive a rewrite" in config_file.read_text()


def test_write_back_rejects_an_unknown_target(config_file: Path) -> None:
    with pytest.raises(KeyError):
        save_collector_id(config_file, "gamma", "c_ccc")


def test_invalid_pattern_fails_at_load_not_mid_run() -> None:
    with pytest.raises(ValidationError, match="invalid pattern"):
        Config.model_validate(
            {
                "targets": [
                    {
                        "name": "x",
                        "url": "https://x.test",
                        "description": "d",
                        "fields": [{"name": "f", "pattern": "([unclosed"}],
                    }
                ]
            }
        )


def test_provisioned_filters_targets_without_a_collector() -> None:
    config = Config(
        targets=[
            Target(name="a", url="https://a.test", description="d", collector_id="c_1"),
            Target(name="b", url="https://b.test", description="d"),
        ]
    )
    assert [t.name for t in config.provisioned] == ["a"]
