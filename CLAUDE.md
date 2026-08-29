# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Agent Instructions

This repo is the ETL/pipeline layer for civic monitoring. It scrapes meeting agendas, transcribes video, extracts structured data (JSONL), and manages the transcription backlog. It does NOT generate prose analysis or advocacy intelligence — that lives in `yimby.analysis`.

## Architecture

**Split-mode pipeline:** AWS Lambda scrapes on schedule → writes S3 marker → local machine polls for marker via `extract-watch` → runs `claude -p` extraction via `extract-local`.

**Config:** Jurisdiction-specific settings (city name, council roster, feeds, advocacy lens) live in AWS SSM Parameter Store. Local dev uses `config.local.yaml` fallback. `agencies.yaml` is the structural scraper registry (platforms, URLs, bodies). See `config.py` for the loader.

**Key files:**
- `agencies.yaml` — agency registry (platform, base_url, enabled, bodies, lookback)
- `config.py` — SSM/YAML config loader (cached, ~50 lines)
- `config.local.yaml` — local dev config (gitignored)
- `lambda_handler.py` — Lambda entry point, dispatches by platform
- `extract-watch` — cron-driven S3 marker poller
- `extract-local` — runs `claude -p` extraction, syncs results to S3
- `civic-pipeline` — legacy local orchestrator (still works for manual runs)
- `civic_utils.py` — shared utilities (PDF extraction, text processing, agency helpers)
- `lib/storage.py` — storage abstraction (local filesystem or S3 via `WATCHDOG_S3_BUCKET`)

## Common Commands

```bash
# Activate venv (required for all commands)
source .venv/bin/activate

# Run full pipeline locally (legacy orchestrator)
./civic-pipeline local --deep --years 1

# Run pipeline with extraction cap (ALWAYS use --extract-until for manual runs)
./civic-pipeline local --extract-until 20

# Fetch building permits (incremental, current year)
python scrapers/etrakit.py fetch
python scrapers/etrakit.py fetch --year 2025    # specific year
python scrapers/etrakit.py fetch --full          # full rescan

# Fetch planning projects (development plans, density bonus, CUPs, etc)
python scrapers/etrakit.py projects
python scrapers/etrakit.py projects --year 2025

# Run extraction on new documents
python transforms/extract_structured.py

# Merge documents into meetings, then roll up
python transforms/meeting_merge.py
python transforms/monthly_rollup.py

# Build unified housing projects (cross-references all sources)
python transforms/housing_projects.py          # build if stale
python transforms/housing_projects.py --stats  # show match rates + top projects

# Pipeline health checks
python pipeline_preflight.py
python pipeline_doctor.py

# Estimate extraction volume for rollout planning
python pipeline_estimate.py                    # tallies since earliest enabled_date
python pipeline_estimate.py --days 14          # last 14 days
python pipeline_estimate.py --csv              # CSV for spreadsheets

# Deploy Lambda (after changing lambda_handler, scrapers, or agencies.yaml)
./deploy-lambda
```

## Data Layout

All data lives in `data/` (gitignored). Structure per agency:
```
data/{agency_slug}/
  documents/          # Raw scraped text (agendas, staff reports)
  doc-index.json      # Document metadata index
  state.json          # Scraper state (last-seen markers)
  permits/            # Building permits + planning projects (eTRAKit JSONL, Oceanside only)
data/structured/      # Extracted JSONL (meetings-combined, all-records)
data/structured/housing-projects.json  # Unified housing projects (cross-ref all sources)
data/exports/         # Parquet files for DuckDB queries
data/transcripts/     # Whisper transcription output
```

Permits and planning projects are standalone structured data (type, description, status, dates) — they do NOT go through `claude -p` extraction. Meeting documents do. Building permits use `etrakit-permits-{year}.jsonl`, planning projects use `etrakit-projects-{year}.jsonl`. `housing_projects.py` cross-references permits, planning projects, HCD APR filings, and meeting extractions into a unified entity per named project.

## Platform Scraper Modules

Each scraper in `scrapers/` implements a platform adapter. `lambda_handler.py` dispatches by the `platform` field in `agencies.yaml`. Supported platforms: `legistar_html`, `legistar_odata`, `escribe`, `civicplus`, `civicclerk`, `granicus`, `swagit`, `carlsbad_cms`, `laserfiche`, `primegov`, custom HTML.

`civic_utils.py` provides shared helpers: `load_agencies()`, `agency_data_dir()`, PDF-to-text, content hashing.

## Related Projects
- `yimby.analysis`: downstream analysis — executive summaries, council member profiles, leadership grading. Reads from this repo's `data/` directory
- `stoside.data`: municipal fiscal intelligence, budget/CIP/vote history

## Knowledge Base Repos (upstream, read-only)

Two external repos own all reusable knowledge — articles for context and Claude Code skills for session-level tooling. Fetch articles from them as needed; skills are handled by the Claude Code system, not by pipeline code.

- **`lacrx/policy-knowledge-docs`** — policy articles and skills. Articles: CA housing law enforcement, PRA strategy, fiscal productivity analysis, crash data methodology. Skills: `draft-pra-request`, `fetch-policy-bundle`, `evaluate-crash-study`.
- **`lacrx/agent-knowledge-docs`** — engineering articles and skills. Articles: AWS deployment, Fargate, SDK patterns, testing. Skills: `scaffold-fastapi`, `provision-fargate-task`, `ecr-push-deploy`, etc.

### Fetching Articles

Both KBs use the same discovery flow. Fetch articles for context during planning and implementation — not skills, which are loaded by Claude Code automatically.

```
gh api repos/lacrx/{repo}/contents/{path}?ref=main -H "Accept: application/vnd.github.raw+json"
```

1. Fetch `QUICK-REF.md` first. Find the row matching the task's topic.
2. Extract the article path from the matched row and fetch it.
3. If no match, fetch `TOPIC-INDEX.md` and retry.
4. If still no match, continue without KB.

### When to Fetch Which

- **Engineering KB** (`agent-knowledge-docs`): infrastructure, deployment, cloud services, testing patterns, frameworks. Fetch once during planning.
- **Policy KB** (`policy-knowledge-docs`): housing law, land use, transit, municipal governance, advocacy, PRA strategy. Fetch **whenever relevant** — during extractions, analysis, drafting, evaluation, or any policy-adjacent work.
- **Skip both**: GIS/spatial analysis, pure data pipeline code that doesn't touch infrastructure or policy.
