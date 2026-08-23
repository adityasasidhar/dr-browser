# AGENTS.md

Dr Browser is a self-healing changelog intelligence pipeline built on Bright
Data Scraper Studio for the Scrape-Verse hackathon. The defining idea: every
target declares a **contract** (how often each field must be populated and what
its values should look like), so when a site changes, the pipeline detects the
drift itself and phrases the violation as an English symptom that goes straight
into `bdata scraper heal`. A heal only counts as verified once fresh output
passes the same contract that flagged it.

Read `README.md` for the architecture. The one thing worth knowing before
changing code: `pipeline.pull()` is the whole product in one function — every
other module is a seam it composes.

```bash
uv sync                                  # install (uv is the convention here)
uv run pytest                            # 90 tests, no network or credentials needed
uv run pytest tests/test_pipeline.py::test_break_is_detected_healed_and_verified_in_one_pull
uv run ruff check . --fix && uv run ruff format .
uv run mypy src/scrapverse               # strict mode, must stay clean

uv run dr-browser init                   # write targets.toml
uv run dr-browser provision              # bdata scraper create -> collector IDs (5-10 min each)
uv run dr-browser pull                   # run + check + heal + diff
uv run dr-browser report -o site/index.html

# Preview the dashboard without real collections (writes SYNTHETIC data):
uv run python scripts/seed_demo.py --db data/demo.db
```

Bright Data auth: `bdata login` for local work (interactive, opens a browser),
`BRIGHTDATA_API_KEY` for CI. Never commit a key or paste one into a file here.

Tests never touch the network. `tests/conftest.py` injects a fake
`CommandRunner` into the real `BrightDataClient`, so argv construction, JSON
envelope parsing, and the heal state machine are all exercised for real — only
the subprocess is faked. Keep it that way: a test that needs credentials is a
test that will not run.
