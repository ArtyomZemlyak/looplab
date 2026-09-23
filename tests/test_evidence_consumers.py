"""EVERY tool loop that hands a model a toolset is REGISTERED: fenced, or exempt for a stated reason
(review 2026-09-22, TAT-02 — the root of it, RC-8 "the boundary is opt-in in every constructor").

The untrusted-evidence fence (`core/evidence.py::fence_untrusted`) is applied by ONE function,
`agents/tool_loop.py::drive_tool_loop`, and only when its caller passes `tool_result_label`. That is
deliberate — a prompt is a contract, so no loop grows a fence nobody decided on — and it is also why
the fence kept not arriving: the four judge wrappers could not carry the label at all, and once they
could, most of the other consumers turned out never to have asked. Nothing listed the call sites, so
a new loop over a candidate's code was one more bare consumer no test could see.

`core/evidence.py::EVIDENCE_CONSUMERS` lists them, and this file keeps the list TRUE both ways:

* the set of sites is DERIVED here by AST, never typed in: every call of `drive_tool_loop` and —
  transitively — of every function that passes one of its OWN parameters into one as the toolset
  (the four wrappers, `run_phase`, and every forwarder after them), minus the calls that pass no
  toolset at all (`None`, or the keyword left out). The site a row names is the one that DECIDES
  WHICH TOOLSET the loop gets, because that is what decides whose words come back;
* a derived site with no row is RED, and so is a row whose site is gone;
* a FENCED row names the test that proves it — for every row added with the registry, one that
  drives a real tool result through it with the envelope on and off — and that test must exist; an
  EXEMPT row carries its reason.

The derivation is a NAME graph (like the containment census's paid closure): a loop entry point
reached through an attribute (`obj.run_phase(...)`) is matched by the attribute's name, and a
toolset is classified by the call site's own argument, whatever helper built it. What it cannot see
is an entry point called through an alias (`loop = drive_tool_loop; loop(...)`), exactly as the
census cannot see a paid callable called through a local.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

import pytest

from _source_scan import PKG, iter_trees

from looplab.core.evidence import (EVIDENCE_CONSUMERS, EXEMPT, FENCED, EvidenceConsumer)

ROOT = PKG.parent
# The one function that applies the fence, and the position of its toolset argument.
LOOP = "drive_tool_loop"
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


class Site(NamedTuple):
    key: str        # "<module>::<qualname> -> <callee>", the registry's key spelling
    lineno: int


def _params(fn) -> tuple[list[str], list[str]]:
    args = fn.args
    return [a.arg for a in args.posonlyargs + args.args], [a.arg for a in args.kwonlyargs]


def _binding(fn, name: str):
    """How `fn` binds `name`: `"param"`, `"local"` (any assignment in its own body — nested defs
    are their own scopes), or None."""
    pos, kwonly = _params(fn)
    starred = [a.arg for a in (fn.args.vararg, fn.args.kwarg) if a is not None]
    if name in pos or name in kwonly or name in starred:
        return "param"
    todo = list(fn.body)
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            if getattr(node, "name", None) == name:
                return "local"
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id == name:
            return "local"
        todo.extend(ast.iter_child_nodes(node))
    return None


def _calls(tree) -> list[tuple[list, ast.Call]]:
    """Every call in `tree` with the ClassDef/def chain that encloses it (outermost first)."""
    out: list = []

    def visit(node, stack):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, stack + [child])
                continue
            if isinstance(child, ast.Call):
                out.append((stack, child))
            visit(child, stack)
    visit(tree, [])
    return out


def _callee(call: ast.Call):
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None


def _tools_arg(call: ast.Call, name: str, index):
    """The expression passed as the entry point's toolset, `None` for a call that passes none, and
    `...` when it cannot be told (a `**mapping` that may carry it) — treated as a consumer."""
    if index is not None and len(call.args) > index and not any(
            isinstance(a, ast.Starred) for a in call.args[:index + 1]):
        return call.args[index]
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    if any(kw.arg is None for kw in call.keywords) or any(
            isinstance(a, ast.Starred) for a in call.args):
        return ...
    return None


def derive(modules) -> tuple[dict[str, Site], dict[str, tuple[str, int]]]:
    """`({consumer key: Site}, {forwarder name: (param, positional index or None)})` over
    `modules` — an iterable of `(module path relative to looplab/, parsed tree)`."""
    calls = [(rel, stack, call) for rel, tree in modules for stack, call in _calls(tree)]
    entries: dict[str, tuple[str, int]] = {LOOP: ("tools", 1)}
    while True:
        consumers: dict[str, Site] = {}
        grew = False
        for rel, stack, call in calls:
            name = _callee(call)
            if name not in entries:
                continue
            param, index = entries[name]
            expr = _tools_arg(call, param, index)
            if expr is None or (isinstance(expr, ast.Constant) and expr.value is None):
                continue                                   # this call hands the loop no toolset
            if isinstance(expr, ast.Name):
                owner = next(((i, b) for i in range(len(stack) - 1, -1, -1)
                              if isinstance(stack[i], _DEFS)
                              for b in [_binding(stack[i], expr.id)] if b), None)
                if owner is not None and owner[1] == "param":
                    # A FORWARDER: the toolset is its caller's, so its callers are the sites.
                    i = owner[0]
                    fn = stack[i]
                    pos, _kwonly = _params(fn)
                    idx = pos.index(expr.id) if expr.id in pos else None
                    if (idx is not None and i > 0 and isinstance(stack[i - 1], ast.ClassDef)
                            and pos and pos[0] in ("self", "cls")):
                        idx -= 1                           # a method is called without its self
                    if fn.name not in entries:
                        entries[fn.name] = (expr.id, idx)
                        grew = True
                    continue
            qual = ".".join(node.name for node in stack) or "<module>"
            key = f"{rel}::{qual} -> {name}"
            consumers.setdefault(key, Site(key, call.lineno))
        if not grew:
            return consumers, entries


def _package_modules():
    return [(str(path.relative_to(PKG)).replace("\\", "/"), tree) for path, tree in iter_trees()]


# ------------------------------------------------------------------ the derivation's own truth table

def _derive_source(files: dict[str, str]):
    return derive([(rel, ast.parse(src)) for rel, src in files.items()])


def test_a_new_loop_over_a_toolset_is_a_site():
    consumers, _ = _derive_source({"a.py": (
        "def reads_run(state):\n"
        "    return drive_tool_loop(client, RunTools(state), msgs, spec)\n")})
    assert set(consumers) == {"a.py::reads_run -> drive_tool_loop"}


def test_a_call_that_hands_the_loop_no_toolset_is_not():
    consumers, _ = _derive_source({"a.py": (
        "def emit_only():\n"
        "    drive_tool_loop(client, None, msgs, spec)\n"
        "    structured_judge(client, msgs, Model)\n"          # keyword left out entirely
        "def forwards(client, tools):\n"
        "    return structured_judge(client, msgs, Model, tools=tools)\n"
        "def judge(client, msgs, model, *, tools=None):\n"
        "    return drive_tool_loop(client, tools, msgs, spec)\n")})
    assert consumers == {}


def test_a_forwarder_moves_the_site_to_whoever_built_the_toolset():
    """`agentic_text(client, tools, …)` passes its CALLER's toolset: the caller is the site, however
    many forwarders deep — including a method (called without `self`) and a nested closure."""
    consumers, entries = _derive_source({
        "w.py": ("def wrapper(client, tools, msgs):\n"
                 "    return drive_tool_loop(client, tools, msgs, spec)\n"),
        "m.py": ("class Judge:\n"
                 "    def verdict(self, evidence, tools=None):\n"
                 "        def once():\n"
                 "            return wrapper(c, tools, [])\n"
                 "        return once()\n"
                 "    def watch(self):\n"
                 "        tools = log_tools(self)\n"
                 "        return self.verdict('e', tools)\n"),
    })
    assert entries["wrapper"] == ("tools", 1) and entries["verdict"] == ("tools", 1)
    assert set(consumers) == {"m.py::Judge.watch -> verdict"}


def test_a_toolset_behind_a_mapping_spread_counts_as_a_site():
    """A `**kwargs` that MAY carry the toolset cannot be read statically: it is a site, so a human
    decides, rather than a guess that it is empty."""
    consumers, _ = _derive_source({"a.py": "def f(kw):\n    drive_tool_loop(c, **kw)\n"})
    assert set(consumers) == {"a.py::f -> drive_tool_loop"}


# ------------------------------------------------------------------ the registry against the tree

@pytest.fixture(scope="module")
def derived():
    return derive(_package_modules())


def test_every_site_that_hands_a_loop_a_toolset_is_registered(derived):
    consumers, _ = derived
    missing = sorted(set(consumers) - set(EVIDENCE_CONSUMERS))
    assert not missing, (
        "tool-loop call sites with no `core/evidence.py::EVIDENCE_CONSUMERS` row — decide whether "
        "their tool results can carry a candidate's, a repository's or a third party's text; if "
        "so, fence them under `Settings.evidence_envelope` (the site's one switch reader) and name "
        "the test that drives it; if not, register them EXEMPT with the reason:\n  "
        + "\n  ".join(f"{k}  (line {consumers[k].lineno})" for k in missing))


def test_no_row_names_a_site_that_is_gone(derived):
    consumers, _ = derived
    stale = sorted(set(EVIDENCE_CONSUMERS) - set(consumers))
    assert not stale, ("EVIDENCE_CONSUMERS rows whose call site no longer hands a loop a toolset "
                       "(moved, renamed or deleted) — re-point or delete them:\n  "
                       + "\n  ".join(stale))


def test_the_derivation_still_reaches_the_forwarders_it_was_built_for(derived):
    """Non-vacuity: a scan that silently stopped following forwarders would pass both guards above
    over a smaller, wrong set. These are the names the product's loops actually go through."""
    consumers, entries = derived
    assert {"run_phase", "agentic_text", "agentic_struct", "emit_loop",
            "structured_judge"} <= set(entries)
    assert len(consumers) >= 25


def _test_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name for node in ast.walk(tree) if isinstance(node, _DEFS)}


def test_every_row_is_fenced_with_a_driving_test_or_exempt_with_a_reason():
    problems = []
    names_by_file: dict[str, set[str]] = {}
    for key, row in sorted(EVIDENCE_CONSUMERS.items()):
        if not isinstance(row, EvidenceConsumer) or row.status not in (FENCED, EXEMPT):
            problems.append(f"{key}: status must be FENCED or EXEMPT, got {row!r}")
            continue
        if len(row.why.strip()) < 40:
            problems.append(f"{key}: say what its tools return and where its switch comes from")
        if row.status == EXEMPT:
            if row.proof:
                problems.append(f"{key}: an EXEMPT row names no fencing test")
            continue
        test_file, _, test_name = row.proof.partition("::")
        path = ROOT / test_file
        if not (test_file.startswith("tests/") and path.is_file() and test_name):
            problems.append(f"{key}: proof {row.proof!r} is not `tests/<file>.py::<test>`")
            continue
        names = names_by_file.setdefault(test_file, _test_names(path))
        if test_name not in names:
            problems.append(f"{key}: proof test {test_name!r} is not in {test_file}")
    assert not problems, "\n".join(problems)
