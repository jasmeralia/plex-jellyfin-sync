from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Sequence


STATUS_CONTINUE = "CONTINUE"
STATUS_DONE = "DONE"
STATUS_BLOCKED = "BLOCKED"
VALID_STATUSES = {STATUS_CONTINUE, STATUS_DONE, STATUS_BLOCKED}

DEFAULT_PROTOCOL = """\
You are running inside an automated Codex CLI continuation loop.

Rules:
1. Start every response with exactly one status line in this format:
   STATUS: CONTINUE
   STATUS: DONE
   STATUS: BLOCKED
2. Use CONTINUE if more work should happen automatically in the next turn.
3. Use DONE only when the task is actually complete.
4. Use BLOCKED only when you need user input, approval, credentials, or some external dependency you cannot resolve yourself.
5. After the status line, write the normal response body.
6. Do not ask whether you should continue. If more work is possible, use STATUS: CONTINUE.
"""

DEFAULT_CONTINUE_PROMPT = (
    "Continue autonomously. Do not stop at green checkpoints. "
    "Only stop with STATUS: DONE or STATUS: BLOCKED."
)
DEFAULT_DONE_CONFIRMATIONS = 2


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the local codex CLI in an auto-continue loop.",
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        help="Initial user prompt. If omitted, reads from stdin.",
    )
    parser.add_argument(
        "--codex-bin",
        default="codex",
        help="Path to the codex executable.",
    )
    parser.add_argument(
        "--cd",
        type=Path,
        default=Path.cwd(),
        help="Workspace root to pass to codex.",
    )
    parser.add_argument("--model")
    parser.add_argument("--max-turns", type=int, default=50)
    parser.add_argument("--delay-seconds", type=float, default=0.0)
    parser.add_argument(
        "--done-confirmations",
        type=int,
        default=DEFAULT_DONE_CONFIRMATIONS,
        help=(
            "Number of consecutive STATUS: DONE responses required before exiting. "
            "A value of 2 means the first DONE triggers a verification turn."
        ),
    )
    parser.add_argument(
        "--continue-prompt",
        default=DEFAULT_CONTINUE_PROMPT,
        help="Prompt sent on each automatic continuation turn.",
    )
    parser.add_argument(
        "--transcript",
        type=Path,
        help="Optional transcript file.",
    )
    parser.add_argument(
        "--config",
        action="append",
        default=[],
        help="Repeatable codex -c/--config override.",
    )
    parser.add_argument(
        "--full-auto",
        action="store_true",
        help="Pass --full-auto through to codex.",
    )
    parser.add_argument(
        "--sandbox",
        choices=("read-only", "workspace-write", "danger-full-access"),
        help="Explicit codex sandbox mode. Ignored when --full-auto is set.",
    )
    return parser.parse_args(argv)


def read_initial_prompt(args: argparse.Namespace) -> str:
    if args.prompt:
        return args.prompt.strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    raise SystemExit("Provide a prompt argument or pipe the prompt on stdin.")


def status_from_text(text: str) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None
    first_line = stripped.splitlines()[0].strip()
    if not first_line.startswith("STATUS:"):
        return None
    status = first_line.split(":", 1)[1].strip().upper()
    if status in VALID_STATUSES:
        return status
    return None


def build_prompt(user_text: str) -> str:
    return f"{DEFAULT_PROTOCOL}\nTask:\n{user_text.strip()}\n"


def build_done_check_prompt(original_task: str, *, confirmations_seen: int, confirmations_required: int) -> str:
    return (
        "You previously replied with STATUS: DONE.\n\n"
        "Before stopping, audit the work against the original task and the current repository state.\n"
        "If any meaningful implementation, verification, or spec work remains, respond with STATUS: CONTINUE "
        "and keep working immediately.\n"
        "Use STATUS: BLOCKED only for a real blocker.\n"
        "Use STATUS: DONE only if the work is genuinely complete.\n\n"
        f"DONE confirmations: {confirmations_seen}/{confirmations_required}\n\n"
        "Original task:\n"
        f"{original_task.strip()}"
    )


def should_exit_on_done(*, confirmations_seen: int, confirmations_required: int) -> bool:
    return confirmations_seen >= max(confirmations_required, 1)


