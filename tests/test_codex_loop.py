from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import plex_jellyfin_sync.codex_loop as codex_loop
from plex_jellyfin_sync.codex_loop import (
    STATUS_BLOCKED,
    STATUS_CONTINUE,
    STATUS_DONE,
    build_codex_command,
    build_done_check_prompt,
    build_prompt,
    build_subprocess_env,
    should_exit_on_done,
    status_from_text,
)


def test_status_from_text_parses_valid_status() -> None:
    assert status_from_text("STATUS: CONTINUE\nworking") == STATUS_CONTINUE
    assert status_from_text("STATUS: DONE\nfinished") == STATUS_DONE
    assert status_from_text("STATUS: BLOCKED\nneed credentials") == STATUS_BLOCKED


def test_status_from_text_rejects_invalid_values() -> None:
    assert status_from_text("") is None
    assert status_from_text("working without protocol") is None
    assert status_from_text("STATUS: MAYBE\nunclear") is None


def test_build_prompt_wraps_user_task() -> None:
    prompt = build_prompt("Implement the next slice.")

    assert "STATUS: CONTINUE" in prompt
    assert "STATUS: DONE" in prompt
    assert "STATUS: BLOCKED" in prompt
    assert prompt.rstrip().endswith("Implement the next slice.")


def test_build_done_check_prompt_references_original_task() -> None:
    prompt = build_done_check_prompt(
        "Finish spec implementation.",
        confirmations_seen=1,
        confirmations_required=2,
    )

    assert "You previously replied with STATUS: DONE." in prompt
    assert "DONE confirmations: 1/2" in prompt
    assert prompt.rstrip().endswith("Finish spec implementation.")


def test_should_exit_on_done_requires_requested_confirmations() -> None:
    assert not should_exit_on_done(confirmations_seen=1, confirmations_required=2)
    assert should_exit_on_done(confirmations_seen=2, confirmations_required=2)
    assert should_exit_on_done(confirmations_seen=1, confirmations_required=1)


def test_build_subprocess_env_prefers_workspace_venv(tmp_path: Path, monkeypatch) -> None:
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)

    env = build_subprocess_env(tmp_path)

    assert env["PATH"].split(":")[0] == str(venv_bin)
    assert env["VIRTUAL_ENV"] == str(tmp_path / ".venv")


def test_build_subprocess_env_leaves_env_unchanged_without_workspace_venv(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)

    env = build_subprocess_env(tmp_path)

    assert env["PATH"] == "/usr/bin:/bin"
    assert "VIRTUAL_ENV" not in env


def test_build_codex_command_resume_uses_only_supported_flags(tmp_path: Path) -> None:
    output_path = tmp_path / "last-message.txt"

    command = build_codex_command(
        codex_bin="codex",
        prompt="wrapped prompt",
        output_path=output_path,
        workdir=tmp_path,
        model="gpt-5",
        config_overrides=["foo.bar=1", 'baz="qux"'],
        full_auto=False,
        sandbox="workspace-write",
        resume=True,
    )

    assert command[:4] == ["codex", "exec", "resume", "--last"]
    assert "--output-last-message" in command
    assert str(output_path) in command
    assert "--cd" not in command
    assert "--color" not in command
    assert "--sandbox" not in command
    assert "--model" in command
    assert "--config" in command
    assert command[-1] == "wrapped prompt"


def test_build_codex_command_initial_exec_includes_cd_and_sandbox(tmp_path: Path) -> None:
    output_path = tmp_path / "last-message.txt"

    command = build_codex_command(
        codex_bin="codex",
        prompt="wrapped prompt",
        output_path=output_path,
        workdir=tmp_path,
        model="gpt-5",
        config_overrides=[],
        full_auto=False,
        sandbox="workspace-write",
        resume=False,
    )

    assert command[:2] == ["codex", "exec"]
    assert "resume" not in command
    assert "--cd" in command
    assert str(tmp_path) in command
    assert "--color" in command
    assert "never" in command
    assert "--sandbox" in command
    assert "workspace-write" in command


def test_direct_script_entrypoint_runs_without_installed_package() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/auto_continue_codex.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Run the local codex CLI in an auto-continue loop." in result.stdout


def test_main_requires_done_confirmation_before_exit(monkeypatch, tmp_path: Path) -> None:
    prompts: list[str] = []
    responses = iter(
        [
            (0, "STATUS: DONE\nfirst pass"),
            (0, "STATUS: DONE\nconfirmed"),
        ]
    )
    args = argparse.Namespace(
        prompt="Ship the task.",
        codex_bin="codex",
        cd=tmp_path,
        model=None,
        max_turns=5,
        delay_seconds=0.0,
        done_confirmations=2,
        continue_prompt="continue working",
        transcript=None,
        config=[],
        full_auto=False,
        sandbox="workspace-write",
    )

    monkeypatch.setattr(codex_loop, "parse_args", lambda argv=None: args)
    monkeypatch.setattr(codex_loop.shutil, "which", lambda *args, **kwargs: "/usr/bin/codex")
    monkeypatch.setattr(codex_loop, "run_turn", lambda **kwargs: prompts.append(kwargs["prompt"]) or next(responses))

    exit_code = codex_loop.main()

    assert exit_code == 0
    assert len(prompts) == 2
    assert "Task:\nShip the task." in prompts[0]
    assert "DONE confirmations: 1/2" in prompts[1]
    assert "Original task:\nShip the task." in prompts[1]


def test_main_returns_subprocess_failure_even_with_done_status(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    args = argparse.Namespace(
        prompt="Ship the task.",
        codex_bin="codex",
        cd=tmp_path,
        model=None,
        max_turns=5,
        delay_seconds=0.0,
        done_confirmations=2,
        continue_prompt="continue working",
        transcript=None,
        config=[],
        full_auto=False,
        sandbox="workspace-write",
    )

    monkeypatch.setattr(codex_loop, "parse_args", lambda argv=None: args)
    monkeypatch.setattr(codex_loop.shutil, "which", lambda *args, **kwargs: "/usr/bin/codex")
    monkeypatch.setattr(codex_loop, "run_turn", lambda **kwargs: (17, "STATUS: DONE\nfinished"))

    exit_code = codex_loop.main()
    captured = capsys.readouterr()

    assert exit_code == 17
    assert "despite STATUS: DONE" in captured.err
