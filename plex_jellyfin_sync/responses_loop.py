from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    from openai import OpenAI


STATUS_CONTINUE = "CONTINUE"
STATUS_DONE = "DONE"
STATUS_BLOCKED = "BLOCKED"
VALID_STATUSES = {STATUS_CONTINUE, STATUS_DONE, STATUS_BLOCKED}

DEFAULT_INSTRUCTIONS = """\
You are working in an autonomous execution loop.

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
        description="Run an OpenAI Responses API task in an auto-continue loop.",
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        help="Initial user prompt. If omitted, reads from stdin.",
    )
    parser.add_argument("--model", default="gpt-5")
    parser.add_argument("--max-turns", type=int, default=50)
    parser.add_argument("--delay-seconds", type=float, default=0.0)
    parser.add_argument("--instructions-file", type=Path)
    parser.add_argument("--continue-prompt", default=DEFAULT_CONTINUE_PROMPT)
    parser.add_argument(
        "--done-confirmations",
        type=int,
        default=DEFAULT_DONE_CONFIRMATIONS,
        help=(
            "Number of consecutive STATUS: DONE responses required before exiting. "
            "A value of 2 means the first DONE triggers a verification turn."
        ),
    )
    parser.add_argument("--transcript", type=Path)
    return parser.parse_args(argv)


def read_initial_prompt(args: argparse.Namespace) -> str:
    if args.prompt:
        return args.prompt.strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    raise SystemExit("Provide a prompt argument or pipe the prompt on stdin.")


def load_instructions(args: argparse.Namespace) -> str:
    if args.instructions_file:
        return args.instructions_file.read_text(encoding="utf-8")
    return DEFAULT_INSTRUCTIONS


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


def write_transcript(path: Path | None, role: str, text: str) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{role}\n{text}\n\n")


def build_openai_client() -> "OpenAI":
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise SystemExit(
            "The Python 'openai' package is required. Install it with `pip install openai`."
        ) from exc
    return OpenAI()


def create_response(
    *,
    client: Any,
    model: str,
    instructions: str,
    text: str,
    previous_response_id: str | None,
) -> Any:
    kwargs = {
        "model": model,
        "instructions": instructions,
        "input": text,
    }
    if previous_response_id is not None:
        kwargs["previous_response_id"] = previous_response_id
    return client.responses.create(**kwargs)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is required.")

    prompt = read_initial_prompt(args)
    instructions = load_instructions(args)
    client = build_openai_client()

    previous_response_id: str | None = None
    user_text = prompt
    done_confirmations_seen = 0

    for turn in range(1, args.max_turns + 1):
        write_transcript(args.transcript, f"USER TURN {turn}", user_text)
        response = create_response(
            client=client,
            model=args.model,
            instructions=instructions,
            text=user_text,
            previous_response_id=previous_response_id,
        )
        previous_response_id = response.id
        output_text = (response.output_text or "").strip()

        print(f"\n===== TURN {turn} =====\n")
        print(output_text)
        print()
        write_transcript(args.transcript, f"ASSISTANT TURN {turn}", output_text)

        status = status_from_text(output_text)
        if status == STATUS_DONE:
            done_confirmations_seen += 1
            if should_exit_on_done(
                confirmations_seen=done_confirmations_seen,
                confirmations_required=args.done_confirmations,
            ):
                return 0
            user_text = build_done_check_prompt(
                prompt,
                confirmations_seen=done_confirmations_seen,
                confirmations_required=max(args.done_confirmations, 1),
            )
        elif status == STATUS_BLOCKED:
            return 2
        elif status == STATUS_CONTINUE:
            done_confirmations_seen = 0
            user_text = args.continue_prompt
        else:
            print(
                "Missing or invalid STATUS line; stopping defensively.",
                file=sys.stderr,
            )
            return 3

        if args.delay_seconds > 0:
            time.sleep(args.delay_seconds)

    print(f"Reached max turns ({args.max_turns}) without DONE/BLOCKED.", file=sys.stderr)
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
