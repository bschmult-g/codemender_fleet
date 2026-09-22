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

## Running Scans

### Live Local Scan across Your Repositories
Run the real CodeMender scanner locally against your repositories:

```bash
# Set your GitHub PAT for repository cloning
export GITHUB_TOKEN="<your-pat>"

# Run live scan across your repositories (capped at 2 repos for initial run)
./tools/local_run.sh --user <your-github-username> --limit 2
```

This will:
1. Enumerate your repositories via GitHub API.
2. Shallow-clone each target into an isolated workspace.
3. Execute real `cm find -y --bypass-warning .` on the codebase.
4. Export real SARIF results via `cm report -f sarif > report.sarif`.
5. Roll up findings into `fleet_report.md`, `fleet_report.json`, and update `state/cursor.json`.

---

## Staged Bring-Up Ladder

To prevent wasted spend and catch configuration bugs early, follow this progressive verification ladder:

### Rung 0: Rollup Offline against SARIF Fixtures ($0)
Verify SARIF parsing, suppression filtering, CWE tallying, and cursor persistence using the provided test fixtures.

```bash
pytest -v
```

### Rung 1: Enumerate with Real PAT ($0)
Verify read-only GitHub API enumeration, rate-limiting, and cursor skipping against your account without executing scans.

```bash
export GITHUB_TOKEN="<your-pat>"
python3 fleet_enumerate.py --user <your-github-username> --limit 3

# Test cursor skipping by confirming repos in cursor.json are skipped
python3 fleet_enumerate.py --user <your-github-username> --skip-unchanged --limit 3
```

### Rung 2: Live Local Scan on One Small Repo
Run a real CodeMender scan on a single small repository to confirm findings and output generation:

```bash
./tools/local_run.sh --user <your-github-username> --limit 1
```

### Rung 3: CI via `workflow_dispatch` (Small Pilot)
Trigger `.github/workflows/fleet_scan.yml` manually via GitHub Actions UI:
- **`limit`**: `2`
- **`max_parallel`**: `2`

Verify:
1. `enumerate` job outputs matrix with 2 targets.
2. `scan` matrix executes concurrently across 2 jobs.
3. GCP Workload Identity Federation authenticates successfully.
4. Real `cm find` and `cm report -f sarif` run with credentials scrubbed.
5. `rollup` job downloads artifacts, generates report summary in `$GITHUB_STEP_SUMMARY`, and commits `state/cursor.json`.

### Rung 4: Fleet Production & Re-run Validation
Widen the run parameters:
- **`limit`**: `25`
- **`max_parallel`**: `10`

Once finished, immediately trigger a second run with the same inputs:
- Confirm that `enumerate` detects all 25 repos as unchanged (`skipped_unchanged=25`).
- Confirm that the `scan` job is skipped cleanly (`count == 0`), preventing redundant LLM token spend.

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
