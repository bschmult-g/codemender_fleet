#!/usr/bin/env bash
# tools/local_run.sh - Asynchronous, Parallel CodeMender Fleet Scanner Runner.
# Fans out across discovered repositories in parallel using a concurrent worker pool.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REAL_HOME="${REAL_HOME:-$HOME}"

# Ensure real CodeMender binary is on PATH (check standard location /Users/bschmult/.gemini/jetski/bin)
if ! command -v cm &>/dev/null; then
  if [[ -x "/Users/bschmult/.gemini/jetski/bin/cm" ]]; then
    export PATH="/Users/bschmult/.gemini/jetski/bin:$PATH"
  else
    echo "Error: Real CodeMender CLI ('cm') was not found on PATH or at /Users/bschmult/.gemini/jetski/bin/cm." >&2
    echo "This script requires the real CodeMender CLI (no fake fallbacks)." >&2
    exit 1
  fi
fi

CM_BIN="$(command -v cm)"

# Clean up any legacy fake_cm symlink if present
if [[ -L "$SCRIPT_DIR/cm" ]]; then
  rm -f "$SCRIPT_DIR/cm"
fi

USER_ARG=""
ORG_ARG=""
LIMIT_ARG="3"
PARALLEL_ARG="3"
SKIP_UNCHANGED=false
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)
      USER_ARG="$2"
      shift 2
      ;;
    --org)
      ORG_ARG="$2"
      shift 2
      ;;
    --limit)
      LIMIT_ARG="$2"
      shift 2
      ;;
    -j|--parallel|--max-parallel)
      PARALLEL_ARG="$2"
      shift 2
      ;;
    --skip-unchanged)
      SKIP_UNCHANGED=true
      shift
      ;;
    *)
      EXTRA_ARGS+=("$1")
      shift
      ;;
  esac
done

# Auto-detect GITHUB_TOKEN from gh CLI if not set
if [[ -z "${GITHUB_TOKEN:-}" && -z "${FLEET_READ_TOKEN:-}" ]]; then
  if command -v gh &>/dev/null; then
    DETECTED_TOKEN="$(gh auth token 2>/dev/null || true)"
    if [[ -n "$DETECTED_TOKEN" ]]; then
      export GITHUB_TOKEN="$DETECTED_TOKEN"
      echo "Auto-detected GitHub authentication from 'gh' CLI."
    fi
  fi
fi

# Auto-detect user from gh if not set
if [[ -z "$USER_ARG" && -z "$ORG_ARG" && -z "${GH_USER:-}" ]]; then
  if command -v gh &>/dev/null; then
    DETECTED_USER="$(gh api user -q .login 2>/dev/null || true)"
    if [[ -n "$DETECTED_USER" ]]; then
      USER_ARG="$DETECTED_USER"
      echo "Auto-detected target GitHub account from 'gh': $USER_ARG"
    fi
  fi
fi

if [[ -z "$USER_ARG" && -z "$ORG_ARG" ]]; then
  echo "Error: Must specify --user <login> or --org <name> (or set GH_USER)." >&2
  exit 1
fi

ENUM_CMD=(python3 "$REPO_ROOT/fleet_enumerate.py" --limit "$LIMIT_ARG")
if [[ -n "$USER_ARG" ]]; then
  ENUM_CMD+=(--user "$USER_ARG")
elif [[ -n "$ORG_ARG" ]]; then
  ENUM_CMD+=(--org "$ORG_ARG")
fi

if [[ "$SKIP_UNCHANGED" == true ]]; then
  ENUM_CMD+=(--skip-unchanged)
fi

if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
  ENUM_CMD+=("${EXTRA_ARGS[@]}")
fi

echo "=========================================================="
echo " CodeMender Fleet Scanner - Asynchronous Parallel Runner"
echo "=========================================================="
echo "Using CodeMender binary: $CM_BIN"
echo "Target Concurrency:      $PARALLEL_ARG parallel workers"

echo ""
echo "[Step 1/3] Enumerating repositories..."
MATRIX_JSON=$("${ENUM_CMD[@]}")

