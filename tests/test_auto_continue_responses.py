from __future__ import annotations

import os
import subprocess
import sys


def test_auto_continue_responses_help_runs_without_openai_package() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/auto_continue_responses.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Run an OpenAI Responses API task in an auto-continue loop." in result.stdout


def test_auto_continue_responses_reports_missing_openai_dependency() -> None:
    env = dict(os.environ)
    env["OPENAI_API_KEY"] = "test-key"

    result = subprocess.run(
        [sys.executable, "scripts/auto_continue_responses.py", "continue"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode != 0
    assert "The Python 'openai' package is required." in result.stderr
