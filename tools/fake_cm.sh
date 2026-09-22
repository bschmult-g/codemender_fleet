#!/usr/bin/env bash
# fake_cm.sh - Stub CodeMender CLI for free offline testing.
# Accepts: cm find . --format sarif -o <path>
# Ignores everything except -o and writes valid SARIF 2.1.0.
set -euo pipefail

OUTPUT="report.sarif"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|--output)
      OUTPUT="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done

python3 - "$OUTPUT" << 'PYEOF'
import hashlib
import json
import os
import sys

out_path = sys.argv[1]
pwd = os.getcwd()

# Hash working directory to obtain a deterministic 0-3 result count
h = int(hashlib.sha256(pwd.encode("utf-8")).hexdigest(), 16)
num_results = h % 4

rules = [
    {
        "id": "SQLI",
        "name": "SqlInjection",
        "shortDescription": {
            "text": "Potential SQL Injection vulnerability"
        },
        "fullDescription": {
            "text": "Untrusted input concatenated into SQL query without parameterization."
        },
        "properties": {
            "problem.severity": "high",
            "tags": [
                "CWE-89",
                "security"
            ]
        }
    }
]

results = []
for i in range(num_results):
    results.append({
        "ruleId": "SQLI",
        "ruleIndex": 0,
        "level": "error",
        "message": {
            "text": f"Potential SQL injection finding #{i + 1} detected in {os.path.basename(pwd)}"
        },
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {
                        "uri": f"src/db_query_{i + 1}.py"
                    },
                    "region": {
                        "startLine": 10 * (i + 1),
                        "startColumn": 1
                    }
                }
            }
        ],
        "properties": {
            "problem.severity": "high"
        }
    })

sarif_doc = {
    "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
    "version": "2.1.0",
    "runs": [
        {
            "tool": {
                "driver": {
                    "name": "CodeMender",
                    "semanticVersion": "1.0.0-preview",
                    "rules": rules
                }
            },
            "results": results
        }
    ]
}

parent_dir = os.path.dirname(os.path.abspath(out_path))
if parent_dir:
    os.makedirs(parent_dir, exist_ok=True)

with open(out_path, "w", encoding="utf-8") as f:
    json.dump(sarif_doc, f, indent=2)
    f.write("\n")

print(f"[fake_cm] Scanned {pwd}: generated {num_results} findings into {out_path}", file=sys.stderr)
PYEOF
