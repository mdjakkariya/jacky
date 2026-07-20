"""Unit tests for the coder-stream event fan-out hub."""

from __future__ import annotations

from typing import Any

from autobot.daemon.coder_events import CoderEventHub


def test_publish_reaches_all_subscribers_and_unsubscribe_stops() -> None:
    hub = CoderEventHub()
    got_a: list[dict[str, Any]] = []
    got_b: list[dict[str, Any]] = []
    un_a = hub.subscribe(got_a.append)
    hub.subscribe(got_b.append)
    hub.publish({"type": "mcp_status", "server": "s1"})
    un_a()
    un_a()  # idempotent
    hub.publish({"type": "mcp_oauth", "server": "s1"})
    assert got_a == [{"type": "mcp_status", "server": "s1"}]
    assert len(got_b) == 2


def test_bad_subscriber_never_breaks_publish() -> None:
    hub = CoderEventHub()
    got: list[dict[str, Any]] = []

    def boom(evt: dict[str, Any]) -> None:
        raise RuntimeError("sink failed")

    hub.subscribe(boom)
    hub.subscribe(got.append)
    hub.publish({"type": "mcp_status"})
    assert got == [{"type": "mcp_status"}]
