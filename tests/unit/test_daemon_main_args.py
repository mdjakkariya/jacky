from autobot.config import Settings
from autobot.daemon.__main__ import _parse_args, _settings_from_args


def test_profile_and_port_flags() -> None:
    args = _parse_args(["--profile", "coder", "--port", "8766"])
    s = _settings_from_args(Settings(), args)
    assert s.profile == "coder"
    assert s.daemon_port == 8766


def test_defaults_keep_assistant() -> None:
    s = _settings_from_args(Settings(), _parse_args([]))
    assert s.profile == "assistant"