def build_subprocess_env(workdir: Path) -> dict[str, str]:
    env = dict(os.environ)
    venv_path = workdir / ".venv"
    venv_bin = venv_path / "bin"
    if venv_bin.is_dir():
        current_path = env.get("PATH", "")
        env["PATH"] = f"{venv_bin}{os.pathsep}{current_path}" if current_path else str(venv_bin)
        env["VIRTUAL_ENV"] = str(venv_path)
    return env


def write_transcript(path: Path | None, role: str, text: str) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{role}\n{text.rstrip()}\n\n")


def build_codex_command(
    *,
    codex_bin: str,
    prompt: str,
    output_path: Path,
    workdir: Path,
    model: str | None,
    config_overrides: Sequence[str],
    full_auto: bool,
    sandbox: str | None,
    resume: bool,
) -> list[str]:
    command = [codex_bin, "exec"]
    if resume:
        command.extend(["resume", "--last"])
        if model:
            command.extend(["--model", model])
        for override in config_overrides:
            command.extend(["--config", override])
        if full_auto:
            command.append("--full-auto")
        command.extend(["--output-last-message", str(output_path), prompt])
        return command

    if model:
        command.extend(["--model", model])
    command.extend(["--cd", str(workdir), "--color", "never"])
    for override in config_overrides:
        command.extend(["--config", override])
    if full_auto:
        command.append("--full-auto")
    elif sandbox:
        command.extend(["--sandbox", sandbox])
    command.extend(["--output-last-message", str(output_path), prompt])
    return command


def run_turn(
    *,
    codex_bin: str,
    prompt: str,
    workdir: Path,
    model: str | None,
    config_overrides: Sequence[str],
    full_auto: bool,
    sandbox: str | None,
    resume: bool,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    with tempfile.NamedTemporaryFile(prefix="codex-last-message-", suffix=".txt") as handle:
        output_path = Path(handle.name)
        command = build_codex_command(
            codex_bin=codex_bin,
            prompt=prompt,
            output_path=output_path,
            workdir=workdir,
            model=model,
            config_overrides=config_overrides,
            full_auto=full_auto,
            sandbox=sandbox,
            resume=resume,
        )
        completed = subprocess.run(command, check=False, cwd=workdir, env=env)
        message = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
    return completed.returncode, message


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    subprocess_env = build_subprocess_env(args.cd)
    if shutil.which(args.codex_bin, path=subprocess_env.get("PATH")) is None:
        raise SystemExit(f"Could not find codex executable: {args.codex_bin}")

    initial_prompt = read_initial_prompt(args)
    current_prompt = initial_prompt
    resume = False
    done_confirmations_seen = 0

    for turn in range(1, args.max_turns + 1):
        wrapped_prompt = build_prompt(current_prompt)
        write_transcript(args.transcript, f"USER TURN {turn}", wrapped_prompt)
        exit_code, message = run_turn(
            codex_bin=args.codex_bin,
            prompt=wrapped_prompt,
            workdir=args.cd,
            model=args.model,
            config_overrides=args.config,
            full_auto=args.full_auto,
            sandbox=args.sandbox,
            resume=resume,
            env=subprocess_env,
        )
        write_transcript(args.transcript, f"ASSISTANT TURN {turn}", message)

        status = status_from_text(message)
        if status in VALID_STATUSES and exit_code != 0:
            print(
                f"codex exited with status {exit_code} despite STATUS: {status}; stopping.",
                file=sys.stderr,
            )
            return exit_code
        if status == STATUS_DONE:
            done_confirmations_seen += 1
            if should_exit_on_done(
                confirmations_seen=done_confirmations_seen,
                confirmations_required=args.done_confirmations,
            ):
                return 0
            current_prompt = build_done_check_prompt(
                initial_prompt,
                confirmations_seen=done_confirmations_seen,
                confirmations_required=max(args.done_confirmations, 1),
            )
            resume = True
            if args.delay_seconds > 0:
                time.sleep(args.delay_seconds)
            continue
        if status == STATUS_BLOCKED:
            return 2
        if status == STATUS_CONTINUE:
            done_confirmations_seen = 0
            current_prompt = args.continue_prompt
            resume = True
            if args.delay_seconds > 0:
                time.sleep(args.delay_seconds)
            continue

        if exit_code != 0:
            return exit_code
        print(
            "Missing or invalid STATUS line in the final message; stopping defensively.",
            file=sys.stderr,
        )
        return 3

    print(f"Reached max turns ({args.max_turns}) without DONE/BLOCKED.", file=sys.stderr)
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
