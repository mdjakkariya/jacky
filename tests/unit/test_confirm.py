"""Tests for destructive-action confirmation: parser, inbox, and the voice/click flow."""

from __future__ import annotations

from autobot.tools.confirm import ConfirmInbox, VoiceConfirmer, parse_confirmation


def test_parse_confirmation_yes() -> None:
    for t in ["yes", "yeah", "sure", "proceed", "go ahead", "do it", "okay do it"]:
        assert parse_confirmation(t) is True, t


def test_parse_confirmation_no() -> None:
    for t in ["no", "nope", "cancel", "stop", "don't", "never mind", "wait"]:
        assert parse_confirmation(t) is False, t


def test_parse_confirmation_negation_wins() -> None:
    # A mixed answer must not be read as a yes for a destructive action.
    assert parse_confirmation("yes but actually no, wait") is False


def test_parse_confirmation_unclear_is_none() -> None:
    for t in ["", "um", "what was that", "the weather"]:
        assert parse_confirmation(t) is None, t
    # Whole-word match: "now" must not count as "no".
    assert parse_confirmation("right now") is None


# --- ConfirmInbox (click -> engine bridge) --------------------------------


def test_inbox_round_trips_one_answer() -> None:
    box = ConfirmInbox()
    assert box.take() is None  # empty
    box.submit(True)
    assert box.take() is True
    assert box.take() is None  # consumed


def test_inbox_keeps_first_answer_when_full() -> None:
    box = ConfirmInbox()
    box.submit(True)
    box.submit(False)  # ignored — one already pending
    assert box.take() is True


# --- VoiceConfirmer flow (fakes; no mic, no audio) ------------------------


class _Flow:
    def __init__(self, answers: list[str]) -> None:
        self.spoken: list[str] = []
        self.shown: list[str] = []
        self.cleared = 0
        self.now = 0.0
        self._answers = list(answers)

    def speak(self, text: str) -> None:
        self.spoken.append(text)

    def listen(self, _timeout: float) -> str:
        self.now += 1.0  # each chunk advances the clock by 1s
        return self._answers.pop(0) if self._answers else ""

    def clock(self) -> float:
        return self.now


def _voice(answers: list[str], timeout_s: float = 5.0) -> tuple[VoiceConfirmer, _Flow]:
    flow = _Flow(answers)
    confirmer = VoiceConfirmer(
        speak=flow.speak,
        listen=flow.listen,
        on_show=flow.shown.append,
        on_clear=lambda: setattr(flow, "cleared", flow.cleared + 1),
        timeout_s=timeout_s,
        clock=flow.clock,
    )
    return confirmer, flow


def test_confirm_flushes_pre_prompt_audio_before_listening() -> None:
    flushed = {"n": 0}
    flow = _Flow(["proceed"])
    c = VoiceConfirmer(
        speak=flow.speak,
        listen=flow.listen,
        flush=lambda: flushed.__setitem__("n", flushed["n"] + 1),
        clock=flow.clock,
    )
    assert c.confirm("Empty the Trash?") is True
    assert flushed["n"] == 1  # dropped pre-prompt audio so only the answer counts


def test_confirm_true_on_voice_yes_and_clears_card() -> None:
    c, flow = _voice(["proceed"])
    assert c.confirm("Empty the Trash?") is True
    assert flow.shown == ["Empty the Trash?"]
    assert flow.cleared == 1


def test_chat_mode_confirms_by_click_without_speaking() -> None:
    spoken: list[str] = []
    shown: list[str] = []
    polls = {"n": 0}

    def poll() -> bool | None:
        polls["n"] += 1
        return True if polls["n"] >= 2 else None  # drain sees None, then a click

    def no_listen(_t: float) -> str:
        raise AssertionError("chat mode must not listen on the mic")

    c = VoiceConfirmer(
        speak=spoken.append,
        listen=no_listen,
        on_show=shown.append,
        poll_click=poll,
        is_chat=lambda: True,
        timeout_s=10.0,
        clock=lambda: 0.0,
        sleep=lambda _s: None,
    )
    assert c.confirm("Empty the Trash?") is True
    assert spoken == []  # never spoke the prompt
    assert shown == ["Empty the Trash?"]  # the card was shown


def test_chat_mode_times_out_silently() -> None:
    spoken: list[str] = []
    t = {"v": 0.0}

    def clock() -> float:
        t["v"] += 1.0
        return t["v"]

    c = VoiceConfirmer(
        speak=spoken.append,
        listen=lambda _t: "",
        poll_click=lambda: None,
        is_chat=lambda: True,
        timeout_s=3.0,
        clock=clock,
        sleep=lambda _s: None,
    )
    assert c.confirm("Delete it?") is False
    assert spoken == []


def test_confirm_false_on_voice_no() -> None:
    c, _ = _voice(["no"])
    assert c.confirm("Delete it?") is False


def test_confirm_reprompts_once_then_times_out_to_cancel() -> None:
    c, flow = _voice(["uhh"], timeout_s=4.0)
    assert c.confirm("Empty the Trash?") is False
    assert any("yes or no" in s for s in flow.spoken)
    assert flow.cleared == 1


def test_confirm_times_out_on_silence() -> None:
    c, _ = _voice([], timeout_s=3.0)
    assert c.confirm("Empty the Trash?") is False


def test_confirm_resolves_on_card_click() -> None:
    # A stale click is drained at the start; the real click arrives a poll later.
    polls = {"n": 0}

    def poll() -> bool | None:
        polls["n"] += 1
        return True if polls["n"] >= 3 else None

    spoken: list[str] = []
    c = VoiceConfirmer(
        speak=spoken.append,
        listen=lambda _t: "",  # never any voice
        poll_click=poll,
        timeout_s=10.0,
        clock=lambda: 0.0,  # deadline never reached; the click resolves it
    )
    assert c.confirm("Empty the Trash?") is True
