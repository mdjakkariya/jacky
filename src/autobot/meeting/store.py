"""Per-meeting folder, manifest, retention, and crash recovery."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from autobot.logging_setup import get_logger

_log = get_logger("meeting")

ACTIVE_STATES = {"recording", "paused", "transcribing", "summarizing"}


@dataclass(frozen=True, slots=True)
class MeetingPaths:
    """Absolute paths for one meeting's artifacts."""

    id: str
    dir: str
    near_wav: str
    far_wav: str
    transcript_md: str
    minutes_md: str
    manifest_json: str


def _slug(title: str) -> str:
    """A filesystem-safe, lowercased slug from a title (empty -> 'meeting')."""
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s or "meeting"


class MeetingStore:
    """Creates/locates meeting folders and manages manifests + retention."""

    def __init__(self, meetings_dir: str, *, now: Callable[[], datetime] | None = None) -> None:
        self._root = Path(meetings_dir).expanduser()
        self._now = now or datetime.now

    def _paths(self, meeting_id: str) -> MeetingPaths:
        d = self._root / meeting_id
        return MeetingPaths(
            id=meeting_id,
            dir=str(d),
            near_wav=str(d / "near.wav"),
            far_wav=str(d / "far.wav"),
            transcript_md=str(d / "transcript.md"),
            minutes_md=str(d / "minutes.md"),
            manifest_json=str(d / "manifest.json"),
        )

    def create(self, title: str) -> MeetingPaths:
        """Create a new meeting folder named ``YYYY-MM-DD-HHMM-<slug>``."""
        stamp = self._now().strftime("%Y-%m-%d-%H%M")
        meeting_id = f"{stamp}-{_slug(title)}"
        paths = self._paths(meeting_id)
        Path(paths.dir).mkdir(parents=True, exist_ok=True)
        _log.info("meeting folder created id=%s", meeting_id)
        return paths

    def write_manifest(self, paths: MeetingPaths, data: dict[str, object]) -> None:
        """Write the manifest JSON atomically."""
        tmp = Path(paths.manifest_json + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(paths.manifest_json)

    def read_manifest(self, meeting_dir: str) -> dict[str, object]:
        """Read a manifest; ``{}`` if missing/malformed."""
        p = Path(meeting_dir) / "manifest.json"
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _all_dirs(self) -> list[Path]:
        if not self._root.exists():
            return []
        return sorted((d for d in self._root.iterdir() if d.is_dir()), reverse=True)

    def list_recent(self) -> list[dict[str, object]]:
        """All meetings' manifests, newest first (by folder name = timestamped id)."""
        return [m for d in self._all_dirs() if (m := self.read_manifest(str(d)))]

    def find_interrupted(self) -> list[str]:
        """Ids of meetings left in a non-terminal state (to finalize on startup)."""
        out: list[str] = []
        for d in self._all_dirs():
            state = str(self.read_manifest(str(d)).get("state", ""))
            if state in ACTIVE_STATES:
                out.append(d.name)
        return out

    def prune(self, keep: int) -> list[str]:
        """Delete oldest meetings beyond ``keep`` (never an active one)."""
        import shutil

        removed: list[str] = []
        dirs = self._all_dirs()  # newest first
        for d in dirs[keep:]:
            state = str(self.read_manifest(str(d)).get("state", ""))
            if state in ACTIVE_STATES:
                continue
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)
        if removed:
            _log.info("meeting retention pruned count=%d", len(removed))
        return removed
