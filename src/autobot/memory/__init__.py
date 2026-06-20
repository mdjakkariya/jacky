"""Persistent, on-device memory: the user's name and learned facts.

Phase 4. A single evolving profile stored in a local SQLite file, injected into
the model's context so Jack greets the user by name and personalizes, and grown
over time as the model saves durable facts via the memory tools. Nothing leaves
the machine.
"""

from __future__ import annotations
