"""Tests for secret redaction (redact_secrets) — pure regex, no I/O."""

from __future__ import annotations

from autobot.tools.code.redact import redact_secrets

_PLACEHOLDER = "«redacted»"


def test_plain_prose_is_unchanged() -> None:
    text = "The quick brown fox jumps over the lazy dog."
    out, count = redact_secrets(text)
    assert out == text
    assert count == 0


def test_empty_text_returns_unchanged() -> None:
    out, count = redact_secrets("")
    assert out == ""
    assert count == 0


def test_pem_private_key_block_is_redacted() -> None:
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"  # gitleaks:allow — synthetic fixture, not a real key
        "MIIEpAIBAAKCAQEA1c7otM8k9e8wJ4t9x1z0v6b6b6b6b6b6b6b6b6b6b6b6b6b6b\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out, count = redact_secrets(pem)
    assert _PLACEHOLDER in out
    assert "MIIEpAIBAAKCAQEA1c7otM8k9e8wJ4t9x1z0v6b6b6b6b6b6b6b6b6b6b6b6b6b6b" not in out
    assert count == 1


def test_aws_access_key_id_is_redacted() -> None:
    secret = "AKIA" + "IOSFODNN7EXAMPLE"  # split so no full key literal lives in source
    out, count = redact_secrets(f"my key is {secret} thanks")
    assert _PLACEHOLDER in out
    assert secret not in out
    assert count == 1


def test_github_token_is_redacted() -> None:
    secret = "ghp_" + "a" * 36
    out, count = redact_secrets(f"token: {secret}")
    assert _PLACEHOLDER in out
    assert secret not in out
    assert count >= 1


def test_openai_style_key_is_redacted() -> None:
    secret = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
    out, count = redact_secrets(f"OPENAI_API_KEY={secret}")
    assert _PLACEHOLDER in out
    assert secret not in out
    assert count >= 1


def test_slack_token_is_redacted() -> None:
    secret = "xoxb-" + "1234567890"
    out, count = redact_secrets(f"slack token {secret} in use")
    assert _PLACEHOLDER in out
    assert secret not in out
    assert count == 1


def test_google_api_key_is_redacted() -> None:
    secret = "AIza" + "S" * 35
    out, count = redact_secrets(f"key={secret}")
    assert _PLACEHOLDER in out
    assert secret not in out
    assert count >= 1


def test_bearer_token_is_redacted() -> None:
    secret_val = "abcDEF123456789012345"  # gitleaks:allow — synthetic fixture, not a real token
    text = f"Authorization: Bearer {secret_val}"
    out, count = redact_secrets(text)
    assert _PLACEHOLDER in out
    assert secret_val not in out
    assert count == 1


def test_generic_key_value_assignment_redacts_value_keeps_key_name() -> None:
    secret_val = "abcdef0123456789ZZZZ"  # gitleaks:allow — synthetic fixture, not a real value
    out, count = redact_secrets(f'api_key = "{secret_val}"')
    assert _PLACEHOLDER in out
    assert secret_val not in out
    assert "api_key" in out
    assert count == 1


def test_generic_password_assignment_redacts_value() -> None:
    secret_val = "Sup3rSecretPassw0rd12345"  # gitleaks:allow — synthetic fixture, not a real value
    out, count = redact_secrets(f"password: {secret_val}")
    assert _PLACEHOLDER in out
    assert secret_val not in out
    assert "password" in out
    assert count == 1


def test_count_reflects_number_of_redactions() -> None:
    first = "AKIA" + "IOSFODNN7EXAMPLE"  # split so no full key literal lives in source
    second = "AKIA" + "IOSFODNN7SECOND1"
    out, count = redact_secrets(f"{first} and also {second}")
    assert count == 2
    assert first not in out
    assert second not in out


def test_realistic_env_snippet_and_pem_block_both_redact() -> None:
    # Fixtures are synthetic and split across concatenation so no full token is a source
    # literal (keeps secret scanners quiet while the runtime string still exercises redaction).
    aws_key = "AKIA" + "ABCDEFGHIJKLMNOP"
    pem_body = "MIIBogIBAAJBAKj34" + "GkxFhD91assz7QSoYkCFHFR"
    snippet = (
        "# .env\n"
        "DATABASE_URL=postgres://user:pass@localhost/db\n"
        f"AWS_ACCESS_KEY_ID={aws_key}\n"
        "SECRET_TOKEN=zzzzzzzzzzzzzzzzzzzzzzzzz1234\n"
        "-----BEGIN PRIVATE KEY-----\n"
        f"{pem_body}\n"
        "-----END PRIVATE KEY-----\n"
    )
    out, count = redact_secrets(snippet)
    assert _PLACEHOLDER in out
    assert aws_key not in out
    assert "zzzzzzzzzzzzzzzzzzzzzzzzz1234" not in out
    assert pem_body not in out
    assert "SECRET_TOKEN" in out
    assert "AWS_ACCESS_KEY_ID" in out
    assert count >= 3


def test_never_raises_on_weird_input() -> None:
    out, count = redact_secrets("\x00\x01 not a secret �")
    assert isinstance(out, str)
    assert isinstance(count, int)
    assert count == 0
