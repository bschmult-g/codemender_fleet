"""tests/test_rollup.py - Test fleet rollup processing, severity mapping, suppression filtering, and cursor updates."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys

# Ensure repository root is on sys.path for test runner imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import fleet_rollup


def test_rollup_with_fixtures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Assert rollup counts 2 findings, 2 repos, and writes both cursor entries."""
    fixtures_dir = Path(__file__).parent / "fixtures"
    cursor_file = tmp_path / "cursor.json"
    cursor_file.write_text("{}", encoding="utf-8")

    # Run fleet_rollup in tmp_path working directory so reports are written there
    monkeypatch.chdir(tmp_path)

    exit_code = fleet_rollup.main(["--in", str(fixtures_dir), "--cursor", str(cursor_file)])
    assert exit_code == 0

    # 1. Verify fleet_report.json
    report_json_file = tmp_path / "fleet_report.json"
    assert report_json_file.exists()
    report = json.loads(report_json_file.read_text(encoding="utf-8"))

    assert report["repos_scanned"] == 2
    assert report["total_findings"] == 2
    assert report["by_severity"]["high"] == 1
    assert report["by_severity"]["medium"] == 1
    assert report["by_severity"]["critical"] == 0
    assert report["by_severity"]["low"] == 0

    # Verify suppressed finding (CMDI / CWE-78) is excluded from CWE tallies
    cwe_keys = [item["cwe"] for item in report["top_cwes"]]
    assert "CWE-89" in cwe_keys
    assert "CWE-79" in cwe_keys
    assert "CWE-78" not in cwe_keys

    # 2. Verify state/cursor.json
    assert cursor_file.exists()
    cursor = json.loads(cursor_file.read_text(encoding="utf-8"))

    assert "acme/repo-with-findings" in cursor
    assert cursor["acme/repo-with-findings"]["last_sha"] == "aaaabbbbccccddddeeeeffff0000111122223333"
    assert cursor["acme/repo-with-findings"]["findings"] == 2
    assert "scanned_at" in cursor["acme/repo-with-findings"]

    assert "acme/clean-repo" in cursor
    assert cursor["acme/clean-repo"]["last_sha"] == "4444555566667777888899990000aaaabbbbcccc"
    assert cursor["acme/clean-repo"]["findings"] == 0
    assert "scanned_at" in cursor["acme/clean-repo"]

    # 3. Verify fleet_report.md
    report_md_file = tmp_path / "fleet_report.md"
    assert report_md_file.exists()
    md_content = report_md_file.read_text(encoding="utf-8")
    assert "# CodeMender Fleet Scan Report" in md_content
    assert "acme/repo-with-findings" in md_content
    assert "acme/clean-repo" in md_content


def test_rollup_skips_bad_sarif(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Assert corrupt/malformed SARIF files are skipped without crashing rollup."""
    sarif_dir = tmp_path / "sarifs"
    sarif_dir.mkdir()

    # Valid SARIF
    fixtures_dir = Path(__file__).parent / "fixtures"
    shutil.copy(fixtures_dir / "repo2.sarif", sarif_dir / "valid.sarif")

    # Corrupt JSON file
    (sarif_dir / "corrupted.sarif").write_text("{ incomplete json ...", encoding="utf-8")

    cursor_file = tmp_path / "cursor.json"
    monkeypatch.chdir(tmp_path)

    exit_code = fleet_rollup.main(["--in", str(sarif_dir), "--cursor", str(cursor_file)])
    assert exit_code == 0

    report = json.loads((tmp_path / "fleet_report.json").read_text(encoding="utf-8"))
    assert report["repos_scanned"] == 1
    assert report["total_findings"] == 0
