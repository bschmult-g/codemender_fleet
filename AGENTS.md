# AGENTS.md

Context file for AI coding assistants and developers working on `codemender-fleet-scanner`.

## System Overview

CodeMender Fleet Scanner is an automated vulnerability discovery engine designed to audit multiple GitHub repositories in parallel. It uses the real Google CodeMender CLI (`cm find`), shallow-clones repositories into isolated ephemeral workspaces, stamps SARIF reports with repository metadata, aggregates findings into executive Markdown and JSON summaries, and tracks commit SHAs in a state cursor to prevent redundant scans.

## Essential Commands

### Testing
Run unit tests across enumeration, filtering, suppression handling, and rollup:
```bash
pytest -v
```

### Discovery & Enumeration
Discover candidate repositories via GitHub API (read-only):
```bash
# Enumerate repos for a user
python3 fleet_enumerate.py --user bschmult-g --limit 3

# Enumerate an organization and skip repos matching cursor.json
python3 fleet_enumerate.py --org cloud-gtm --skip-unchanged --limit 10
```

### Local Parallel Scanning
Execute parallel scans locally using real CodeMender CLI:
```bash
./tools/local_run.sh --user bschmult-g --limit 3 --parallel 3
```

### SARIF Rollup & Reporting
Roll up collected SARIF files into unified reports:
```bash
python3 fleet_rollup.py --in sarifs --cursor state/cursor.json
```

## Architecture & Code Map

* [`fleet_enumerate.py`](file:///Users/bschmult/.gemini/jetski/scratch/codemender-fleet-scanner/fleet_enumerate.py) — Paginates GitHub API (`/users/{login}/repos` or `/orgs/{name}/repos`). Filters archived, forks, disabled, small repos (`--min-kb`), and unchanged SHAs (`--skip-unchanged`). Auto-detects token from `gh auth token`.
* [`fleet_rollup.py`](file:///Users/bschmult/.gemini/jetski/scratch/codemender-fleet-scanner/fleet_rollup.py) — Pure Python standard library parser. Scans `sarifs/**/*.sarif`, suppresses false positives/suppressed findings, tallies CWEs and severity levels, outputs `fleet_report.md` and `fleet_report.json`, and updates `state/cursor.json`.
* [`tools/local_run.sh`](file:///Users/bschmult/.gemini/jetski/scratch/codemender-fleet-scanner/tools/local_run.sh) — Multi-threaded local orchestrator using `concurrent.futures.ThreadPoolExecutor`. Creates isolated `$HOME` per target repo, passes `--unrestricted`, and preserves `scan.log`.
* [`.github/workflows/fleet_scan.yml`](file:///Users/bschmult/.gemini/jetski/scratch/codemender-fleet-scanner/.github/workflows/fleet_scan.yml) — Production 3-job GitHub Actions pipeline (`enumerate` -> `scan` matrix -> `rollup`). Uses Google Cloud Workload Identity Federation and scrubs credentials before analysis.
* [`.codemender.yaml`](file:///Users/bschmult/.gemini/jetski/scratch/codemender-fleet-scanner/.codemender.yaml) — Global scan configuration copied into cloned target repos.
* [`state/cursor.json`](file:///Users/bschmult/.gemini/jetski/scratch/codemender-fleet-scanner/state/cursor.json) — JSON tracking database mapping repository full names to last scanned commit SHA and timestamp.

## Critical Invariants

1. **Discovery Scan-Only**: Run `cm find` only. Do not invoke `cm verify`, `cm fix`, or attempt pull request generation.
2. **Real Findings Only**: Never use mock fallbacks (`fake_cm.sh`) for live scans. Tests use fixtures in `tests/fixtures/`.
3. **Zero Target Changes**: Target code is cloned read-only into temporary directories and destroyed immediately after report extraction.
4. **Whole-Codebase Parallelism**: Parallelize across distinct repositories. Never split directories within a single repository to avoid breaking cross-file taint analysis.

## Known CLI Gotchas & Solutions

* **`cm find` CLI Contract**: `cm find` does not support `-o` or `--format`. The verified pattern is:
  ```bash
  cm find -y --bypass-warning --unrestricted .
  cm report -f sarif > report.sarif
  ```
* **Filesystem Sandbox Boundary**: CodeMender's agent defaults to `project_paths` in `~/.codemender/config.yaml`. When scanning in temporary directories, pass `--unrestricted` to allow the agent to inspect the cloned target codebase.
* **Isolated `$HOME` Authentication**: When redirecting `$HOME` for SQLite database isolation, symlink `~/.config` and export `GOOGLE_APPLICATION_CREDENTIALS` so Google Cloud ADC remains valid.
* **SARIF Namespacing**: Always inject `_fleet: {repo, sha}` into `report.sarif` before saving to `sarifs/` to prevent rule ID collision across repositories.
