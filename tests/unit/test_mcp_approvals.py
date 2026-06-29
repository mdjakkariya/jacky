"""Unit tests for autobot.mcp.approvals — pure stdlib, no SDK, no real home dir."""

from __future__ import annotations

import json
from pathlib import Path

from autobot.mcp.approvals import (
    ApprovalsFile,
    SpawnApproval,
    load_approvals,
    record_fingerprints,
    record_spawn_approval,
    save_approvals,
)

# ---------------------------------------------------------------------------
# Round-trip: save → load
# ---------------------------------------------------------------------------


def test_round_trip_fingerprints_and_spawn_approvals(tmp_path: Path) -> None:
    """save_approvals followed by load_approvals returns equal data."""
    p = tmp_path / "approved.json"
    af = ApprovalsFile(
        fingerprints={"server-a": {"server-a__tool1": "abc123", "server-a__tool2": "def456"}},
        spawn_approvals={
            "server-b": SpawnApproval(
                command="/usr/local/bin/mcp-server",
                args=["--port", "9000"],
                approved_at="2025-01-01T00:00:00+00:00",
            )
        },
    )
    save_approvals(af, p)
    loaded = load_approvals(p)
    assert loaded.fingerprints == af.fingerprints
    assert loaded.spawn_approvals["server-b"].command == af.spawn_approvals["server-b"].command
    assert loaded.spawn_approvals["server-b"].args == af.spawn_approvals["server-b"].args
    assert (
        loaded.spawn_approvals["server-b"].approved_at == af.spawn_approvals["server-b"].approved_at
    )


# ---------------------------------------------------------------------------
# load_approvals edge cases
# ---------------------------------------------------------------------------


def test_load_approvals_missing_file_returns_empty(tmp_path: Path) -> None:
    """load_approvals on a non-existent path returns an empty ApprovalsFile."""
    p = tmp_path / "does_not_exist.json"
    result = load_approvals(p)
    assert result.fingerprints == {}
    assert result.spawn_approvals == {}


def test_load_approvals_malformed_json_returns_empty(tmp_path: Path) -> None:
    """load_approvals on malformed JSON returns an empty ApprovalsFile (no exception)."""
    p = tmp_path / "approved.json"
    p.write_text("THIS IS NOT JSON {{{", encoding="utf-8")
    result = load_approvals(p)
    assert result.fingerprints == {}
    assert result.spawn_approvals == {}


def test_load_approvals_empty_json_object_returns_empty(tmp_path: Path) -> None:
    """load_approvals on a valid but empty JSON object returns empty ApprovalsFile."""
    p = tmp_path / "approved.json"
    p.write_text("{}", encoding="utf-8")
    result = load_approvals(p)
    assert result.fingerprints == {}
    assert result.spawn_approvals == {}


# ---------------------------------------------------------------------------
# record_fingerprints merges (doesn't clobber other servers/tools)
# ---------------------------------------------------------------------------


def test_record_fingerprints_merges_without_clobbering(tmp_path: Path) -> None:
    """record_fingerprints merges new tools into existing data for other servers/tools."""
    p = tmp_path / "approved.json"
    # Seed with server-a data
    initial = ApprovalsFile(
        fingerprints={"server-a": {"server-a__tool1": "fp_orig"}},
    )
    save_approvals(initial, p)

    # Record fingerprints for server-b — server-a must be preserved
    record_fingerprints("server-b", {"server-b__new_tool": "fp_new"}, p)

    loaded = load_approvals(p)
    assert loaded.fingerprints["server-a"]["server-a__tool1"] == "fp_orig"
    assert loaded.fingerprints["server-b"]["server-b__new_tool"] == "fp_new"


def test_record_fingerprints_merges_within_same_server(tmp_path: Path) -> None:
    """record_fingerprints adds new tools to an existing server entry, keeps old ones."""
    p = tmp_path / "approved.json"
    record_fingerprints("srv", {"srv__alpha": "fp_a"}, p)
    record_fingerprints("srv", {"srv__beta": "fp_b"}, p)

    loaded = load_approvals(p)
    assert loaded.fingerprints["srv"]["srv__alpha"] == "fp_a"
    assert loaded.fingerprints["srv"]["srv__beta"] == "fp_b"


