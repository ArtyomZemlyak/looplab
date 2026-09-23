"""An `async def` in `looplab/serve/` does no filesystem work on the event loop (review 2026-09-22).

Every `async def` handler in the server shares ONE event loop with every SSE stream and every other
async route. A synchronous filesystem call there stalls all of them for as long as the disk takes,
and on the FUSE/S3-backed run roots this project runs on that is SECONDS (`routers/boss.py`'s
chat-log handler measured it). The census below found 27 such calls in 20 async functions — the
run-dir resolvers (`srv.run_dir` reads the deletion fence and the reset marker) at 14 of them,
Replay's whole prologue (`validate_run_child`, the reset marker, `run_generation`'s event-log scan),
the settings/secret-store reads, a knowledge-note write and the review store's per-request file read
in the auth middleware — each now on a worker via `anyio.to_thread.run_sync`.

The rule is a CENSUS of named callees, not a proof: a disk read behind a name this list does not
know is invisible to it (as `validate_run_child` was to the first cut of this list). What it does
guarantee is that none of the shapes that were found can come back. A call inside a nested `def` or
`lambda` is exempt — that is the body handed to the worker — and so is anything `await`ed, which
yields rather than blocks.
"""
from __future__ import annotations

import ast
from pathlib import Path

import looplab
from _source_scan import iter_trees

SERVE = Path(looplab.__file__).resolve().parent / "serve"

# Callees that touch the disk (or take a blocking cross-process lock) when called. Resolver and
# loader names are this package's own; the rest are `pathlib.Path` I/O methods.
ON_LOOP_FORBIDDEN = frozenset({
    "_run_dir", "run_dir", "safe_run_dir", "validate_run_child", "_strict_existing_run",
    "load_run_reset_marker", "load_run_deletion_fence", "run_generation",
    "llm_settings", "global_settings", "load_ui_settings",
    "event_store", "read_all", "state", "fold", "sequence",
    "exists", "is_file", "is_dir", "stat", "lstat", "resolve", "iterdir", "glob", "rglob",
    "read_text", "read_bytes", "write_text", "write_bytes", "mkdir", "unlink", "open",
})


def _on_loop_calls(fn: ast.AsyncFunctionDef) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.Await):
            # The awaited call yields; its ARGUMENTS still evaluate on the loop.
            call = node.value
            if isinstance(call, ast.Call):
                stack.extend(call.args)
                stack.extend(kw.value for kw in call.keywords)
                if isinstance(call.func, ast.Attribute):
                    stack.append(call.func.value)
            else:
                stack.append(call)
            continue
        if isinstance(node, ast.Call):
            func = node.func
            name = (func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else None)
            if name in ON_LOOP_FORBIDDEN:
                hits.append((node.lineno, name))
        stack.extend(ast.iter_child_nodes(node))
    return hits


def _census(root: Path) -> list[str]:
    rows = []
    for path, tree in iter_trees(root):
        for fn in ast.walk(tree):
            if isinstance(fn, ast.AsyncFunctionDef):
                for line, name in _on_loop_calls(fn):
                    rows.append(f"{path.relative_to(root.parent)}:{line} {fn.name} -> {name}()")
    return sorted(rows)


def test_no_async_serve_function_touches_the_disk_on_the_loop():
    rows = _census(SERVE)
    assert not rows, (
        "a synchronous filesystem call on the event loop — move it into "
        "`await anyio.to_thread.run_sync(...)`:\n  " + "\n  ".join(rows))


def test_the_census_sees_each_shape_it_was_written_for(tmp_path):
    """Driven on a synthetic package: the shapes that were found are red, the fixed shapes are not."""
    pkg = tmp_path / "serve"
    pkg.mkdir()
    (pkg / "routes.py").write_text('''
import anyio

async def bad(srv, run_id, snap, reviews, token, request):
    rd = srv.run_dir(run_id)
    if not snap.exists():
        pass
    review = reviews.resolve(token)
    body = await request.json()
    await srv.send(srv.state(rd))

async def good(srv, run_id, snap, reviews, token, request):
    rd = await anyio.to_thread.run_sync(srv.run_dir, run_id)
    if not await anyio.to_thread.run_sync(snap.exists):
        pass
    review = await anyio.to_thread.run_sync(reviews.resolve, token)

    def _work():
        return srv.state(rd)

    return await anyio.to_thread.run_sync(_work)
''', encoding="utf-8")
    rows = _census(pkg)
    assert sorted(row.split(" -> ")[1] for row in rows) == [
        "exists()", "resolve()", "run_dir()", "state()"], rows
    assert all(" bad -> " in row for row in rows), rows