# Validate if we have any targets
NUM_TARGETS=$(python3 -c "import json, sys; items = json.loads(sys.argv[1]); print(len(items))" "$MATRIX_JSON")
echo "Found $NUM_TARGETS target repositories to scan."

if [[ "$NUM_TARGETS" -eq 0 ]]; then
  echo "No repositories to scan. Exiting cleanly."
  exit 0
fi

SARIFS_DIR="$REPO_ROOT/sarifs"
mkdir -p "$SARIFS_DIR"

echo ""
echo "[Step 2/3] Fanning out scans asynchronously ($PARALLEL_ARG workers in parallel)..."

python3 - <<PYEOF
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

targets = json.loads('''$MATRIX_JSON''')
repo_root = "$REPO_ROOT"
sarifs_dir = "$SARIFS_DIR"
real_home = "$REAL_HOME"
cm_bin = "$CM_BIN"
max_workers = int("$PARALLEL_ARG")
token = os.environ.get("GITHUB_TOKEN") or os.environ.get("FLEET_READ_TOKEN") or ""

def scan_single_target(target):
    repo = target["repo"]
    branch = target["branch"]
    sha = target["sha"]
    safe_name = repo.replace("/", "_")

    worker_temp = tempfile.mkdtemp(prefix=f"cm_{safe_name}_")
    try:
        clone_dir = os.path.join(worker_temp, "repo")
        clone_url = f"https://github.com/{repo}.git"
        if token:
            clone_url = f"https://x-access-token:{token}@github.com/{repo}.git"

        print(f"  [>] [{repo}] 📥 Shallow cloning branch '{branch}' (HEAD: {sha[:7]})...")
        git_res = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", branch, "--quiet", clone_url, clone_dir],
            capture_output=True,
            text=True
        )
        if git_res.returncode != 0:
            print(f"  [!] [{repo}] ❌ Git clone failed: {git_res.stderr.strip()}", file=sys.stderr)
            return False, repo, 0

        # Copy central .codemender.yaml into cloned repo
        yaml_src = os.path.join(repo_root, ".codemender.yaml")
        if os.path.exists(yaml_src):
            shutil.copy(yaml_src, os.path.join(clone_dir, ".codemender.yaml"))

        # Setup isolated environment per repo
        isolated_home = os.path.join(worker_temp, "home")
        isolated_cm = os.path.join(isolated_home, ".codemender")
        os.makedirs(isolated_cm, exist_ok=True)

        real_cm = os.path.join(real_home, ".codemender")
        iso_cfg = os.path.join(isolated_cm, "config.yaml")
        if os.path.exists(os.path.join(real_cm, "config.yaml")):
            shutil.copy(os.path.join(real_cm, "config.yaml"), iso_cfg)
            try:
                import re
                with open(iso_cfg, "r", encoding="utf-8") as f:
                    cfg_text = f.read()
                abs_clone = os.path.abspath(clone_dir)
                if "project_paths:" in cfg_text:
                    cfg_text = re.sub(r"project_paths:\s*\[.*?\]", f'project_paths: ["{abs_clone}"]', cfg_text)
                    cfg_text = re.sub(r"project_paths:\s*\n(\s*-[^\n]*\n)*", f'project_paths:\n  - "{abs_clone}"\n', cfg_text)
                else:
                    cfg_text += f'\nproject_paths:\n  - "{abs_clone}"\n'
                with open(iso_cfg, "w", encoding="utf-8") as f:
                    f.write(cfg_text)
            except Exception:
                pass
        for k in os.listdir(real_cm):
            if k.startswith("identity.key"):
                shutil.copy(os.path.join(real_cm, k), os.path.join(isolated_cm, k))

        # Setup environment variables
        env = os.environ.copy()
        env["HOME"] = isolated_home
        real_config = os.path.join(real_home, ".config")
        if os.path.exists(real_config):
            try:
                os.symlink(real_config, os.path.join(isolated_home, ".config"))
            except Exception:
                pass

        adc_file = os.path.join(real_home, ".config", "gcloud", "application_default_credentials.json")
        if os.path.exists(adc_file):
            env["GOOGLE_APPLICATION_CREDENTIALS"] = adc_file

        print(f"  [>] [{repo}] 🚀 Launching CodeMender scan (cm find)...")
        start_time = time.time()

        # Capture scan log
        log_file = os.path.join(worker_temp, "scan.log")
        with open(log_file, "w", encoding="utf-8") as lf:
            find_res = subprocess.run(
                [cm_bin, "find", "-y", "--bypass-warning", "--unrestricted", "."],
                cwd=clone_dir,
                env=env,
                stdout=lf,
                stderr=subprocess.STDOUT
            )

        # Export SARIF findings
        report_sarif_path = os.path.join(clone_dir, "report.sarif")
        with open(report_sarif_path, "w", encoding="utf-8") as sf:
            rep_res = subprocess.run(
                [cm_bin, "report", "-f", "sarif"],
                cwd=clone_dir,
                env=env,
                stdout=sf,
                stderr=subprocess.DEVNULL
            )

        # Ensure destination directory exists and always preserve scan log
        dest_dir = os.path.join(sarifs_dir, safe_name)
        os.makedirs(dest_dir, exist_ok=True)
        if os.path.exists(log_file):
            shutil.copy(log_file, os.path.join(dest_dir, "scan.log"))

        # Stamp _fleet into SARIF
        findings_count = 0
        if os.path.exists(report_sarif_path) and os.path.getsize(report_sarif_path) > 0:
            try:
                with open(report_sarif_path, "r", encoding="utf-8") as f:
                    sarif_data = json.load(f)
                sarif_data["_fleet"] = {"repo": repo, "sha": sha}

                for run in sarif_data.get("runs", []):
                    findings_count += len(run.get("results", []))

                with open(report_sarif_path, "w", encoding="utf-8") as f:
                    json.dump(sarif_data, f, indent=2)
                    f.write("\n")

                shutil.copy(report_sarif_path, os.path.join(dest_dir, "report.sarif"))
            except Exception as exc:
                print(f"  [!] [{repo}] ⚠️ SARIF parse error: {exc}", file=sys.stderr)

        elapsed = int(time.time() - start_time)
        print(f"  [*] [{repo}] ✅ Finished in {elapsed}s (Findings: {findings_count})")
        return True, repo, findings_count

    except Exception as exc:
        print(f"  [!] [{repo}] ❌ Exception during scan: {exc}", file=sys.stderr)
        return False, repo, 0
    finally:
        shutil.rmtree(worker_temp, ignore_errors=True)