def test_record_fingerprints_updates_existing_tool_fingerprint(tmp_path: Path) -> None:
    """record_fingerprints overwrites the fingerprint for a tool that changed."""
    p = tmp_path / "approved.json"
    record_fingerprints("srv", {"srv__tool": "old_fp"}, p)
    record_fingerprints("srv", {"srv__tool": "new_fp"}, p)

    loaded = load_approvals(p)
    assert loaded.fingerprints["srv"]["srv__tool"] == "new_fp"


# ---------------------------------------------------------------------------
# record_spawn_approval
# ---------------------------------------------------------------------------


def test_record_spawn_approval_writes_command_and_args(tmp_path: Path) -> None:
    """record_spawn_approval persists command and args correctly."""
    p = tmp_path / "approved.json"
    record_spawn_approval("my-server", "/path/to/cmd", ["--flag", "value"], p)

    loaded = load_approvals(p)
    assert "my-server" in loaded.spawn_approvals
    sa = loaded.spawn_approvals["my-server"]
    assert sa.command == "/path/to/cmd"
    assert sa.args == ["--flag", "value"]


def test_record_spawn_approval_approved_at_is_non_empty_iso(tmp_path: Path) -> None:
    """record_spawn_approval sets approved_at to a non-empty ISO-format timestamp."""
    p = tmp_path / "approved.json"
    record_spawn_approval("srv", "cmd", [], p)

    loaded = load_approvals(p)
    approved_at = loaded.spawn_approvals["srv"].approved_at
    assert approved_at  # non-empty
    # Must be parseable as an ISO 8601 datetime; raises ValueError if not
    from datetime import datetime

    parsed = datetime.fromisoformat(approved_at)
    assert parsed.tzinfo is not None  # timezone-aware


def test_record_spawn_approval_is_idempotent(tmp_path: Path) -> None:
    """Calling record_spawn_approval twice overwrites the first entry cleanly."""
    p = tmp_path / "approved.json"
    record_spawn_approval("srv", "old_cmd", ["a"], p)
    record_spawn_approval("srv", "new_cmd", ["b", "c"], p)

    loaded = load_approvals(p)
    sa = loaded.spawn_approvals["srv"]
    assert sa.command == "new_cmd"
    assert sa.args == ["b", "c"]


# ---------------------------------------------------------------------------
# File permission: mode 0600
# ---------------------------------------------------------------------------


def test_saved_file_is_mode_0600(tmp_path: Path) -> None:
    """save_approvals sets the file to 0600 (owner read/write only)."""
    p = tmp_path / "approved.json"
    save_approvals(ApprovalsFile(), p)
    mode_str = oct(p.stat().st_mode)[-3:]
    assert mode_str == "600"


def test_record_fingerprints_resulting_file_is_mode_0600(tmp_path: Path) -> None:
    """record_fingerprints leaves the file at 0600."""
    p = tmp_path / "approved.json"
    record_fingerprints("srv", {"srv__t": "fp"}, p)
    assert oct(p.stat().st_mode)[-3:] == "600"


# ---------------------------------------------------------------------------
# Structural: ApprovalsFile is a plain dataclass (no SDK import)
# ---------------------------------------------------------------------------


def test_approvals_file_has_expected_fields() -> None:
    """ApprovalsFile instantiates with default factories and correct field names."""
    af = ApprovalsFile()
    assert af.fingerprints == {}
    assert af.spawn_approvals == {}


def test_spawn_approval_stores_fields() -> None:
    """SpawnApproval stores command, args, approved_at as given."""
    sa = SpawnApproval(command="cmd", args=["a", "b"], approved_at="2025-01-01T00:00:00+00:00")
    assert sa.command == "cmd"
    assert sa.args == ["a", "b"]
    assert sa.approved_at == "2025-01-01T00:00:00+00:00"


def test_saved_json_is_valid_and_sorted(tmp_path: Path) -> None:
    """save_approvals writes valid JSON with sort_keys=True."""
    p = tmp_path / "approved.json"
    af = ApprovalsFile(
        fingerprints={"srv": {"srv__z": "zfp", "srv__a": "afp"}},
    )
    save_approvals(af, p)
    raw = json.loads(p.read_text(encoding="utf-8"))
    keys = list(raw["fingerprints"]["srv"].keys())
    assert keys == sorted(keys)
