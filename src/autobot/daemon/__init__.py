"""Headless daemon: a localhost API that streams engine state to UI clients.

The engine is headless (a golden rule); every UI — the floating orb, the
terminal client — is a thin client of this daemon. The daemon owns no logic: it
subscribes to an :class:`~autobot.core.events.EventBus` and forwards
``{state, amplitude}`` frames over a localhost WebSocket. See
``docs/plans/autobot_floating_orb_ui_plan.md``.
"""

from __future__ import annotations
