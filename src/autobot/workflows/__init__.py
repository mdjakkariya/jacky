"""Workflows — deterministic, ordered tool-step recipes from ``WORKFLOW.md`` files.

A workflow is a sequence of tool calls defined declaratively in Markdown, parsed
into a :class:`WorkflowSpec` that can be executed by a :class:`WorkflowRegistry`.
"""

from __future__ import annotations
