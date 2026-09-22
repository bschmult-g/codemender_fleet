"""tests/test_enumerate.py - Unit tests for repository enumeration and filtering."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fleet_enumerate


def test_is_repo_filtered() -> None:
    exclude = [".github", "*-archive", "*-sandbox", "*-docs"]

    # Normal valid repo
    valid_repo = {"name": "core-api", "size": 100, "archived": False, "fork": False, "disabled": False}
    assert not fleet_enumerate.is_repo_filtered(valid_repo, 64, exclude, None)

    # Archived repo
    archived_repo = {"name": "core-api", "size": 100, "archived": True, "fork": False, "disabled": False}
    assert fleet_enumerate.is_repo_filtered(archived_repo, 64, exclude, None)

    # Fork
    fork_repo = {"name": "core-api", "size": 100, "archived": False, "fork": True, "disabled": False}
    assert fleet_enumerate.is_repo_filtered(fork_repo, 64, exclude, None)

    # Disabled
    disabled_repo = {"name": "core-api", "size": 100, "archived": False, "fork": False, "disabled": True}
    assert fleet_enumerate.is_repo_filtered(disabled_repo, 64, exclude, None)

    # Too small
    small_repo = {"name": "core-api", "size": 32, "archived": False, "fork": False, "disabled": False}
    assert fleet_enumerate.is_repo_filtered(small_repo, 64, exclude, None)

    # Matches exclude pattern
    docs_repo = {"name": "project-docs", "size": 100, "archived": False, "fork": False, "disabled": False}
    assert fleet_enumerate.is_repo_filtered(docs_repo, 64, exclude, None)


def test_effective_limit() -> None:
    assert fleet_enumerate.get_effective_limit(3) == 3
    assert fleet_enumerate.get_effective_limit(0) == 256
    assert fleet_enumerate.get_effective_limit(500) == 256


@patch("fleet_enumerate.create_session")
@patch("fleet_enumerate.fetch_all_repos")
@patch("fleet_enumerate.get_default_branch_head_sha")
def test_enumerate_skips_unchanged(
    mock_get_head_sha: MagicMock,
    mock_fetch_all: MagicMock,
    mock_session: MagicMock,
    tmp_path: Path,
    monkeypatch: any,
) -> None:
    mock_fetch_all.return_value = [
        {"name": "repo1", "full_name": "user/repo1", "size": 100, "default_branch": "main"},
        {"name": "repo2", "full_name": "user/repo2", "size": 100, "default_branch": "main"},
    ]
    mock_get_head_sha.side_effect = ["sha-1111", "sha-2222"]

    cursor_file = tmp_path / "cursor.json"
    cursor_file.write_text(
        json.dumps({"user/repo1": {"last_sha": "sha-1111", "scanned_at": "...", "findings": 0}}),
        encoding="utf-8",
    )

    out_file = tmp_path / "github_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))

    exit_code = fleet_enumerate.main(
        [
            "--user",
            "dummy_user",
            "--cursor",
            str(cursor_file),
            "--skip-unchanged",
            "--limit",
            "5",
        ]
    )
    assert exit_code == 0

    content = out_file.read_text(encoding="utf-8")
    assert "count=1" in content
    assert "user/repo2" in content
    assert "user/repo1" not in content
