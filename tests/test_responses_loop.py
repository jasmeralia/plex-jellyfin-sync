from __future__ import annotations

import argparse
from pathlib import Path

import plex_jellyfin_sync.responses_loop as responses_loop
from plex_jellyfin_sync.responses_loop import (
    STATUS_BLOCKED,
    STATUS_CONTINUE,
    STATUS_DONE,
    build_done_check_prompt,
    should_exit_on_done,
    status_from_text,
)


class FakeResponse:
    def __init__(self, response_id: str, output_text: str) -> None:
        self.id = response_id
        self.output_text = output_text


class FakeClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []
        self.responses = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("unexpected response create call")
        return self._responses.pop(0)


def test_status_from_text_parses_valid_status() -> None:
    assert status_from_text("STATUS: CONTINUE\nworking") == STATUS_CONTINUE
    assert status_from_text("STATUS: DONE\nfinished") == STATUS_DONE
    assert status_from_text("STATUS: BLOCKED\nneed credentials") == STATUS_BLOCKED


def test_status_from_text_rejects_invalid_values() -> None:
    assert status_from_text("") is None
    assert status_from_text("working without protocol") is None
    assert status_from_text("STATUS: MAYBE\nunclear") is None


def test_build_done_check_prompt_references_original_task() -> None:
    prompt = build_done_check_prompt(
        "Finish the task.",
        confirmations_seen=1,
        confirmations_required=2,
    )

    assert "You previously replied with STATUS: DONE." in prompt
    assert "DONE confirmations: 1/2" in prompt
    assert prompt.rstrip().endswith("Finish the task.")


def test_should_exit_on_done_requires_requested_confirmations() -> None:
    assert not should_exit_on_done(confirmations_seen=1, confirmations_required=2)
    assert should_exit_on_done(confirmations_seen=2, confirmations_required=2)
    assert should_exit_on_done(confirmations_seen=1, confirmations_required=1)


def test_main_requires_done_confirmation_before_exit(monkeypatch, tmp_path: Path, capsys) -> None:
    fake_client = FakeClient(
        [
            FakeResponse("resp-1", "STATUS: DONE\nfirst pass"),
            FakeResponse("resp-2", "STATUS: DONE\nconfirmed"),
        ]
    )
    args = argparse.Namespace(
        prompt="Ship the task.",
        model="gpt-5",
        max_turns=5,
        delay_seconds=0.0,
        instructions_file=None,
        continue_prompt="continue working",
        done_confirmations=2,
        transcript=None,
    )

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(responses_loop, "parse_args", lambda argv=None: args)
    monkeypatch.setattr(responses_loop, "build_openai_client", lambda: fake_client)

    exit_code = responses_loop.main()
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "STATUS: DONE\nfirst pass" in captured.out
    assert "STATUS: DONE\nconfirmed" in captured.out
    assert len(fake_client.calls) == 2
    assert fake_client.calls[0]["input"] == "Ship the task."
    assert "DONE confirmations: 1/2" in str(fake_client.calls[1]["input"])
    assert "Original task:\nShip the task." in str(fake_client.calls[1]["input"])
    assert fake_client.calls[1]["previous_response_id"] == "resp-1"


def test_main_resets_done_confirmation_after_continue(monkeypatch, tmp_path: Path) -> None:
    fake_client = FakeClient(
        [
            FakeResponse("resp-1", "STATUS: DONE\nfirst pass"),
            FakeResponse("resp-2", "STATUS: CONTINUE\nkeep going"),
            FakeResponse("resp-3", "STATUS: DONE\nsecond first pass"),
            FakeResponse("resp-4", "STATUS: DONE\nconfirmed"),
        ]
    )
    args = argparse.Namespace(
        prompt="Ship the task.",
        model="gpt-5",
        max_turns=6,
        delay_seconds=0.0,
        instructions_file=None,
        continue_prompt="continue working",
        done_confirmations=2,
        transcript=None,
    )

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(responses_loop, "parse_args", lambda argv=None: args)
    monkeypatch.setattr(responses_loop, "build_openai_client", lambda: fake_client)

    exit_code = responses_loop.main()

    assert exit_code == 0
    assert len(fake_client.calls) == 4
    assert "DONE confirmations: 1/2" in str(fake_client.calls[1]["input"])
    assert fake_client.calls[2]["input"] == "continue working"
    assert "DONE confirmations: 1/2" in str(fake_client.calls[3]["input"])


def test_main_supports_single_done_confirmation(monkeypatch, tmp_path: Path) -> None:
    fake_client = FakeClient([FakeResponse("resp-1", "STATUS: DONE\nfinished")])
    args = argparse.Namespace(
        prompt="Ship the task.",
        model="gpt-5",
        max_turns=5,
        delay_seconds=0.0,
        instructions_file=None,
        continue_prompt="continue working",
        done_confirmations=1,
        transcript=None,
    )

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(responses_loop, "parse_args", lambda argv=None: args)
    monkeypatch.setattr(responses_loop, "build_openai_client", lambda: fake_client)

    exit_code = responses_loop.main()

    assert exit_code == 0
    assert len(fake_client.calls) == 1
