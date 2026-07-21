"""Skill sourcing: types and state management for discovering and caching skills.

This module defines value types for skill sources (git repositories) and the skills
they contain, along with persistence mechanisms for tracking which skills came from
which sources.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from autobot.logging_setup import get_logger
from autobot.skills.spec import SkillError, spec_from_text

_log = get_logger("skills")

_GIT_TIMEOUT_S = 120
_MAX_SKILLS_SCANNED = 500


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


def _run_git(args: list[str]) -> str:
    """Run a git command and return its stdout.

    Any failure (non-zero exit, timeout, or a missing ``git`` executable) is
    re-raised as :class:`SkillSourceError` with a concise message — the caller
    never sees a raw ``subprocess`` traceback.

    Args:
        args: Arguments to pass to ``git`` (excluding the ``git`` executable itself).

    Returns:
        The command's stdout.

    Raises:
        SkillSourceError: If the command fails, times out, or git is missing.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except subprocess.CalledProcessError as exc:
        tail = "\n".join((exc.stderr or "").strip().splitlines()[-5:])
        raise SkillSourceError(f"git {' '.join(args)} failed: {tail}") from None
    except subprocess.TimeoutExpired:
        raise SkillSourceError(f"git {' '.join(args)} timed out after {_GIT_TIMEOUT_S}s") from None
    except FileNotFoundError:
        raise SkillSourceError("git executable not found; is git installed?") from None
    return proc.stdout


def _ensure_repo(repo: str, cache_dir: Path, *, whitelist: list[str]) -> tuple[Path, str]:
    """Shallow-clone (or update) a whitelisted git repo into a stable cache subdir.

    This is the sole network egress seam for skill sourcing: a repo is only ever
    contacted if it appears verbatim in ``whitelist`` (the configured
    ``skill_registries``). Everything else is rejected before any subprocess runs.

    On first use the repo is cloned with ``--depth 1``. On subsequent calls the
    existing clone is updated in place (``fetch`` + ``reset --hard FETCH_HEAD``,
    so a moved branch is picked up); if that update fails, the cache subdir is
    removed and re-cloned once.

    Args:
        repo: The git repository URI (or local path/``file://`` URL for tests).
        cache_dir: The directory under which per-repo clones are cached.
        whitelist: The allowed repo URIs (``settings.skill_registries``). ``repo``
            must appear here verbatim, or nothing is contacted.

    Returns:
        A tuple of ``(local_path, resolved_commit_sha)`` for the cached clone.

    Raises:
        SkillSourceError: If ``repo`` is not in ``whitelist``, or if any git
            operation fails.
    """
    if repo not in whitelist:
        raise SkillSourceError(f"repo not in skill_registries whitelist: {repo!r}")

    try:
        slug = hashlib.sha256(repo.encode()).hexdigest()[:16]
        cache_dir.mkdir(parents=True, exist_ok=True)
        dest = cache_dir / slug

        if not dest.exists():
            _run_git(["clone", "--depth", "1", "--", repo, str(dest)])
        else:
            try:
                _run_git(["-C", str(dest), "fetch", "--depth", "1", "origin", "HEAD"])
                _run_git(["-C", str(dest), "reset", "--hard", "FETCH_HEAD"])
            except SkillSourceError:
                shutil.rmtree(dest, ignore_errors=True)
                _run_git(["clone", "--depth", "1", "--", repo, str(dest)])

        sha = _run_git(["-C", str(dest), "rev-parse", "HEAD"]).strip()

        # Validate the resolved SHA is a valid 40-character hex string
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise SkillSourceError(f"unexpected git sha: {sha!r}")

        _log.info("skill source fetched repo=%r sha=%s", repo, sha[:7])
        return dest, sha
    except OSError as exc:
        raise SkillSourceError(f"skill cache setup failed: {exc}") from exc


def _relevance_score(tokens: list[str], name: str, description: str) -> int:
    """Count how many query tokens appear (case-insensitively) in name + description.

    Args:
        tokens: Lower-cased, whitespace-split query tokens.
        name: The skill's name.
        description: The skill's description.

    Returns:
        The number of tokens found as a substring of ``name + description``
        (lower-cased). Zero means no relevance.
    """
    haystack = f"{name} {description}".lower()
    return sum(1 for token in tokens if token in haystack)


class SkillSource:
    """Searches whitelisted git registries for skills matching a query.

    Each configured registry doubles as its own whitelist entry, so
    :func:`_ensure_repo` never contacts anything the caller didn't already
    configure via ``skill_registries``. A registry that fails to clone or update
    (unreachable, not a git repo, etc.) is skipped with a warning rather than
    failing the whole search — one bad registry must not block skills discovered
    from other, healthy registries.
    """

    def __init__(self, registries: list[str], cache_dir: Path) -> None:
        """Initialize with the configured registries and a shared cache directory.

        Args:
            registries: Whitelisted git repository URIs to search. Also passed as
                the whitelist to :func:`_ensure_repo`, so only these repos are
                ever contacted.
            cache_dir: Directory under which per-repo shallow clones are cached.
        """
        self._registries = registries
        self._cache_dir = cache_dir

    def search(self, query: str, *, limit: int = 5) -> list[SkillHit]:
        """Search all configured registries for skills relevant to ``query``.

        Args:
            query: Free-text search query, split on whitespace into tokens and
                matched case-insensitively against each skill's name +
                description.
            limit: The maximum number of hits to return.

        Returns:
            The top ``limit`` matching :class:`SkillHit` instances, ranked by
            relevance score (descending) then name (ascending). Returns an empty
            list if nothing matches or every registry is unreachable; never
            raises.
        """
        tokens = [token.lower() for token in query.split() if token]
        scored: list[tuple[int, SkillHit]] = []

        for repo in self._registries:
            try:
                repo_dir, sha = _ensure_repo(repo, self._cache_dir, whitelist=self._registries)
            except SkillSourceError as exc:
                _log.warning(
                    "skill search: skipping unreachable registry repo=%r error=%s", repo, exc
                )
                continue

            candidates = itertools.islice(repo_dir.rglob("SKILL.md"), _MAX_SKILLS_SCANNED)
            for md in candidates:
                if ".git" in md.parts:
                    continue

                try:
                    text = md.read_text(encoding="utf-8")
                    spec = spec_from_text(text, path=md, source="remote", strict=False)
                except (SkillError, OSError, UnicodeDecodeError):
                    continue

                score = _relevance_score(tokens, spec.name, spec.description)
                if score <= 0:
                    continue

                subpath = md.parent.relative_to(repo_dir).as_posix()
                hit = SkillHit(
                    name=spec.name,
                    description=spec.description,
                    repo=repo,
                    subpath=subpath,
                    sha=sha,
                )
                scored.append((score, hit))

        scored.sort(key=lambda pair: (-pair[0], pair[1].name))
        results = [hit for _, hit in scored[:limit]]
        _log.info("skill search query=%r hits=%d", query, len(results))
        return results
