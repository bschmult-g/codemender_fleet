# CodeMender Fleet Scanner POC

A lightweight, automated fleet scanner for scanning dozens or hundreds of GitHub repositories in parallel using GitHub Actions matrix jobs, rolling up per-repo SARIF findings into a unified executive report and advancing state to avoid redundant rescans.

---

## ⚠️ Critical Scoping & Architectural Invariants

- **Discovery Scan-Only (`cm find .`)**: This scanner executes discovery only. There is **NO** `cm verify`, **NO** `cm fix`, **NO** patch generation, **NO** pull requests, and **NO** branch manipulation.
- **Real CodeMender Analysis (No Fake Fallbacks)**: Scans run using the actual CodeMender CLI (`cm find -y --bypass-warning .` followed by `cm report -f sarif > report.sarif`), generating genuine security findings directly against your code.
- **Zero Execution of Target Code**: Target repositories are cloned strictly read-only. Nothing inside target repositories is ever built or executed. No sandboxing of untrusted builds is required or permitted.
- **Zero Changes to Target Repos**: Target repositories require no workflows, no tokens, and no modifications. All work happens centrally in this scanner repo.
- **Whole-Codebase Parallelism**: Parallelism is across **repositories** (N repositories scanned concurrently via GitHub Actions matrix jobs), **never** across directories within a single repository. `cm find` runs as a whole-codebase pass to prevent cross-file taint-flow blind spots and silent false negatives.
- **Cost Control via Cursor Tracking**: `cm find` is backed by Gemini 2.5 Flash / Gemini 3.8 Flash and bills per run. `state/cursor.json` tracks each repository's scanned HEAD commit SHA. When `--skip-unchanged` is active, repositories without new commits are skipped before running scans.

---

## Repository Structure

```
├── .codemender.yaml              # Global CodeMender config copied to target repos
├── .github/
│   └── workflows/
│       └── fleet_scan.yml        # 3-job workflow: enumerate -> scan -> rollup
├── fleet_enumerate.py            # Target discovery & matrix generation (requests only)
├── fleet_rollup.py               # Aggregation, normalization & cursor advancement (stdlib only)
├── state/
│   └── cursor.json               # Tracks {repo: {last_sha, scanned_at, findings}}
├── tests/
│   ├── fixtures/                 # SARIF fixtures for Rung 0 offline verification
│   │   ├── repo1.sarif           # 3 findings (1 suppressed) -> 2 effective findings
│   │   └── repo2.sarif           # Clean repo (0 findings)
│   ├── test_enumerate.py         # Unit tests for enumeration & filtering
│   └── test_rollup.py            # Unit tests for rollup, suppressions, and cursor
├── tools/
│   ├── fake_cm.sh                # Deterministic offline mock for test fixtures
│   └── local_run.sh              # Live local runner using real CodeMender
└── README.md
```

---

## Prerequisites & Setup

### 1. CodeMender CLI
The real CodeMender CLI (`cm`) must be installed on your system (e.g., at `/Users/bschmult/.gemini/jetski/bin/cm` or on your `PATH`). Verify by running:
```bash
cm --version
```

### 2. GitHub Configuration

Configure the following in the scanner repository (**Settings > Secrets and variables > Actions**):

#### Repository Variables (`vars`)
- `GH_USER`: Target GitHub username (personal account) to enumerate by default.
- `CODEMENDER_RUNNER_IMAGE`: *(Optional)* Docker runner container image containing the `cm` CLI (default: `ghcr.io/codemender/runner:latest`).

#### Repository Secrets (`secrets`)
- `FLEET_READ_TOKEN`: A GitHub Personal Access Token (PAT) with `read:org` and `repo` (or public repo) read permissions used to enumerate and shallow-clone target repositories. Falls back to `GITHUB_TOKEN` if omitted.
- `GCP_WORKLOAD_IDENTITY_PROVIDER`: Workload Identity Federation provider URI for GCP authentication (e.g., `projects/123456/locations/global/workloadIdentityPools/github-pool/providers/github-provider`).
- `GCP_SERVICE_ACCOUNT`: Service account email on Google Cloud authorized for Gemini/Vertex AI invocations.

---

---

## Running Scans

### Asynchronous Parallel Local Runner
Run the real CodeMender scanner locally across your repositories in parallel:

```bash
# Auto-detects GitHub auth and user login from `gh` CLI if available
./tools/local_run.sh --user <your-github-username> --limit 3 --parallel 3
```

Key runner options:
* `--limit <N>`: Maximum number of target repositories to process (default: `3`).
* `-j, --parallel <N>`: Number of concurrent scan worker threads (default: `3`).
* `--skip-unchanged`: Skip repositories whose current commit SHA matches `state/cursor.json`.
* `--include <pattern>`: Target specific repositories (e.g. `--include "MNPI-*"`).

Execution pipeline per target:
1. Enumerates candidate repositories via GitHub API.
2. Shallow-clones (`--depth 1`) each target into an isolated ephemeral workspace.
3. Executes real `cm find -y --bypass-warning --unrestricted .` on the target codebase.
4. Preserves full reasoning traces in `sarifs/<safe_repo_name>/scan.log`.
5. Exports SARIF results via `cm report -f sarif > report.sarif` and injects `_fleet` metadata.
6. Aggregates findings into `fleet_report.md` and `fleet_report.json`, advancing `state/cursor.json`.

