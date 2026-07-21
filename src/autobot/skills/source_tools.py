"""The ``find_skill`` / ``install_skill`` tools: model-facing skill sourcing.

``find_skill`` searches the configured, whitelisted skill registries (see
``autobot.skills.source``) for a skill that provides a capability the model
doesn't already have. ``install_skill`` then installs the exact match into the
local skills folder, where the skill catalog's normal freshness check picks it
up.

Both tools are ``network=True``: they are the model-facing entry points onto
the one network egress seam skill sourcing has (cloning/fetching a whitelisted
git registry) — the disclosed, opt-in exception to on-device-only, like
``web_search``/``web_fetch``. ``find_skill`` sends only the search query off
the device; ``install_skill`` downloads the matched skill's files.
"""

from __future__ import annotations

from pathlib import Path

from autobot.core.types import ErrorCategory, Risk
from autobot.logging_setup import get_logger
from autobot.skills.source import SkillHit, SkillSource, SkillSourceError
from autobot.tools.registry import ToolFailure, ToolRegistry, ToolSpec

_log = get_logger("skills")


def _format_hits(hits: list[SkillHit]) -> str:
    """Render search hits as a short bulleted list, ending with an install hint."""
    lines = [f"- {hit.name} — {hit.description} (repo {hit.repo})" for hit in hits]
    lines.append('Install one with install_skill(name="<exact name>").')
    return "\n".join(lines)


def register_source_tools(registry: ToolRegistry, source: SkillSource, dest_root: Path) -> None:
    """Register ``find_skill`` and ``install_skill``, bound to ``source``.

    Args:
        registry: The tool registry to register into.
        source: The :class:`SkillSource` used to search and install skills, already
            configured with the whitelisted registries and cache directory.
        dest_root: The local skills directory that ``install_skill`` installs into.
    """

    def _find_skill(query: str) -> str:
        hits = source.search(query)
        _log.info("find_skill query=%r hits=%d", query, len(hits))
        if not hits:
            return f"No matching skill found in the configured skill registries for: {query}"
        return _format_hits(hits)

    def _install_skill(name: str) -> str:
        _log.info("install_skill name=%r", name)
        hits = source.search(name)
        hit = next((h for h in hits if h.name.lower() == name.lower()), None)
        if hit is None:
            return ToolFailure(
                f"no installable skill named {name!r} in the configured registries",
                ErrorCategory.NOT_FOUND,
            )
        try:
            source.install(hit, dest_root)
        except SkillSourceError as exc:
            return ToolFailure(str(exc))
        return (
            f"Installed skill {hit.name!r} from {hit.repo} at {hit.sha[:7]}. "
            "It's now available in the skills catalog."
        )

    registry.register(
        ToolSpec(
            name="find_skill",
            description=(
                "Search trusted skill registries for a skill that provides a capability you "
                "don't already have. Call this the moment a request needs an ability none of "
                "your current skills cover; then install the best match with install_skill. "
                "Sends only the search query off-device."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The capability to search for."}
                },
                "required": ["query"],
            },
            handler=_find_skill,
            risk=Risk.READ_ONLY,
            core=True,
            network=True,
        )
    )
    registry.register(
        ToolSpec(
            name="install_skill",
            description=(
                "Install a skill discovered via find_skill, by its exact name, from a trusted "
                "registry into the local skills folder. Downloads files off-device (gated)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Exact skill name, as returned by find_skill.",
                    }
                },
                "required": ["name"],
            },
            handler=_install_skill,
            risk=Risk.WRITE,
            network=True,
        )
    )
    _log.info("skill source tools registered")
