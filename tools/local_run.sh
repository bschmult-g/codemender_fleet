#!/usr/bin/env bash
# tools/local_run.sh - Drive the CodeMender fleet scanner pipeline locally with fake_cm.
# Puts tools/ on PATH as cm, runs enumerate, clones each target shallowly,
# runs fake cm, stamps _fleet, collects SARIFs, and rolls up the final report.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Ensure tools/ has a 'cm' command pointing to fake_cm.sh and is prepended to PATH
ln -sf "$SCRIPT_DIR/fake_cm.sh" "$SCRIPT_DIR/cm"
export PATH="$SCRIPT_DIR:$PATH"

USER_ARG=""
ORG_ARG=""
LIMIT_ARG="3"
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

if [[ -z "$USER_ARG" && -z "$ORG_ARG" ]]; then
  if [[ -n "${GH_USER:-}" ]]; then
    USER_ARG="$GH_USER"
  else
    echo "Error: Must specify --user <login> or --org <name> (or set GH_USER)." >&2
    exit 1
  fi
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
echo " CodeMender Fleet Scanner - Local Pipeline Driver"
echo "=========================================================="
echo "Using cm at: $(command -v cm)"

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
echo "[Step 2/3] Scanning repositories locally using fake cm..."
TEMP_BASE=$(mktemp -d)
trap 'rm -rf "$TEMP_BASE"' EXIT

# Read each target line from Python helper
python3 -c "
import json, sys
data = json.loads(sys.argv[1])
for item in data:
    print(f\"{item['repo']}\t{item['branch']}\t{item['sha']}\")
" "$MATRIX_JSON" | while IFS=$'\t' read -r repo branch sha; do
  echo "----------------------------------------------------------"
  echo "Target: $repo (branch: $branch, sha: ${sha:0:7})"

  safe_name=$(echo "$repo" | tr '/' '_')
  clone_dir="$TEMP_BASE/$safe_name"
  mkdir -p "$clone_dir"

  CLONE_URL="https://github.com/${repo}.git"
  TOKEN="${GITHUB_TOKEN:-${FLEET_READ_TOKEN:-}}"
  if [[ -n "$TOKEN" ]]; then
    CLONE_URL="https://x-access-token:${TOKEN}@github.com/${repo}.git"
  fi

  echo "Shallow cloning $repo..."
  if ! git clone --depth 1 --branch "$branch" --quiet "$CLONE_URL" "$clone_dir" 2>/dev/null; then
    echo "Warning: git clone failed for $repo; skipping" >&2
    rm -rf "$clone_dir"
    continue
  fi

  # Copy .codemender.yaml into cloned tree
  cp "$REPO_ROOT/.codemender.yaml" "$clone_dir/.codemender.yaml"

  # Run fake cm find
  echo "Running fake cm find..."
  (cd "$clone_dir" && cm find . --format sarif -o report.sarif)

  # Stamp _fleet into the SARIF
  echo "Stamping _fleet metadata..."
  python3 -c "
import json, sys
path, r, s = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, 'r', encoding='utf-8') as f:
    sarif = json.load(f)
sarif['_fleet'] = {'repo': r, 'sha': s}
with open(path, 'w', encoding='utf-8') as f:
    json.dump(sarif, f, indent=2)
    f.write('\n')
" "$clone_dir/report.sarif" "$repo" "$sha"

  # Save to sarifs/<owner_name>/report.sarif
  target_sarif_dir="$SARIFS_DIR/$safe_name"
  mkdir -p "$target_sarif_dir"
  cp "$clone_dir/report.sarif" "$target_sarif_dir/report.sarif"
  echo "Saved SARIF to $target_sarif_dir/report.sarif"

  rm -rf "$clone_dir"
done

echo ""
echo "[Step 3/3] Running rollup on collected SARIFs..."
(cd "$REPO_ROOT" && python3 fleet_rollup.py --in "$SARIFS_DIR" --cursor "$REPO_ROOT/state/cursor.json")

echo ""
echo "=========================================================="
echo "Local pipeline run completed successfully!"
echo "Outputs generated:"
echo " - Report (Markdown): $REPO_ROOT/fleet_report.md"
echo " - Report (JSON):     $REPO_ROOT/fleet_report.json"
echo " - Updated cursor:    $REPO_ROOT/state/cursor.json"
echo "=========================================================="
