"""Skill sourcing: types and state management for discovering and caching skills.

This module defines value types for skill sources (git repositories) and the skills
they contain, along with persistence mechanisms for tracking which skills came from
which sources.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class SkillSourceError(Exception):
    """Error in skill sourcing or source management."""

    pass


@dataclass(frozen=True, slots=True)
class SkillHit:
    """A skill discovered in a source repository.

    Attributes:
        name: The skill's canonical name (e.g., "web-search").
        description: A short description of what the skill does.
        repo: The git repository where the skill was found.
        subpath: The path within the repo to the skill's directory.
        sha: The git commit SHA at which this skill was discovered.
    """

    name: str
    description: str
    repo: str
    subpath: str
    sha: str


@dataclass(frozen=True, slots=True)
class SourcePin:
    """Tracks which source (repo + commit) a skill was installed from.

    Used to pin installed skills to a specific repo and git commit, enabling
    reproducible sourcing and updates.

    Attributes:
        repo: The git repository URI.
        sha: The git commit SHA this pin points to.
        subpath: The path within the repo where the skill lives.
    """

    repo: str
    sha: str
    subpath: str

    def to_json(self) -> str:
        """Serialize the pin to JSON.

        Returns:
            A JSON string representation of the pin's fields.
        """
        return json.dumps({"repo": self.repo, "sha": self.sha, "subpath": self.subpath})

    @classmethod
    def read(cls, directory: Path) -> SourcePin | None:
        """Load a SourcePin from directory/.jack-source.json.

        Returns None if the file is absent, malformed JSON, or missing required keys.
        Never raises an exception.

        Args:
            directory: The directory to search for .jack-source.json.

        Returns:
            A SourcePin instance if the file is valid, or None if it is absent,
            malformed, or missing keys.
        """
        pin_file = directory / ".jack-source.json"
        if not pin_file.exists():
            return None

        try:
            data = json.loads(pin_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        if not isinstance(data, dict):
            return None

        # Check for required keys
        if not all(key in data for key in ("repo", "sha", "subpath")):
            return None

        try:
            return cls(repo=data["repo"], sha=data["sha"], subpath=data["subpath"])
        except (KeyError, TypeError):
            return None

    def write(self, directory: Path) -> None:
        """Write this SourcePin to directory/.jack-source.json.

        Creates the directory if needed; overwrites any existing file.

        Args:
            directory: The directory where .jack-source.json will be written.
        """
        directory.mkdir(parents=True, exist_ok=True)
        pin_file = directory / ".jack-source.json"
        pin_file.write_text(self.to_json(), encoding="utf-8")
