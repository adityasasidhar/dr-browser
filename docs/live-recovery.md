# Live collection evidence

Status recorded 2026-08-23. This file separates actual Bright Data output from
the synthetic dashboard preview. It does **not** claim a verified recovery.

## Fresh collection results

The live command used was `uv run dr-browser pull --no-heal`. It runs the
collectors and their contracts, but deliberately does not change a Bright Data
scraper. The key was supplied only through `BRIGHTDATA_API_KEY` and is not
stored in this repository.

| Target | Collector | Fresh rows | Contract result | Evidence |
|---|---|---:|---|---|
| `langchain` | `c_msyjolqk19z8yfpnrl` | 45 | passed | All four field contracts hold. |
| `anthropic-api` | `c_msyjv15k2227gketj4` | 133 | drift detected | `title` is distinct from `description` on only 33% of populated rows; the contract requires 75%. |
| `runpod-docs` | `c_msyjymc32eaagvcljn` | 60 | broken | `entry_title`, `publication_date`, and `summary_text` are empty in the returned rows. |
| `ollama` | `c_msyk228w2dwlrr0q17` | — | pending refresh | Its retained 2026-08-18 snapshot passed with 56 rows; this run did not persist a new snapshot. |

## What this proves

- The collector IDs are provisioned and live Bright Data output is entering the
  same SQLite history used by the dashboard.
- The contract check catches both subtle selector collapse (Anthropic’s
  populated-but-duplicated fields) and total field disappearance (RunPod).
- Broken snapshots are retained as evidence but marked untrusted, so they do
  not become the baseline for the change feed.

## What it does not claim

No live repair has yet been verified. A verified recovery requires a separate
approved heal followed by fresh output that passes the exact contract that
flagged it. Until then, the synthetic preview remains visibly labelled as demo
data rather than being presented as production proof.
