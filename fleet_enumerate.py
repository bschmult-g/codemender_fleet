#!/usr/bin/env python3
"""fleet_enumerate.py - Enumerate GitHub repositories and emit a GitHub Actions matrix.

Enumerates repos for a personal user or organization, applies filtering rules,
checks state/cursor.json for unchanged HEAD commits, and outputs a JSON matrix
compatible with GitHub Actions and local pipeline runners.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import os
import sys
import time
from typing import Any

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("fleet_enumerate")

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
DEFAULT_EXCLUDES = [".github", "*-archive", "*-sandbox", "*-docs"]
MAX_GITHUB_MATRIX_CAP = 256


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enumerate GitHub repositories for CodeMender fleet scanning."
    )
    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument(
        "--user",
        help="GitHub user login (personal account). Default if GH_USER env is set.",
    )
    target_group.add_argument(
        "--org",
        help="GitHub organization name.",
    )
    parser.add_argument(
        "--min-kb",
        type=int,
        default=64,
        help="Minimum repository size in KB to consider (default: 64).",
    )
    parser.add_argument(
        "--include",
        action="extend",
        nargs="+",
        default=None,
        help="fnmatch globs of repo names to include (default: all).",
    )
    parser.add_argument(
        "--exclude",
        action="extend",
        nargs="+",
        default=None,
        help="fnmatch globs of repo names to exclude (default: .github, *-archive, *-sandbox, *-docs).",
    )
    parser.add_argument(
        "--skip-unchanged",
        action="store_true",
        help="Skip repositories whose HEAD commit SHA matches state/cursor.json.",
    )
    parser.add_argument(
        "--cursor",
        default="state/cursor.json",
        help="Path to cursor state file (default: state/cursor.json).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=3,
        help="Safety cap on targets (0 = unlimited, capped at 256; default: 3).",
    )
    args = parser.parse_args(argv)

    # Validate target group (default to --user if env GH_USER is set)
    if not args.user and not args.org:
        env_user = os.environ.get("GH_USER")
        if env_user:
            args.user = env_user
            logger.info("Defaulting target to user from GH_USER env var: %s", args.user)
        else:
            parser.error("One of --user <login> or --org <name> is required.")

    return args


def get_effective_limit(limit_arg: int) -> int:
    """Validate limit and clamp to GitHub Actions matrix hard cap (256)."""
    if limit_arg <= 0:
        logger.warning(
            "Limit set to %d (unlimited); capping at %d due to GitHub Actions matrix hard cap.",
            limit_arg,
            MAX_GITHUB_MATRIX_CAP,
        )
        return MAX_GITHUB_MATRIX_CAP
    if limit_arg > MAX_GITHUB_MATRIX_CAP:
        logger.warning(
            "Requested limit %d exceeds GitHub matrix limit; truncating to %d.",
            limit_arg,
            MAX_GITHUB_MATRIX_CAP,
        )
        return MAX_GITHUB_MATRIX_CAP
    return limit_arg


def create_session() -> requests.Session:
    """Build requests session with GitHub API authentication and headers."""
    session = requests.Session()
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("FLEET_READ_TOKEN")
    session.headers.update(
        {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
    )
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    else:
        logger.warning("No GITHUB_TOKEN or FLEET_READ_TOKEN found; running unauthenticated.")
    return session


def api_request(
    session: requests.Session,
    url: str,
    params: dict[str, Any] | None = None,
    max_retries: int = 5,
) -> requests.Response:
    """Execute GET request with automatic sleep-and-retry on rate limit (HTTP 403 / 429)."""
    attempts = 0
    while True:
        attempts += 1
        resp = session.get(url, params=params)
        if resp.status_code in (403, 429) and "rate limit" in resp.text.lower():
            if attempts > max_retries:
                logger.error("Rate limit retry budget exceeded for URL: %s", url)
                resp.raise_for_status()
            reset_header = resp.headers.get("X-RateLimit-Reset")
            if reset_header:
                try:
                    reset_time = int(reset_header)
                    sleep_duration = max(1, reset_time - int(time.time())) + 1
                except ValueError:
                    sleep_duration = 60
            else:
                sleep_duration = 60
            logger.warning(
                "Hit GitHub rate limit on %s (attempt %d/%d). Sleeping %d seconds until reset...",
                url,
                attempts,
                max_retries,
                sleep_duration,
            )
            time.sleep(sleep_duration)
            continue

        resp.raise_for_status()
        return resp


def fetch_all_repos(session: requests.Session, is_user: bool, target_name: str) -> list[dict[str, Any]]:
    """Paginate and fetch all repositories for user or org via Link header."""
    if is_user:
        endpoint = f"{GITHUB_API_BASE}/users/{target_name}/repos"
    else:
        endpoint = f"{GITHUB_API_BASE}/orgs/{target_name}/repos"

    repos: list[dict[str, Any]] = []
    next_url: str | None = endpoint
    params: dict[str, Any] | None = {"per_page": 100, "type": "all"}

    while next_url:
        logger.info("Fetching repos from %s", next_url)
        resp = api_request(session, next_url, params=params)
        page_data = resp.json()
        if not isinstance(page_data, list):
            break
        repos.extend(page_data)
        params = None
        # Follow Link header next relation
        next_link = resp.links.get("next")
        next_url = next_link.get("url") if next_link else None

    logger.info("Discovered %d total repositories from GitHub API", len(repos))
    return repos


def load_cursor(cursor_path: str) -> dict[str, Any]:
    """Load state/cursor.json, returning empty dict if missing or unparseable."""
    if not os.path.exists(cursor_path):
        return {}
    try:
        with open(cursor_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Could not read cursor state at %s: %s", cursor_path, exc)
        return {}


def is_repo_filtered(
    repo: dict[str, Any],
    min_kb: int,
    exclude_patterns: list[str],
    include_patterns: list[str] | None,
) -> bool:
    """Filter out archived, fork, disabled, small repos, and glob exclusions."""
    name = repo.get("name", "")
    full_name = repo.get("full_name", name)

    if repo.get("archived", False):
        logger.debug("Filtering %s: archived", full_name)
        return True
    if repo.get("fork", False):
        logger.debug("Filtering %s: fork", full_name)
        return True
    if repo.get("disabled", False):
        logger.debug("Filtering %s: disabled", full_name)
        return True

    size_kb = repo.get("size", 0)
    if size_kb < min_kb:
        logger.debug("Filtering %s: size %d KB < %d KB", full_name, size_kb, min_kb)
        return True

    if any(fnmatch.fnmatch(name, pat) for pat in exclude_patterns):
        logger.debug("Filtering %s: matches exclude pattern", full_name)
        return True

    if include_patterns and not any(fnmatch.fnmatch(name, pat) for pat in include_patterns):
        logger.debug("Filtering %s: does not match include patterns", full_name)
        return True

    return False


def get_default_branch_head_sha(
    session: requests.Session,
    full_name: str,
    default_branch: str,
) -> str | None:
    """Fetch HEAD commit SHA for repo default branch. Return None if empty repo (409)."""
    commit_url = f"{GITHUB_API_BASE}/repos/{full_name}/commits/{default_branch}"
    try:
        resp = api_request(session, commit_url)
        data = resp.json()
        return data.get("sha")
    except requests.exceptions.HTTPError as err:
        if err.response is not None and err.response.status_code == 409:
            logger.warning("Skipping %s: repository is empty (HTTP 409)", full_name)
            return None
        raise


def emit_output(target_items: list[dict[str, str]]) -> None:
    """Write matrix and count to $GITHUB_OUTPUT if set; otherwise print to stdout."""
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        compact_json = json.dumps(target_items, separators=(",", ":"))
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"matrix={compact_json}\n")
            f.write(f"count={len(target_items)}\n")
        logger.info("Wrote matrix (%d targets) to $GITHUB_OUTPUT", len(target_items))
    else:
        # Pretty-print to stdout for local consumption
        print(json.dumps(target_items, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    effective_limit = get_effective_limit(args.limit)

    exclude_patterns = DEFAULT_EXCLUDES if args.exclude is None else args.exclude
    include_patterns = args.include

    session = create_session()
    is_user = bool(args.user)
    target_name = args.user if is_user else args.org

    repos = fetch_all_repos(session, is_user, target_name)
    cursor = load_cursor(args.cursor)

    target_items: list[dict[str, str]] = []
    skipped_unchanged = 0

    for repo in repos:
        if is_repo_filtered(repo, args.min_kb, exclude_patterns, include_patterns):
            continue

        full_name = repo.get("full_name", "")
        default_branch = repo.get("default_branch") or "main"

        head_sha = get_default_branch_head_sha(session, full_name, default_branch)
        if not head_sha:
            continue

        if args.skip_unchanged:
            last_sha = cursor.get(full_name, {}).get("last_sha")
            if last_sha and last_sha == head_sha:
                skipped_unchanged += 1
                logger.info("Skipping %s: unchanged at %s", full_name, head_sha[:7])
                continue

        target_items.append(
            {
                "repo": full_name,
                "branch": default_branch,
                "sha": head_sha,
            }
        )

        if len(target_items) >= effective_limit:
            logger.info("Reached limit cap of %d targets; stopping enumeration", effective_limit)
            break

    logger.info("targets=%d skipped_unchanged=%d", len(target_items), skipped_unchanged)
    emit_output(target_items)
    return 0


if __name__ == "__main__":
    sys.exit(main())
