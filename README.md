# CodeMender Fleet Scanner POC

A lightweight, automated fleet scanner for scanning dozens or hundreds of GitHub repositories in parallel using GitHub Actions matrix jobs, rolling up per-repo SARIF findings into a unified executive report and advancing state to avoid redundant rescans.

---

## ⚠️ Critical Scoping & Architectural Invariants

- **Discovery Scan-Only (`cm find .`)**: This scanner executes discovery only. There is **NO** `cm verify`, **NO** `cm fix`, **NO** patch generation, **NO** pull requests, and **NO** branch manipulation.
- **Zero Execution of Target Code**: Target repositories are cloned strictly read-only. Nothing inside target repositories is ever built or executed. No sandboxing of untrusted builds is required or permitted.
- **Zero Changes to Target Repos**: Target repositories require no workflows, no tokens, and no modifications. All work happens centrally in this scanner repo.
- **Whole-Codebase Parallelism**: Parallelism is across **repositories** (N repositories scanned concurrently via GitHub Actions matrix jobs), **never** across directories within a single repository. `cm find` runs as a whole-codebase pass to prevent cross-file taint-flow blind spots and silent false negatives.
- **Cost Control via Cursor Tracking**: `cm find` is backed by Gemini 2.5 Flash and bills per run. `state/cursor.json` tracks each repository's scanned HEAD commit SHA. When `--skip-unchanged` is active, repositories without new commits are skipped before running scans.
- **CLI Contract Verification**: `cm find . --format sarif -o <output.sarif>` is an assumed CLI contract. **This contract must be explicitly verified against the actual CodeMender binary at Rung 3.**

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
│   ├── fake_cm.sh                # Deterministic offline mock for CodeMender CLI
│   └── local_run.sh              # Local zero-cost end-to-end runner
└── README.md
```

---

## Prerequisites & Setup

### GitHub Configuration

Configure the following in the scanner repository (**Settings > Secrets and variables > Actions**):

#### Repository Variables (`vars`)
- `GH_USER`: Target GitHub username (personal account) to enumerate by default.
- `CODEMENDER_RUNNER_IMAGE`: *(Optional)* Docker runner container image containing the `cm` CLI (default: `ghcr.io/codemender/runner:latest`).

#### Repository Secrets (`secrets`)
- `FLEET_READ_TOKEN`: A GitHub Personal Access Token (PAT) with `read:org` and `repo` (or public repo) read permissions used to enumerate and shallow-clone target repositories. Falls back to `GITHUB_TOKEN` if omitted.
- `GCP_WORKLOAD_IDENTITY_PROVIDER`: Workload Identity Federation provider URI for GCP authentication (e.g., `projects/123456/locations/global/workloadIdentityPools/github-pool/providers/github-provider`).
- `GCP_SERVICE_ACCOUNT`: Service account email on Google Cloud authorized for Gemini/Vertex AI invocations.

---

## Staged Bring-Up Ladder

To prevent wasted spend and catch configuration bugs early, follow this progressive verification ladder:

### Rung 0: Rollup Offline against SARIF Fixtures ($0)
Verify SARIF parsing, suppression filtering, CWE tallying, and cursor persistence using the provided test fixtures.

```bash
# Run automated test suite
pytest -v
```

### Rung 1: Enumerate with Real PAT ($0)
Verify read-only GitHub API enumeration, rate-limiting, and cursor skipping against your account without executing scans.

```bash
# Test enumeration dry-run
export GITHUB_TOKEN="<your-pat>"
python3 fleet_enumerate.py --user <your-github-username> --limit 3

# Test cursor skipping by adding a repo's HEAD SHA to state/cursor.json
python3 fleet_enumerate.py --user <your-github-username> --skip-unchanged --limit 3
```

Confirm that repositories matching the recorded `last_sha` log:
`Skipping <owner/repo>: unchanged at <sha>` and report `skipped_unchanged=N`.

### Rung 2: Local End-to-End Pipeline with Fake CLI ($0)
Execute the complete multi-repo clone, scan, and rollup pipeline locally using the deterministic `fake_cm.sh` stub.

```bash
./tools/local_run.sh --user <your-github-username> --limit 2
```

Inspect the generated outputs:
- `fleet_report.md` (Markdown summary table)
- `fleet_report.json` (Structured metrics and per-repo breakdowns)
- `state/cursor.json` (Updated commit SHAs)

### Rung 3: Verify Real `cm find` Contract
> **CRITICAL CHECK**: Verify the exact CLI flags, authentication mechanism, and SARIF output structure on a single small test repository before deploying fleet-wide CI.

Inside your runner container or environment with `cm` installed:
```bash
git clone --depth 1 https://github.com/<your-account>/<small-test-repo>.git /tmp/test-repo
cd /tmp/test-repo
cp <path-to>/.codemender.yaml .

# Confirm cm CLI flags match: cm find . --format sarif -o report.sarif
cm find . --format sarif -o report.sarif

# Verify report.sarif is valid SARIF JSON 2.1.0
jq '.runs[0].results | length' report.sarif
```

### Rung 4: CI via `workflow_dispatch` (Small Pilot)
Trigger `.github/workflows/fleet_scan.yml` manually via GitHub Actions UI:
- **`limit`**: `2`
- **`max_parallel`**: `2`

Verify:
1. `enumerate` job outputs matrix with 2 targets.
2. `scan` matrix executes concurrently across 2 jobs.
3. GCP Workload Identity Federation authenticates successfully.
4. `cm find` runs with credentials scrubbed (`unset GITHUB_TOKEN`, etc.).
5. `rollup` job downloads artifacts, generates report summary in `$GITHUB_STEP_SUMMARY`, and commits `state/cursor.json`.

### Rung 5: Fleet Production & Re-run Validation
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

Run all unit and integration tests using pytest:

```bash
pytest -v
```
