#!/usr/bin/env python3
"""fleet_rollup.py - Aggregate SARIF scan results from multiple repositories.

Glob-searches SARIF files, filters suppressions, normalizes severities,
tallies CWEs, outputs fleet_report.json and fleet_report.md, and advances
state/cursor.json for each scanned repository.
"""
from __future__ import annotations

import argparse
import collections
from datetime import datetime, timezone
import glob
import json
import logging
import os
import sys
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("fleet_rollup")

SEVERITY_ORDER = ["critical", "high", "medium", "low"]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Roll up CodeMender fleet SARIF reports into consolidated summaries."
    )
    parser.add_argument(
        "--in",
        dest="in_dir",
        default="sarifs",
        help="Input directory containing SARIF files (default: sarifs).",
    )
    parser.add_argument(
        "--cursor",
        default="state/cursor.json",
        help="Path to cursor state file (default: state/cursor.json).",
    )
    return parser.parse_args(argv)


def map_raw_severity(val: Any) -> str | None:
    """Map raw severity value (numeric CVSS score or text string) to standard levels."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        if val >= 9.0:
            return "critical"
        if val >= 7.0:
            return "high"
        if val >= 4.0:
            return "medium"
        return "low"
    if isinstance(val, str):
        v = val.strip().lower()
        if not v:
            return None
        try:
            num = float(v)
            return map_raw_severity(num)
        except ValueError:
            pass
        if v in ("critical", "blocker"):
            return "critical"
        if v in ("high", "error"):
            return "high"
        if v in ("medium", "moderate", "warning"):
            return "medium"
        if v in ("low", "note", "info", "informational"):
            return "low"
    return None


def normalize_severity(result: dict[str, Any], rule: dict[str, Any]) -> str:
    """Normalize severity checking result properties, rule properties, SARIF level, then fallback to low."""
    # 1. Result properties: security-severity, severity, problem.severity
    res_props = result.get("properties") or {}
    for key in ("security-severity", "severity", "problem.severity"):
        val = res_props.get(key)
        mapped = map_raw_severity(val)
        if mapped:
            return mapped

    # 2. Rule properties: problem.severity
    rule_props = rule.get("properties") or {}
    val = rule_props.get("problem.severity")
    mapped = map_raw_severity(val)
    if mapped:
        return mapped

    # 3. SARIF level: error -> high, warning -> medium, note/none -> low
    level = str(result.get("level", "")).strip().lower()
    if level == "error":
        return "high"
    if level == "warning":
        return "medium"
    if level in ("note", "none"):
        return "low"

    # Default fallback
    return "low"


def get_rule_for_result(
    result: dict[str, Any],
    rules_by_id: dict[str, dict[str, Any]],
    rules_list: list[dict[str, Any]],
) -> dict[str, Any]:
    """Retrieve rule matching result via ruleId or ruleIndex."""
    rule_id = result.get("ruleId")
    if rule_id and rule_id in rules_by_id:
        return rules_by_id[rule_id]
    rule_index = result.get("ruleIndex")
    if isinstance(rule_index, int) and 0 <= rule_index < len(rules_list):
        return rules_list[rule_index]
    return {}


def process_sarif_file(
    file_path: str,
    global_severities: dict[str, int],
    cwe_counter: collections.Counter[str],
    repo_summaries: dict[str, dict[str, Any]],
) -> bool:
    """Parse a single SARIF file and aggregate findings. Never raise on malformed file."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        logger.warning("Skipping unparseable SARIF file %s: %s", file_path, exc)
        return False

    if not isinstance(data, dict):
        logger.warning("Skipping non-dict SARIF root in %s", file_path)
        return False

    fleet_meta = data.get("_fleet")
    if not fleet_meta and data.get("runs"):
        fleet_meta = data["runs"][0].get("properties", {}).get("_fleet")
    if not isinstance(fleet_meta, dict):
        fleet_meta = {}

    repo = fleet_meta.get("repo", "unknown")
    sha = fleet_meta.get("sha", "")

    if repo not in repo_summaries:
        repo_summaries[repo] = {
            "repo": repo,
            "sha": sha,
            "findings": 0,
            "by_severity": {s: 0 for s in SEVERITY_ORDER},
        }
    elif sha and not repo_summaries[repo]["sha"]:
        repo_summaries[repo]["sha"] = sha

    runs = data.get("runs", [])
    if not isinstance(runs, list):
        return True

    for run in runs:
        if not isinstance(run, dict):
            continue

        tool_driver = run.get("tool", {}).get("driver", {})
        raw_rules = tool_driver.get("rules", []) if isinstance(tool_driver, dict) else []
        rules_list: list[dict[str, Any]] = [r for r in raw_rules if isinstance(r, dict)]
        rules_by_id: dict[str, dict[str, Any]] = {
            r["id"]: r for r in rules_list if "id" in r and isinstance(r["id"], str)
        }

        results = run.get("results", [])
        if not isinstance(results, list):
            continue

        for res in results:
            if not isinstance(res, dict):
                continue

            # Skip any result carrying a suppressions array
            suppressions = res.get("suppressions")
            if suppressions is not None and isinstance(suppressions, list) and len(suppressions) > 0:
                continue

            rule = get_rule_for_result(res, rules_by_id, rules_list)
            severity = normalize_severity(res, rule)

            # Record severity counts
            global_severities[severity] = global_severities.get(severity, 0) + 1
            repo_summaries[repo]["by_severity"][severity] += 1
            repo_summaries[repo]["findings"] += 1

            # Tally CWEs from rule properties.tags entries starting with CWE
            rule_tags = rule.get("properties", {}).get("tags", [])
            if isinstance(rule_tags, list):
                for tag in rule_tags:
                    if isinstance(tag, str) and tag.strip().upper().startswith("CWE"):
                        cwe_key = tag.strip().split(":")[0].strip().split()[0].upper()
                        cwe_counter[cwe_key] += 1

    return True


