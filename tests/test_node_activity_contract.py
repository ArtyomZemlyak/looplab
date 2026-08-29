"""Writer-side guard for the public node-activity contract."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_every_engine_node_creation_promises_an_eval_start_receipt():
    """A new creation path may not silently collapse queued and evaluating back into pending."""

    calls: list[tuple[Path, ast.Call]] = []
    for path in (ROOT / "looplab" / "engine").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_emit_node_created"):
                calls.append((path, node))

    assert calls, "precondition: the shared node-created emitter has engine call sites"
    missing = []
    for path, call in calls:
        boundary = next((kw.value for kw in call.keywords
                         if kw.arg == "eval_start_boundary"), None)
        if not (isinstance(boundary, ast.Constant) and boundary.value is True):
            missing.append(f"{path.relative_to(ROOT)}:{call.lineno}")
    assert not missing, (
        "every engine-created lifecycle must promise node_eval_started before evaluation; missing: "
        + ", ".join(missing))
