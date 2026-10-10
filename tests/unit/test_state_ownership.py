"""Guard: an event's status may only be changed by ``services/event_state.transition``.

This is a static check over the source tree. Without it the state machine is a
convention; with it, a stray ``event.status = ...`` or bulk ``UPDATE events SET
status`` fails the build.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIRS = [
    ROOT / "packages" / "relay_core" / "relay_core",
    ROOT / "services" / "api" / "relay_api",
    ROOT / "services" / "worker" / "relay_worker",
]
OWNER = "services/event_state.py"
EVENT_NAMES = {"event", "evt", "e", "db_event", "row_event"}


def _sources() -> list[Path]:
    files = [p for d in SOURCE_DIRS for p in d.rglob("*.py") if "migrations" not in p.parts]
    assert len(files) > 30, "source tree not found"
    return files


def _targets(node: ast.AST) -> list[ast.expr]:
    if isinstance(node, ast.Assign):
        return node.targets
    if isinstance(node, ast.AugAssign | ast.AnnAssign):
        return [node.target]
    return []


def _mentions_update_event(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "update"
            and child.args
            and isinstance(child.args[0], ast.Name)
            and child.args[0].id == "Event"
        ):
            return True
    return False


def test_event_status_is_assigned_only_by_the_transition_function() -> None:
    offenders: list[str] = []
    for path in _sources():
        relative = path.as_posix()
        tree = ast.parse(path.read_text(), filename=relative)
        for node in ast.walk(tree):
            for target in _targets(node):
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "status"
                    and isinstance(target.value, ast.Name)
                    and target.value.id in EVENT_NAMES
                    and not relative.endswith(OWNER)
                ):
                    offenders.append(
                        f"{relative}:{node.lineno} assigns {target.value.id}.status directly"
                    )
            # update(Event).values(status=...) would bypass the state machine in bulk.
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "values"
                and any(keyword.arg == "status" for keyword in node.keywords)
                and _mentions_update_event(node.func.value)
            ):
                offenders.append(f"{relative}:{node.lineno} bulk-updates Event.status")
    assert offenders == []


def test_the_guard_itself_detects_violations() -> None:
    """A guard that cannot fail proves nothing: feed it code it must reject."""
    bad_assignment = ast.parse("def f(event):\n    event.status = 'DELIVERED'\n")
    found = [
        t
        for node in ast.walk(bad_assignment)
        for t in _targets(node)
        if isinstance(t, ast.Attribute)
        and t.attr == "status"
        and isinstance(t.value, ast.Name)
        and t.value.id in EVENT_NAMES
    ]
    assert len(found) == 1
    bulk = ast.parse(
        "session.execute(update(Event).where(Event.id == x).values(status='DELIVERED'))"
    )
    calls = [
        n
        for n in ast.walk(bulk)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "values"
    ]
    assert calls
    assert _mentions_update_event(calls[0].func.value)
    # And it does not flag legitimate, unrelated status fields.
    fine = ast.parse(
        "integration.status = 'paused'\nrow.status = 'published'\nsession.execute(update(OutboxMessage).values(status='cancelled'))"
    )
    assert not [
        t
        for node in ast.walk(fine)
        for t in _targets(node)
        if isinstance(t, ast.Attribute)
        and isinstance(t.value, ast.Name)
        and t.value.id in EVENT_NAMES
    ]
    other = [
        n
        for n in ast.walk(fine)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "values"
    ]
    assert not _mentions_update_event(other[0].func.value)


def test_transition_is_the_only_writer_in_its_own_module() -> None:
    tree = ast.parse((SOURCE_DIRS[0] / "services" / "event_state.py").read_text())
    writers = [
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        for node in ast.walk(fn)
        for t in _targets(node)
        if isinstance(t, ast.Attribute) and t.attr == "status"
    ]
    assert writers == ["transition"]