def write_reports(
    now_iso: str,
    repos_scanned: int,
    total_findings: int,
    by_severity: dict[str, int],
    top_cwes: list[dict[str, Any]],
    repos_list: list[dict[str, Any]],
) -> None:
    """Generate fleet_report.json, fleet_report.md, and append to GITHUB_STEP_SUMMARY."""
    report_data = {
        "generated_at": now_iso,
        "repos_scanned": repos_scanned,
        "total_findings": total_findings,
        "by_severity": by_severity,
        "top_cwes": top_cwes,
        "repos": repos_list,
    }

    # 1. Write fleet_report.json
    with open("fleet_report.json", "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)
        f.write("\n")
    logger.info("Wrote fleet_report.json")

    # 2. Write fleet_report.md
    md_lines: list[str] = [
        "# CodeMender Fleet Scan Report",
        "",
        f"**Generated:** {now_iso} | **Repositories Scanned:** {repos_scanned} | **Total Findings:** {total_findings}",
        "",
        "## Findings by Severity",
        "",
        "| Severity | Count |",
        "| :--- | :--- |",
    ]
    for s in SEVERITY_ORDER:
        md_lines.append(f"| {s.capitalize()} | {by_severity.get(s, 0)} |")

    md_lines.extend(
        [
            "",
            "## Repository Summary (Top 50)",
            "",
            "| Repository | Commit | Findings |",
            "| :--- | :--- | :--- |",
        ]
    )

    top_repos = repos_list[:50]
    if top_repos:
        for r in top_repos:
            short_sha = f"`{r['sha'][:7]}`" if r.get("sha") else "-"
            md_lines.append(f"| {r['repo']} | {short_sha} | {r['findings']} |")
    else:
        md_lines.append("| *No repositories scanned* | - | 0 |")

    report_md = "\n".join(md_lines) + "\n"

    with open("fleet_report.md", "w", encoding="utf-8") as f:
        f.write(report_md)
    logger.info("Wrote fleet_report.md")

    # 3. Append to $GITHUB_STEP_SUMMARY if available
    step_summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary_path:
        try:
            with open(step_summary_path, "a", encoding="utf-8") as f:
                f.write(report_md)
            logger.info("Appended summary to $GITHUB_STEP_SUMMARY")
        except Exception as exc:
            logger.warning("Failed writing to $GITHUB_STEP_SUMMARY: %s", exc)


def advance_cursor(cursor_path: str, repos_list: list[dict[str, Any]], now_iso: str) -> None:
    """Advance state/cursor.json for every repo with a non-empty sha, writing back sorted."""
    cursor: dict[str, Any] = {}
    if os.path.exists(cursor_path):
        try:
            with open(cursor_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    cursor = loaded
        except Exception as exc:
            logger.warning("Could not read existing cursor at %s: %s", cursor_path, exc)

    updated_count = 0
    for r in repos_list:
        repo_name = r["repo"]
        sha = r["sha"]
        if repo_name and repo_name != "unknown" and sha:
            cursor[repo_name] = {
                "last_sha": sha,
                "scanned_at": now_iso,
                "findings": r["findings"],
            }
            updated_count += 1

    parent_dir = os.path.dirname(os.path.abspath(cursor_path))
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    with open(cursor_path, "w", encoding="utf-8") as f:
        json.dump(cursor, f, indent=2, sort_keys=True)
        f.write("\n")

    logger.info("Updated cursor at %s (%d repos updated)", cursor_path, updated_count)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    sarif_pattern = os.path.join(args.in_dir, "**", "*.sarif")
    sarif_files = glob.glob(sarif_pattern, recursive=True)
    logger.info("Found %d SARIF files in %s", len(sarif_files), args.in_dir)

    global_severities: dict[str, int] = {s: 0 for s in SEVERITY_ORDER}
    cwe_counter: collections.Counter[str] = collections.Counter()
    repo_summaries: dict[str, dict[str, Any]] = {}

    for file_path in sarif_files:
        process_sarif_file(file_path, global_severities, cwe_counter, repo_summaries)

    repos_list = list(repo_summaries.values())
    repos_list.sort(key=lambda r: r["findings"], reverse=True)

    total_findings = sum(global_severities.values())
    repos_scanned = len(repos_list)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    top_cwes = [{"cwe": cwe, "count": count} for cwe, count in cwe_counter.most_common(10)]

    logger.info(
        "Rollup complete: %d repos, %d findings (Critical: %d, High: %d, Medium: %d, Low: %d)",
        repos_scanned,
        total_findings,
        global_severities["critical"],
        global_severities["high"],
        global_severities["medium"],
        global_severities["low"],
    )

    write_reports(
        now_iso=now_iso,
        repos_scanned=repos_scanned,
        total_findings=total_findings,
        by_severity=global_severities,
        top_cwes=top_cwes,
        repos_list=repos_list,
    )

    advance_cursor(args.cursor, repos_list, now_iso)
    return 0


if __name__ == "__main__":
    sys.exit(main())
