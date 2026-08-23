"""Target configuration: what to scrape, and what its output must look like.

Every target carries a *contract* — the shape its rows are expected to have.
The contract is what turns "the site changed" from something a human notices
into something the pipeline detects on its own, so it is configuration rather
than code.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, Field, model_validator

DEFAULT_CONFIG_PATH = Path("targets.toml")


class FieldContract(BaseModel):
    """Expectations for one extracted field."""

    name: str
    required: bool = True
    min_fill_rate: float = Field(default=0.9, ge=0.0, le=1.0)
    pattern: str | None = None
    # Field names whose values this one must not duplicate. Catches two
    # selectors collapsing onto the same element, which leaves both fields
    # populated and therefore invisible to fill-rate and pattern checks.
    distinct_from: list[str] = Field(default_factory=list)
    description: str = ""

    @model_validator(mode="after")
    def _compile_pattern(self) -> Self:
        """Fail fast on a bad config rather than mid-collection."""
        if self.pattern is not None:
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(
                    f"field {self.name!r} has an invalid pattern {self.pattern!r}: {exc}"
                ) from exc
        return self

    def matches(self, value: Any) -> bool:
        """Whether a value satisfies this field's pattern (if it has one)."""
        if self.pattern is None:
            return True
        return re.search(self.pattern, str(value)) is not None


class Target(BaseModel):
    """One site the pipeline watches."""

    name: str
    url: str
    description: str
    collector_id: str | None = None
    # Scraper Studio often wraps the real records in a per-page envelope
    # (e.g. `{"changelog_entries": [...]}`); name that key here so the rows are
    # unwrapped before the contract is checked against them.
    records_path: str | None = None
    key_fields: list[str] = Field(default_factory=list)
    min_rows: int = 1
    fields: list[FieldContract] = Field(default_factory=list)

    @model_validator(mode="after")
    def _default_key_fields(self) -> Self:
        if not self.key_fields and self.fields:
            self.key_fields = [self.fields[0].name]
        return self

    @property
    def is_provisioned(self) -> bool:
        """A target without a collector ID has not been created upstream yet."""
        return bool(self.collector_id)

    def field(self, name: str) -> FieldContract | None:
        return next((f for f in self.fields if f.name == name), None)


class Config(BaseModel):
    """The full target list."""

    targets: list[Target] = Field(default_factory=list)

    def target(self, name: str) -> Target:
        found = next((t for t in self.targets if t.name == name), None)
        if found is None:
            known = ", ".join(t.name for t in self.targets) or "none configured"
            raise KeyError(f"unknown target {name!r} (known: {known})")
        return found

    @property
    def provisioned(self) -> list[Target]:
        return [t for t in self.targets if t.is_provisioned]


def load_config(path: Path | None = None) -> Config:
    """Read `targets.toml`, or raise with a pointer to `dr-browser init`."""
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — run `dr-browser init` to write a starter config"
        )
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    return Config.model_validate(raw)


def save_collector_id(path: Path, target_name: str, collector_id: str) -> None:
    """Write a newly created collector ID back into the config.

    Edits lines in place rather than re-serializing, so hand-written comments
    and ordering in `targets.toml` survive provisioning. The value must land
    inside the target's own table — anything after the first `[[targets.fields]]`
    sub-table would silently attach to the wrong table.
    """
    lines = path.read_text().splitlines()
    start, end = _target_block(lines, target_name)
    body = lines[start:end]

    # Skip body[0], which is the `[[targets]]` header of this very block.
    for offset, line in enumerate(body[1:], start=1):
        if line.strip().startswith("[["):  # reached a sub-table; stop looking
            break
        if line.strip().startswith("collector_id"):
            lines[start + offset] = f'collector_id = "{collector_id}"'
            path.write_text("\n".join(lines) + "\n")
            return

    name_offset = next(
        i for i, line in enumerate(body) if line.strip() == f'name = "{target_name}"'
    )
    lines.insert(start + name_offset + 1, f'collector_id = "{collector_id}"')
    path.write_text("\n".join(lines) + "\n")


def _target_block(lines: list[str], target_name: str) -> tuple[int, int]:
    """Half-open line range of the `[[targets]]` table declaring `target_name`."""
    starts = [i for i, line in enumerate(lines) if line.strip() == "[[targets]]"]
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(lines)
        block = lines[start:end]
        if any(line.strip() == f'name = "{target_name}"' for line in block):
            return start, end
    raise KeyError(f"target {target_name!r} not found in config")
