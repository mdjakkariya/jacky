"""Tests for the generic async-task registry."""

from __future__ import annotations

from autobot.tasks.registry import Task, TaskRegistry


class _Clock:
    """A hand-cranked monotonic clock for deterministic started/finished stamps."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


def test_add_returns_running_task_with_minted_id() -> None:
    reg = TaskRegistry(now=_Clock())
    task = reg.add(kind="command", session_id="s1", label="pytest")
    assert task.id == "task-1"
    assert task.kind == "command"
    assert task.session_id == "s1"
    assert task.label == "pytest"
    assert task.status == "running"
    assert task.settled is False
    assert task.started == 1.0
    assert reg.get("task-1") == task


def test_ids_are_monotonic() -> None:
    reg = TaskRegistry(now=_Clock())
    a = reg.add(kind="command", session_id="s1", label="a")
    b = reg.add(kind="command", session_id="s1", label="b")
    assert (a.id, b.id) == ("task-1", "task-2")


def test_mark_done_updates_status_result_and_finished() -> None:
    reg = TaskRegistry(now=_Clock())
    reg.add(kind="command", session_id="s1", label="pytest")
    done = reg.mark_done("task-1", result="42 passed", returncode=0)
    assert done is not None
    assert done.status == "done"
    assert done.result == "42 passed"
    assert done.returncode == 0
    assert done.finished == 2.0
    assert done.settled is True
    # The stored row reflects the update.
    assert reg.get("task-1") == done


def test_mark_failed_records_failure() -> None:
    reg = TaskRegistry(now=_Clock())
    reg.add(kind="command", session_id="s1", label="build")
    failed = reg.mark_failed("task-1", result="exit 1", returncode=1)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.returncode == 1
    assert failed.settled is True


def test_finishing_unknown_task_returns_none() -> None:
    reg = TaskRegistry(now=_Clock())
    assert reg.mark_done("task-99", result="x") is None
    assert reg.mark_failed("task-99", result="x") is None
    assert reg.get("task-99") is None


def test_list_is_newest_first_and_scoped_by_session() -> None:
    reg = TaskRegistry(now=_Clock())
    reg.add(kind="command", session_id="s1", label="a")
    reg.add(kind="command", session_id="s2", label="b")
    reg.add(kind="command", session_id="s1", label="c")
    all_labels = [t.label for t in reg.list()]
    assert all_labels == ["c", "b", "a"]  # newest first
    s1_labels = [t.label for t in reg.list(session_id="s1")]
    assert s1_labels == ["c", "a"]


def test_running_count_scoped() -> None:
    reg = TaskRegistry(now=_Clock())
    reg.add(kind="command", session_id="s1", label="a")
    reg.add(kind="command", session_id="s1", label="b")
    reg.add(kind="command", session_id="s2", label="c")
    assert reg.running_count() == 3
    assert reg.running_count(session_id="s1") == 2
    reg.mark_done("task-1", result="ok", returncode=0)
    assert reg.running_count() == 2
    assert reg.running_count(session_id="s1") == 1


def test_eviction_drops_oldest_settled_but_keeps_running() -> None:
    reg = TaskRegistry(now=_Clock(), max_tasks=2)
    reg.add(kind="command", session_id="s1", label="a")  # task-1
    reg.add(kind="command", session_id="s1", label="b")  # task-2
    # task-1 is running (never settled), task-2 settled — adding a third evicts task-2.
    reg.mark_done("task-2", result="ok", returncode=0)
    reg.add(kind="command", session_id="s1", label="c")  # task-3 → over cap
    assert reg.get("task-1") is not None  # running: never evicted
    assert reg.get("task-2") is None  # oldest settled: evicted
    assert reg.get("task-3") is not None


def test_listener_fires_on_settle_with_the_updated_task() -> None:
    reg = TaskRegistry(now=_Clock())
    seen: list[Task] = []
    reg.add_listener(seen.append)
    reg.add(kind="command", session_id="s1", label="pytest")
    assert seen == []  # not fired on add
    reg.mark_done("task-1", result="ok", returncode=0)
    assert len(seen) == 1
    assert seen[0].id == "task-1" and seen[0].status == "done" and seen[0].returncode == 0


def test_listener_fires_on_failure_too() -> None:
    reg = TaskRegistry(now=_Clock())
    seen: list[str] = []
    reg.add_listener(lambda t: seen.append(t.status))
    reg.add(kind="command", session_id="s1", label="build")
    reg.mark_failed("task-1", result="boom", returncode=1)
    assert seen == ["failed"]


def test_unsubscribe_stops_delivery() -> None:
    reg = TaskRegistry(now=_Clock())
    seen: list[Task] = []
    unsubscribe = reg.add_listener(seen.append)
    reg.add(kind="command", session_id="s1", label="a")
    reg.add(kind="command", session_id="s1", label="b")
    reg.mark_done("task-1", result="ok", returncode=0)
    unsubscribe()
    reg.mark_done("task-2", result="ok", returncode=0)
    assert [t.id for t in seen] == ["task-1"]  # task-2 not delivered after unsubscribe


def test_a_raising_listener_never_breaks_the_update() -> None:
    reg = TaskRegistry(now=_Clock())
    good: list[str] = []

    def boom(_task: Task) -> None:
        raise RuntimeError("listener blew up")

    reg.add_listener(boom)
    reg.add_listener(lambda t: good.append(t.id))
    reg.add(kind="command", session_id="s1", label="a")
    done = reg.mark_done("task-1", result="ok", returncode=0)
    assert done is not None and done.status == "done"  # update still applied
    assert good == ["task-1"]  # the second listener still ran


def test_task_is_frozen() -> None:
    task = Task(id="task-1", kind="command", session_id="s1", label="x")
    try:
        task.status = "done"  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("Task should be immutable")
