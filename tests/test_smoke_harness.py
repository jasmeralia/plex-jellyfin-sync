from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is required for the smoke harness")
def test_smoke_test_config_only_bootstraps_harness_config(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    harness_root = tmp_path / "repo"

    for relative_path in (
        "docker-compose.test.yml",
        "Dockerfile",
        "scripts/smoke_test.sh",
        "tests/harness/config/config.test.example.yaml",
    ):
        source = repo_root / relative_path
        destination = harness_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    media_fixture = harness_root / "tests" / "harness" / "media" / "fixture-01.mp4"
    media_fixture.parent.mkdir(parents=True, exist_ok=True)
    media_fixture.write_bytes(b"")

    result = subprocess.run(
        ["bash", str(harness_root / "scripts" / "smoke_test.sh"), "--config-only"],
        check=False,
        capture_output=True,
        text=True,
        cwd=harness_root,
    )

    assert result.returncode == 0, result.stderr
    assert "docker-compose.test.yml validated" in result.stdout

    generated_config = harness_root / "tests" / "harness" / "config" / "config.yaml"
    example_config = harness_root / "tests" / "harness" / "config" / "config.test.example.yaml"

    assert generated_config.exists()
    assert generated_config.read_text(encoding="utf-8") == example_config.read_text(encoding="utf-8")