start_all = time.time()
completed_count = 0
total_findings = 0

print(f"Fanning out across {len(targets)} repositories with {max_workers} concurrent workers...")

with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
    future_to_repo = {executor.submit(scan_single_target, target): target["repo"] for target in targets}
    for future in concurrent.futures.as_completed(future_to_repo):
        repo_name = future_to_repo[future]
        try:
            success, r, count = future.result()
            if success:
                completed_count += 1
                total_findings += count
        except Exception as exc:
            print(f"  [!] Worker exception for {repo_name}: {exc}", file=sys.stderr)

total_elapsed = int(time.time() - start_all)
print(f"\nAll parallel scan jobs completed in {total_elapsed}s! (Scanned: {completed_count}/{len(targets)} repos)")
PYEOF

echo ""
echo "[Step 3/3] Running rollup on collected SARIFs..."
(cd "$REPO_ROOT" && python3 fleet_rollup.py --in "$SARIFS_DIR" --cursor "$REPO_ROOT/state/cursor.json")

echo ""
echo "=========================================================="
echo "Parallel fleet scan completed successfully!"
echo "Outputs generated:"
echo " - Report (Markdown): $REPO_ROOT/fleet_report.md"
echo " - Report (JSON):     $REPO_ROOT/fleet_report.json"
echo " - Updated cursor:    $REPO_ROOT/state/cursor.json"
echo "=========================================================="