---

## Staged Bring-Up Ladder

To prevent wasted spend and catch configuration bugs early, follow this progressive verification ladder:

### Rung 0: Rollup Offline against SARIF Fixtures ($0)
Verify SARIF parsing, suppression filtering, CWE tallying, and cursor persistence using test fixtures:

```bash
pytest -v
```

### Rung 1: Enumerate with Real PAT ($0)
Verify read-only GitHub API enumeration, rate-limiting, and cursor skipping without executing scans:

```bash
# Test enumeration
python3 fleet_enumerate.py --user <your-github-username> --limit 3

# Test cursor skipping (repos matching cursor.json are bypassed)
python3 fleet_enumerate.py --user <your-github-username> --skip-unchanged --limit 3
```

### Rung 2: Live Local Scan on One Small Repo
Run a real CodeMender scan on a single target to verify findings extraction and report generation:

```bash
./tools/local_run.sh --user <your-github-username> --limit 1 --parallel 1
```

### Rung 3: CI via `workflow_dispatch` (Small Pilot)
Trigger `.github/workflows/fleet_scan.yml` manually via GitHub Actions UI:
* **`limit`**: `2`
* **`max_parallel`**: `2`

Verify:
1. `enumerate` job outputs matrix with 2 targets.
2. `scan` matrix executes concurrently across 2 jobs.
3. Google Cloud Workload Identity Federation authenticates successfully.
4. Real `cm find` runs with credentials scrubbed from the environment.
5. `rollup` job generates report summary in `$GITHUB_STEP_SUMMARY` and commits `state/cursor.json`.

### Rung 4: Fleet Production & Re-run Validation
Widen the run parameters:
* **`limit`**: `25`
* **`max_parallel`**: `10`

Trigger an immediate second run with identical inputs:
* Confirm that `enumerate` detects all 25 repos as unchanged (`skipped_unchanged=25`).
* Confirm that `scan` is skipped cleanly (`count == 0`), preventing redundant LLM token spend.

---

## Common Pitfalls & Troubleshooting

### 1. `cm find: unknown shorthand flag: 'o' in -o`
* **Cause**: `cm find` performs discovery and does not accept output format flags directly.
* **Fix**: Run the two-stage sequence:
  ```bash
  cm find -y --bypass-warning --unrestricted .
  cm report -f sarif > report.sarif
  ```

### 2. Zero findings on known vulnerable repositories
* **Cause**: CodeMender's agent filesystem sandbox is restricted by `project_paths` in `~/.codemender/config.yaml`. When target code is cloned into `/private/var/folders/`, the agent is blocked from viewing target files.
* **Fix**: Pass `--unrestricted` to `cm find` and ensure isolated worker configuration sets `project_paths` to the cloned directory.

### 3. `No valid Application Default Credentials found`
* **Cause**: Isolating `$HOME` for parallel workers hides the user's `~/.config/gcloud` credentials.
* **Fix**: Symlink `~/.config` into the worker's isolated home or export `GOOGLE_APPLICATION_CREDENTIALS` pointing to your active ADC JSON file.

### 4. Git history bloat during cloning
* **Cause**: Standard `git clone` downloads entire commit logs dating back years.
* **Fix**: Use shallow clones (`git clone --depth 1`). This provides 100% of the files at HEAD while discarding past history.

---

## Architecture Decision Records (ADRs)

Key architectural decisions are documented in `docs/adr/`:
* [ADR-0001: Parallelize Across Repositories, Never Subdirectories](file:///Users/bschmult/.gemini/jetski/scratch/codemender-fleet-scanner/docs/adr/0001-whole-codebase-parallelism.md)
* [ADR-0002: Real CodeMender CLI Contract and Sandbox Boundary Management](file:///Users/bschmult/.gemini/jetski/scratch/codemender-fleet-scanner/docs/adr/0002-real-cm-contract-and-sandbox-isolation.md)

Developer and AI coding assistant guidelines are maintained in [AGENTS.md](file:///Users/bschmult/.gemini/jetski/scratch/codemender-fleet-scanner/AGENTS.md).

---

## CLI Reference

### `fleet_enumerate.py`

```
options:
  --user USER           GitHub user login (personal account)
  --org ORG             GitHub organization name
  --min-kb MIN_KB       Minimum repository size in KB to consider (default: 64)
  --include PAT [PAT]   fnmatch globs of repo names to include
  --exclude PAT [PAT]   fnmatch globs to exclude (default: .github, *-archive, *-sandbox, *-docs)
  --skip-unchanged      Skip repos whose HEAD SHA matches state/cursor.json
  --cursor CURSOR       Path to cursor file (default: state/cursor.json)
  --limit LIMIT         Safety cap on targets (0 = unlimited up to 256; default: 3)
```

### `fleet_rollup.py`

```
options:
  --in IN_DIR           Input directory containing SARIF files (default: sarifs)
  --cursor CURSOR       Path to cursor state file (default: state/cursor.json)
```

---

## Testing

Run unit and integration tests using pytest:

```bash
pytest -v
```
