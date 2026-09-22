"""Containment made countable (doc 52 row 14; doc 50 XP-03).

The house exception posture is contain-and-continue, and until this census nothing MEASURED it:
670 blind handlers under `looplab/`, 64 with no annotation at all, 103 with a `# noqa: BLE001` and
no reason, and no linter configured anywhere — so the annotations documented nothing, and a
contained failure left no mark on the span it happened in. Four rules, each driven below:

1. THE ALLOW-LIST IS THE ANNOTATION. Every blind handler that does not re-raise carries
   `# noqa: BLE001 — <why this is safe to contain>`. The 103 that still say nothing are listed in
   `tests/data/containment_unreviewed.txt`, keyed by `path::qualname#ordinal`, and that list may
   only SHRINK: reviewing a site means writing its reason and deleting its row, and a NEW blind
   handler with no reason is red rather than one more line of cargo.
2. THE PAID-CALL FUNNEL. Every blind handler around a paid call in the run path re-raises
   `BudgetExceeded` first — a swallowed spend stop lets a run keep billing past the limit set to
   stop it, which is what `verifier.py::verify` did at a SELECTION site (doc 50 AG-01). "Paid" is
   TRANSITIVE (review 2026-09-22): a closure over the call graph, not a list of callee names —
   see the block comment at section 2 for how it is resolved and what it cannot see.
3. `contain(reason, exc)` stamps the enclosing span and counts, and refuses the budget stop.
4. `looplab timings` reports the count, and a run that contained nothing is byte-identical.

The census is re-derived by AST here, so the suite needs no `ruff`; `[tool.ruff]` with `BLE` is
the command-line twin (`python -m ruff check looplab`) and is pinned to stay configured.
"""
from __future__ import annotations

import ast
import functools
import re
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

import pytest

from _source_scan import PKG, iter_trees

from looplab.core import tracing
from looplab.core.containment import (
    CONTAINED_ATTR, CONTAINED_EVENT, contain, containment_counts, reset_containment_counts)
from looplab.core.llm import BudgetExceeded

ROOT = PKG.parent
UNREVIEWED = ROOT / "tests" / "data" / "containment_unreviewed.txt"
HAS_REASON = re.compile(r"noqa:\s*BLE001\s*[-—–:]\s*\S")
HAS_NOQA = re.compile(r"noqa:\s*BLE001")
# The run path: where a paid call's BudgetExceeded is the operator's spend ceiling ending the run.
# `serve/` is deliberately outside it — its loops answer one HTTP request under their own error
# envelope, and a budget stop there is surfaced by that envelope, not by ending a run.
RUN_PATH = ("engine", "agents", "adapters", "search", "trust", "tools")
PAID = frozenset({"complete", "complete_text", "forced_structured", "drive_tool_loop",
                  "agentic_text", "structured_judge", "run_phase", "_pilot_emit"})


def _is_blind(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    if t is None:
        return True
    names = list(t.elts) if isinstance(t, ast.Tuple) else [t]
    return any(getattr(n, "id", getattr(n, "attr", "")) in ("Exception", "BaseException")
               for n in names)


def _is_budget(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    if t is None:
        return False
    names = list(t.elts) if isinstance(t, ast.Tuple) else [t]
    return any(getattr(n, "id", getattr(n, "attr", "")) == "BudgetExceeded" for n in names)


def _reraises(handler: ast.ExceptHandler) -> bool:
    return any(isinstance(n, ast.Raise) for n in ast.walk(handler))


def _called(nodes) -> set[str]:
    out: set[str] = set()
    for node in nodes:
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                out.add(getattr(n.func, "attr", None) or getattr(n.func, "id", None) or "")
    return out


def _blind_handlers():
    """Every blind, non-re-raising handler under `looplab/` with its stable key and source line."""
    for path, tree in iter_trees():
        lines = path.read_text(encoding="utf-8").splitlines()
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        def qualname(node) -> str:
            parts = []
            cur = parents.get(node)
            while cur is not None:
                if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    parts.append(cur.name)
                cur = parents.get(cur)
            return ".".join(reversed(parts)) or "<module>"

        ordinal: dict = {}
        for handler in ast.walk(tree):
            if not isinstance(handler, ast.ExceptHandler) or not _is_blind(handler):
                continue
            if _reraises(handler):
                continue
            q = qualname(handler)
            k = ordinal.get(q, 0)
            ordinal[q] = k + 1
            rel = path.relative_to(ROOT).as_posix()
            yield f"{rel}::{q}#{k}", rel, handler.lineno, lines[handler.lineno - 1]


def _unreviewed() -> list[str]:
    return [ln.strip() for ln in UNREVIEWED.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ------------------------------------------------------------------ 1. the allow-list

def test_every_blind_handler_states_its_reason_or_is_listed_unreviewed():
    """MUTATION: add `except Exception: pass` anywhere under looplab/ -> red, naming the site and
    the two ways out (a reason, or — for a site you are not reviewing today — its key here)."""
    listed = set(_unreviewed())
    offenders = []
    for key, rel, lineno, line in _blind_handlers():
        if HAS_REASON.search(line) or key in listed:
            continue
        offenders.append(f"{rel}:{lineno} [{key}]  {line.strip()[:80]}")
    assert not offenders, (
        "blind handler(s) with no stated reason. Write `# noqa: BLE001 — <why this is safe to "
        "contain>` on the except line, or add the key to tests/data/containment_unreviewed.txt "
        "if it is a pre-existing site you are deliberately not reviewing yet:\n  "
        + "\n  ".join(offenders))


def test_every_blind_handler_is_annotated_at_all():
    """The linter's own rule, re-derived without the linter: no bare blind except survives."""
    bare = [f"{rel}:{lineno}" for key, rel, lineno, line in _blind_handlers()
            if not HAS_NOQA.search(line)]
    assert not bare, bare


def test_the_unreviewed_list_only_shrinks_and_names_live_sites():
    """A row whose site now carries a reason (or is gone) is STALE and must be deleted — the list
    is the backlog of the review, and a backlog that keeps closed rows is the drift this repo's
    open-item rules exist to end. MUTATION: write a reason at a listed site without deleting its
    row -> red."""
    live = {key: line for key, _rel, _lineno, line in _blind_handlers()}
    rows = _unreviewed()
    assert len(rows) == len(set(rows)), "duplicate rows"
    stale = [r for r in rows if r not in live or HAS_REASON.search(live[r])]
    assert not stale, "delete these rows from tests/data/containment_unreviewed.txt:\n  " + "\n  ".join(stale)
    # The ratchet: the number here is the size of the review backlog on 2026-09-06 and may go
    # DOWN with every review; a larger list is a new site slipped in under the old ones.
    assert len(rows) <= 103, len(rows)


def test_a_reason_is_text_and_not_a_bare_dash():
    """`# noqa: BLE001 —` with nothing after it would satisfy a lazier regex."""
    assert not HAS_REASON.search("except Exception:  # noqa: BLE001 —")
    assert not HAS_REASON.search("except Exception:  # noqa: BLE001")
    assert HAS_REASON.search("except Exception:  # noqa: BLE001 — best-effort cleanup")
    assert HAS_REASON.search("except Exception:  # noqa: BLE001 - best-effort cleanup")


# ------------------------------------------------------------------ 2. the paid-call funnel
#
# PAID IS TRANSITIVE (review 2026-09-22: TAT-01, SCJ-03, ENG1-09, ENG3-02, RTA-04, ENG2-12). This
# funnel used to key on a hand list of eight callee names, so a blind handler around any WRAPPER of
# a paid call — `parse_structured`, `agentic_struct`, `verify`, a Researcher's `propose`, a steward,
# a tagger, the summarizer, the embedder — was invisible to it, and CLAUDE.md's "every blind handler
# around a paid call re-raises BudgetExceeded first" was false at 38 sites (a `foresight.rank` whose
# client raised the ceiling returned None; the training watchdog re-invoked a budget-stopped judge
# 300 times in 5 s). Paid-ness is now a CLOSURE computed here, by AST, over `looplab/`.
#
# WHY NOT PURE NAME-TRANSITIVITY — measured on 2026-09-22: closing "calls a name that is paid" over
# every def marks 4,416 of 5,925 defined names paid and flags 265 handlers, led by `get` (63),
# `append` (44) and `read_all` (25), because `PromptStore.get` happens to reach a paid name through
# a helper. So the graph is resolved BY KIND, which is what those collisions were made of:
#   * a bare call `f()` reaches module-level functions named `f`; `mod.f()` on an imported MODULE
#     the same; `x.m()` reaches METHODS named `m` (`SearchFitness(...).rank()` is not
#     `foresight.rank`); `self.m()` reaches the method this FILE defines when it defines one
#     (`ShellTools._run` is not `LLMRepoDeveloper._run`);
#   * a nested def counts for its enclosing function only where that function CALLS it or HANDS it
#     on — a factory returning a paying closure (`_stage_check_fn`, `chat_completer`) spends
#     nothing by being called; the closure pays where it is invoked, under its parameter name;
#   * a reference handed to a REFERENCE_INVOKER (`run_sync`, `partial`, the engine's offloads) is a
#     call — which is how much of the paid engine work actually runs;
#   * PAID_CALLBACKS are the paid callables a parameter carries into code that must not import a
#     model; they mark the try-blocks that call or hand them on, and never the carrier itself
#     (else every subprocess launch is paid, because `run_argv` CAN be handed a deadline judge);
#   * NOT_PROPAGATED names are too generic to carry paid-ness by name, and each lists the paid defs
#     it hides — a NEW paid def under one of them is a red test, so the list IS the review;
#   * an alias is a def: `_consolidate_lessons_file = staticmethod(LessonMemory.consolidate_...)`.
# On the tree it landed on (2026-09-22) the closure is 341 keys over 186 names, 101 try-blocks in
# FUNNEL_SCOPE reach it (the eight-name census saw 23, in RUN_PATH alone), and it flagged 38 blind
# handlers: 33 fixed in the same change, 4 owned by other findings and 1 reviewed false positive
# (FUNNEL_BACKLOG). It remains a NAME graph: a paid callable held in an attribute or a local and
# invoked as `obj(...)` (the `LLMAbstractor` behind `KnowledgeTools.abstract`, the `steward(final)`
# loop in `engine/finalize.py::finalize_run`) is invisible, and so is dispatch by `getattr`.

# The run path: where a paid call's BudgetExceeded is the operator's spend ceiling ending the run —
# plus `core/` and `runtime/`, which the eight-name census never looked at although both host blind
# handlers around paid callables (the summarizer, the stage checker, the deadline judge).
FUNNEL_SCOPE = RUN_PATH + ("core", "runtime")
# Defs that may JOIN the closure: everything the funnel scope can call. `serve/` and `cli/` sit
# above the engine (it may not import them), so nothing in the run path can reach their defs.
DEF_SCOPE = FUNNEL_SCOPE + ("events",)
# A call to one of these IS a provider request (`core/llm.py`'s client surface, the embedder's
# batch call), or is one of the eight names this census started from — kept so it can only ever
# see MORE than the one it replaced. Matched by name, whatever the receiver (the summarizer calls
# a bound `complete_text` held in a local). NOT `probe`: the preflight's 4-token request runs in
# `cli/__init__.py::_engine` before `Engine.__init__` seeds the run's prior spend, so its fresh
# accountant cannot be at the ceiling, and `probe` is a local holding a non-model hook in two
# other places (`engine/resources.py::_task_gpu_capable`, `core/pathsafe.py`).
PROVIDER_ENTRY_POINTS = frozenset({"complete_text", "complete_tool", "chat", "complete_text_stream",
                                   "embed_many"})
PAID = PROVIDER_ENTRY_POINTS | frozenset({
    "complete", "forced_structured", "drive_tool_loop", "agentic_text", "structured_judge",
    "run_phase", "_pilot_emit"})
# Paid callables carried in by a parameter: the stage checker and the deadline judge the engine
# hands `runtime/` (`command_eval._call_stage_check`, `sandbox._granted_grace`), and the history
# summarizer `core/context_budget.py::compact_history` is handed by the tool loop.
PAID_CALLBACKS = frozenset({"check_fn", "on_deadline", "summarize"})
# Helpers that INVOKE a callable they are handed — a reference passed to one is a call.
REFERENCE_INVOKERS = frozenset({
    "run_sync", "partial", "start_soon", "submit", "_offload_build", "_offload_node_build",
    "_offload_under_proposal_sink", "_offload_cadence", "_run_developer"})
# name -> (why the name cannot carry paid-ness, the paid defs it hides). Reviewed 2026-09-22.
NOT_PROPAGATED: dict[str, tuple[str, frozenset[str]]] = {
    "__init__": (
        "construction is `Cls(...)`, a call that names the CLASS, so a paid constructor is "
        "unreachable by name either way — and as a method name `super().__init__()` would make "
        "every such call site paid",
        frozenset({"looplab/tools/knowledge_tools.py::KnowledgeTools.__init__"})),
    "__call__": (
        "a callable object is invoked as `obj(...)`, which names the object; an explicit "
        "`.__call__(` never appears in the tree",
        frozenset({"looplab/adapters/repo_developer.py::LLMOnboarder.__call__",
                   "looplab/tools/memora.py::LLMAbstractor.__call__",
                   "looplab/tools/vectorstore.py::LLMEmbedder.__call__"})),
    "add": (
        "`set.add` is everywhere; the paid `CaseLibrary.add` embeds its case, and embeddings "
        "are outside the spend ceiling today (review 2026-09-22 CORE-01)",
        frozenset({"looplab/engine/memory.py::CaseLibrary.add"})),
    "bind_state": (
        "the optional ToolProvider hook every provider may implement; only the knowledge "
        "index's builds (and embeds) anything",
        frozenset({"looplab/tools/knowledge_tools.py::KnowledgeTools.bind_state"})),
    "execute": (
        "the ToolProvider protocol method and `sqlite3.Connection.execute`; a tool's own "
        "`execute` is checked where it is DEFINED (KnowledgeTools.execute is fixed), and the "
        "tool loop lets a raise from any tool propagate",
        frozenset({"looplab/tools/knowledge_tools.py::KnowledgeTools.execute"})),
    "run": (
        "`subprocess.run`, `anyio.run`, the sandboxes' `run`; `Engine.run` IS the run, and its "
        "stop is `cli/run_cmds.py::_run_engine_guarded`'s, read through `budget_stop_leaf`",
        frozenset({"looplab/engine/orchestrator.py::Engine.run"})),
}
# Blind handlers the census FINDS and this change did not re-point, each owned by a finding whose
# fix is bigger than a re-raise: every one sits in a build or producer lane where a raise tears
# down a task group of sibling builds, or skips the reservation cleanup the handler exists for, so
# the right fix is the deferred-stop sink those findings describe. SHRINK-ONLY: fixing a site means
# deleting its row, and a stale row is red. The one false positive is marked as such.
FUNNEL_BACKLOG: dict[str, str] = {
    # `Engine._create_node_guarded#0` (ENG1-01) and `Engine._serve_forced_requests#0` (ENG1-07)
    # left this list the same day: the guarded build and the inject lane both re-raise the stop now
    # (8dd61cb9, fa1b1415), so the census stopped finding them.
    "looplab/engine/speculation.py::SpeculationMixin._produce_requested_card#0":
        "review 2026-09-22 ENG2-02: a Card producer in a task group turns the stop into a give-up "
        "result; needs the run-level deferred-stop sink",
    "looplab/engine/speculation.py::SpeculationMixin._prepare_raw_card_stage#0":
        "review 2026-09-22 ENG2-02: the raw-proposal stage, same lane and same sink",
    "looplab/engine/speculation.py::SpeculationMixin._claim_requested_card_build#0":
        "FALSE POSITIVE, reviewed: `_create_node(precoded=...)` routes to `_create_precoded_node`, "
        "which this census's own closure finds unpaid; `_create_node` is paid on its other branch",
}
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
_TRIES = (ast.Try,) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())


class _Scope(NamedTuple):
    rel: str                  # "looplab/engine/x.py"
    aliases: frozenset        # names this file binds to a MODULE
    methods: frozenset        # method names the classes of this file define


def _module_aliases(tree: ast.AST, root: Path) -> frozenset:
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out.update((a.asname or a.name).split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            base = root.joinpath(*n.module.split("."))
            out.update(a.asname or a.name for a in n.names
                       if (base / f"{a.name}.py").is_file() or (base / a.name).is_dir())
    return frozenset(out)


def _name_of(node) -> str:
    return getattr(node, "attr", None) or getattr(node, "id", None) or ""


def _target(node, scope: _Scope):
    """What a call target — or a reference handed to an invoker — resolves to, BY KIND."""
    if isinstance(node, ast.Name):
        return ("f", node.id)
    if isinstance(node, ast.Attribute):
        owner = node.value
        if isinstance(owner, ast.Name) and owner.id in scope.aliases:
            return ("f", node.attr)
        if isinstance(owner, ast.Name) and owner.id == "self" and node.attr in scope.methods:
            return ("self", scope.rel, node.attr)
        return ("m", node.attr)
    return None


def _reaches(nodes, scope: _Scope) -> set:
    out: set = set()
    for n in nodes:
        if not isinstance(n, ast.Call):
            continue
        name = _name_of(n.func)
        if name in PAID:
            out.add(("paid", name))
        if name in PAID_CALLBACKS:
            out.add(("callback", name))
        target = _target(n.func, scope)
        if target is not None:
            out.add(target)
        for arg in (*n.args, *(k.value for k in n.keywords)):
            handed = _name_of(arg) if isinstance(arg, (ast.Name, ast.Attribute)) else ""
            if handed in PAID_CALLBACKS:
                out.add(("callback", handed))
            if handed and name in REFERENCE_INVOKERS:
                if handed in PAID:
                    out.add(("paid", handed))
                target = _target(arg, scope)
                if target is not None:
                    out.add(target)
    return out


def _own_nodes(fn):
    """`fn`'s body without the bodies of the functions and classes defined inside it."""
    stack = list(fn.body)
    while stack:
        n = stack.pop()
        if isinstance(n, (*_FUNCS, ast.ClassDef)):
            continue
        yield n
        stack.extend(ast.iter_child_nodes(n))


def _inner_defs(fn):
    stack = list(fn.body)
    while stack:
        n = stack.pop()
        if isinstance(n, _FUNCS):
            yield n
        elif not isinstance(n, ast.ClassDef):
            stack.extend(ast.iter_child_nodes(n))


def _def_reach(fn, scope: _Scope) -> set:
    own = list(_own_nodes(fn))
    reach = _reaches(own, scope)
    used = {_name_of(n.func) for n in own if isinstance(n, ast.Call)}
    used |= {a.id for n in own if isinstance(n, ast.Call)
             for a in (*n.args, *(k.value for k in n.keywords)) if isinstance(a, ast.Name)}
    for inner in _inner_defs(fn):
        if inner.name in used:
            reach |= _def_reach(inner, scope)
    return reach


def _pays(reach: set, closure) -> bool:
    return any(r[0] == "paid" for r in reach) or not reach.isdisjoint(closure)


@functools.lru_cache(maxsize=None)
def _paid_census(pkg: Path = PKG):
    """`(defs, closure, files)`: every def in DEF_SCOPE with what calling it reaches, the paid
    closure (key -> the def that made it paid), and every parsed file with its scope."""
    root = pkg.parent
    defs: dict = defaultdict(list)
    files = []
    for path, tree in iter_trees(pkg):
        rel = path.relative_to(root).as_posix()
        parts = path.relative_to(pkg).parts
        top = parts[0] if len(parts) > 1 else ""
        methods = frozenset(ch.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
                            for ch in n.body if isinstance(ch, _FUNCS))
        scope = _Scope(rel, _module_aliases(tree, root), methods)
        files.append((rel, top, tree, scope))
        if top not in DEF_SCOPE:
            continue

        def index(name, qual, in_class, reach):
            row = (f"{rel}::{'.'.join(qual + [name])}", reach)
            defs[("m" if in_class else "f", name)].append(row)
            if in_class:
                defs[("self", rel, name)].append(row)

        def visit(node, qual, in_class):
            for ch in ast.iter_child_nodes(node):
                if isinstance(ch, ast.ClassDef):
                    visit(ch, qual + [ch.name], True)
                elif isinstance(ch, _FUNCS):
                    index(ch.name, qual, in_class, _def_reach(ch, scope))
                elif isinstance(ch, ast.Assign):
                    # AN ALIAS IS A DEF. `_consolidate_lessons_file = staticmethod(LessonMemory.
                    # consolidate_lessons_file)` on the Engine and `_run_argv = run_argv` are how a
                    # paid function gets a second name; a caller of the second name reaches it.
                    value = ch.value
                    if (isinstance(value, ast.Call) and _name_of(value.func) in (
                            "staticmethod", "classmethod") and len(value.args) == 1):
                        value = value.args[0]
                    target = (_target(value, scope)
                              if isinstance(value, (ast.Name, ast.Attribute)) else None)
                    if target is not None:
                        for name in (t.id for t in ch.targets if isinstance(t, ast.Name)):
                            index(name, qual, in_class, {target})
                else:
                    visit(ch, qual, in_class)

        visit(tree, [], False)
    closure: dict = {}
    grew = True
    while grew:
        grew = False
        for key, rows in defs.items():
            if key in closure or (key[0] in ("f", "m") and key[1] in NOT_PROPAGATED):
                continue
            for where, reach in rows:
                if _pays(reach, closure):
                    closure[key] = where
                    grew = True
                    break
    return defs, closure, files


def _refuses_budget_stop(handler: ast.ExceptHandler) -> bool:
    """The handler routes the exception through `contain(reason, exc)` or `refuse_budget_stop(exc)`,
    both of which re-raise the ceiling (`core/containment.py`)."""
    for n in ast.walk(handler):
        if isinstance(n, ast.Call):
            name = _name_of(n.func)
            if name == "contain" and (len(n.args) >= 2 or any(k.arg == "exc" for k in n.keywords)):
                return True
            if name == "refuse_budget_stop" and (n.args or n.keywords):
                return True
    return False


def _funnel_scan(pkg: Path = PKG):
    """`(paid try-blocks, offenders)` over FUNNEL_SCOPE. An offender is a blind handler whose
    try-body reaches a paid call, with no earlier handler that names `BudgetExceeded` (re-raising
    it, or deciding about it on purpose, as the watchdogs do) and no refusal inside it."""
    _defs, closure, files = _paid_census(pkg)
    paid_tries = 0
    offenders: list[tuple[str, str, int]] = []
    for rel, top, tree, scope in files:
        if top not in FUNNEL_SCOPE:
            continue
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

        def qualname(node) -> str:
            parts, cur = [], parents.get(node)
            while cur is not None:
                if isinstance(cur, (*_FUNCS, ast.ClassDef)):
                    parts.append(cur.name)
                cur = parents.get(cur)
            return ".".join(reversed(parts)) or "<module>"

        ordinal: dict = {}
        for node in ast.walk(tree):
            if not isinstance(node, _TRIES):
                continue
            reach = _reaches([n for stmt in node.body for n in ast.walk(stmt)], scope)
            if not (_pays(reach, closure) or any(r[0] == "callback" for r in reach)):
                continue
            paid_tries += 1
            decided = False
            for handler in node.handlers:
                if _is_budget(handler):
                    decided = True
                elif (_is_blind(handler) and not decided and not _reraises(handler)
                      and not _refuses_budget_stop(handler)):
                    q = qualname(handler)
                    k = ordinal.get(q, 0)
                    ordinal[q] = k + 1
                    offenders.append((f"{rel}::{q}#{k}", rel, handler.lineno))
    return paid_tries, offenders


def test_every_blind_handler_around_a_paid_call_in_the_run_path_reraises_the_budget_stop():
    """THE FUNNEL (doc 50 AG proposal 2), transitive since 2026-09-22. MUTATION: delete the
    `except BudgetExceeded: raise` above any site — `search/foresight.py::rank`, a steward, the
    stage checker — or add a blind `except` around a call to ANY function that reaches a provider
    -> red, naming the site. `resilient` / `forced_structured` / `contain` are the same rule as
    functions: their blind handlers route through `refuse_budget_stop`."""
    _count, offenders = _funnel_scan()
    new = [f"{rel}:{line} [{key}]" for key, rel, line in offenders if key not in FUNNEL_BACKLOG]
    assert not new, (
        "a blind `except` around a paid call swallows BudgetExceeded — add `except BudgetExceeded: "
        "raise` BEFORE it, or route the exception through `core/containment.py::contain(reason, "
        "exc)` (which re-raises it):\n  " + "\n  ".join(new))
    stale = sorted(set(FUNNEL_BACKLOG) - {key for key, _rel, _line in offenders})
    assert not stale, "these FUNNEL_BACKLOG rows no longer name an offender — delete them:\n  " + (
        "\n  ".join(stale))


def test_the_funnel_backlog_only_shrinks():
    """The number here is the backlog on 2026-09-22 and may only go DOWN; a larger list is a new
    swallow parked beside the old ones instead of fixed."""
    assert len(FUNNEL_BACKLOG) <= 3, len(FUNNEL_BACKLOG)


def test_every_paid_def_under_a_generic_name_is_classified():
    """THE CLASSIFICATION GUARD. A name in NOT_PROPAGATED stops the closure, so a paid function
    defined under it would silently exempt every caller. Each row therefore lists the paid defs it
    hides, both ways: a NEW paid def under a generic name is red until it is reviewed (list it, or
    rename it), and a listed def that no longer pays is a stale row. MUTATION: make any other
    `execute`/`add`/`run` reach a provider -> red."""
    defs, closure, _files = _paid_census(PKG)
    problems = []
    for name, (_why, reviewed) in NOT_PROPAGATED.items():
        found = {where for kind in ("f", "m") for where, reach in defs.get((kind, name), ())
                 if _pays(reach, closure) or any(r[0] == "callback" for r in reach)}
        if found != reviewed:
            problems.append(f"{name}: unreviewed {sorted(found - reviewed)}, "
                            f"stale {sorted(reviewed - found)}")
    assert not problems, "\n".join(problems)


def test_the_funnel_census_is_not_vacuous_and_sees_every_site_the_hand_list_saw():
    """The guard proves nothing if the closure lost its spine, and it must see a SUPERSET of what the
    eight-name census saw (23 paid try-blocks on 2026-09-22) — a resolution rule that drops one of
    those is a regression, not a refinement."""
    _defs, closure, files = _paid_census(PKG)
    names = {key[-1] for key in closure}
    for wrapper in ("parse_structured", "agentic_struct", "forced_structured", "verify",
                    "structured_judge", "rank", "tag_text_llm", "consolidate", "propose", "decide",
                    "research", "_training_verdict", "_asha_verdict", "embed",
                    "_maybe_merge_hypotheses", "finalize_run"):
        assert wrapper in names, f"{wrapper} fell out of the paid closure"
    count, _offenders = _funnel_scan()
    assert count >= 100, count
    legacy = frozenset({"complete", "complete_text", "forced_structured", "drive_tool_loop",
                        "agentic_text", "structured_judge", "run_phase", "_pilot_emit"})
    missed = []
    for rel, top, tree, scope in files:
        if top not in RUN_PATH:
            continue
        for node in ast.walk(tree):
            if isinstance(node, _TRIES) and _called(node.body) & legacy:
                reach = _reaches([n for stmt in node.body for n in ast.walk(stmt)], scope)
                if not _pays(reach, closure):
                    missed.append(f"{rel}:{node.lineno}")
    assert not missed, missed


# The scanner itself, driven on a synthetic tree: every resolution rule above has a case that
# would flip if the rule were wrong, and the expected offender set is exact.
_SYNTHETIC = {
    "core/client.py": """
class Client:
    def complete_text(self, messages):
        return "x"
""",
    "search/wrap.py": """
from looplab.core.errors import BudgetExceeded


def ask(client):                      # level 1: a provider entry point
    return client.complete_text([])


def ask_twice(client):                # level 2: paid only through the closure
    return ask(client)


def rank(client):                     # a paid MODULE function named like a method below
    return ask(client)


def factory(client):                  # returns a paying closure; building it spends nothing
    def _paying():
        return client.complete_text([])
    return _paying


def wrapper_calls_inner(client):      # calls its paying closure: paid
    def _inner():
        return ask(client)
    return _inner()


class Fitness:
    def rank(self, nodes):            # NOT the paid `rank`: a method, reached as `x.rank()`
        return sorted(nodes)


class Shell:
    def _run(self):
        return 1

    def execute(self):
        try:
            return self._run()        # `self._run` is THIS file's unpaid `_run`, not Dev's
        except Exception:  # noqa: BLE001 — synthetic
            return None


def swallows_transitively(client):    # OFFENDER
    try:
        return ask_twice(client)
    except Exception:  # noqa: BLE001 — synthetic
        return None


def reraises_first(client):           # clean
    try:
        return ask_twice(client)
    except BudgetExceeded:
        raise
    except Exception:  # noqa: BLE001 — synthetic
        return None


def decides_on_purpose(client):       # clean: naming the ceiling IS the decision
    try:
        return ask_twice(client)
    except BudgetExceeded:
        return "stopped"
    except Exception:  # noqa: BLE001 — synthetic
        return None


def routes_through_contain(client):   # clean
    try:
        return ask_twice(client)
    except Exception as exc:  # noqa: BLE001 — synthetic
        contain("synthetic", exc)
        return None


def contain_without_the_exception(client):   # OFFENDER: contain(reason) cannot re-raise
    try:
        return ask_twice(client)
    except Exception:  # noqa: BLE001 — synthetic
        contain("synthetic")
        return None


def only_builds_the_closure(client):  # clean
    try:
        return factory(client)
    except Exception:  # noqa: BLE001 — synthetic
        return None


def calls_the_wrapper(client):        # OFFENDER: the nested def is called, so the wrapper pays
    try:
        return wrapper_calls_inner(client)
    except Exception:  # noqa: BLE001 — synthetic
        return None


def uses_the_method(nodes):           # clean: a method `rank` is not the module function
    try:
        return Fitness().rank(nodes)
    except Exception:  # noqa: BLE001 — synthetic
        return None


def uses_a_generic_name(store, row):  # clean: `add` does not carry paid-ness by name
    try:
        store.add(row)
    except Exception:  # noqa: BLE001 — synthetic
        pass


def hands_it_to_a_worker(client):     # OFFENDER: a reference handed to an invoker is a call
    try:
        return anyio.to_thread.run_sync(ask_twice, client)
    except Exception:  # noqa: BLE001 — synthetic
        return None


def calls_a_paid_callback(check_fn):  # OFFENDER: a paid callable carried in by a parameter
    try:
        return check_fn("stage", "tail")
    except Exception:  # noqa: BLE001 — synthetic
        return None


def hands_on_a_paid_callback(check_fn):   # OFFENDER: handing it on is calling it here
    try:
        return helper(check_fn)
    except Exception:  # noqa: BLE001 — synthetic
        return None


def carries_a_callback(check_fn):     # carrying it does not make THIS function paid
    return check_fn("stage", "tail")


def calls_the_carrier():              # clean
    try:
        return carries_a_callback(None)
    except Exception:  # noqa: BLE001 — synthetic
        return None


class Engine:
    _aliased = staticmethod(ask_twice)    # an alias is a def

    def uses_the_alias(self, client):     # OFFENDER: reached through its second name
        try:
            return self._aliased(client)
        except Exception:  # noqa: BLE001 — synthetic
            return None
""",
    "engine/memory.py": """
class Tools:
    def add(self, client):            # a paid def under a NOT_PROPAGATED name
        from looplab.search.wrap import ask
        return ask(client)
""",
    "adapters/dev.py": """
from looplab.search.wrap import ask


class Dev:
    def _run(self, client):           # a PAID `_run` — in another file than `Shell._run`
        return ask(client)
""",
    "serve/route.py": """
def outside_the_run_path(client):    # clean: serve/ answers its own request envelope
    try:
        return client.complete_text([])
    except Exception:  # noqa: BLE001 — synthetic
        return None
""",
}


def test_the_census_scanner_on_a_synthetic_tree(tmp_path):
    """Every rule of the transitive funnel, driven. MUTATIONS each of these catches: resolve
    `x.m()` to module functions too (`uses_the_method` flips), fold a returned closure into its
    factory (`only_builds_the_closure`), drop same-file `self.` resolution (`Shell.execute`),
    propagate through a callback carrier (`calls_the_carrier`), ignore invokers
    (`hands_it_to_a_worker`), ignore aliases (`Engine.uses_the_alias`), accept `contain(reason)`
    without the exception, or accept only a RE-RAISING budget handler (`decides_on_purpose`)."""
    pkg = tmp_path / "looplab"
    for rel, text in _SYNTHETIC.items():
        path = pkg / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        (path.parent / "__init__.py").touch()
        path.write_text(text.lstrip(), encoding="utf-8")
    (pkg / "__init__.py").touch()
    _paid_census.cache_clear()
    try:
        count, offenders = _funnel_scan(pkg)
    finally:
        _paid_census.cache_clear()
    flagged = {key.split("::", 1)[1] for key, _rel, _line in offenders}
    assert flagged == {
        "swallows_transitively#0", "contain_without_the_exception#0", "calls_the_wrapper#0",
        "hands_it_to_a_worker#0", "calls_a_paid_callback#0", "hands_on_a_paid_callback#0",
        "Engine.uses_the_alias#0",
    }, sorted(flagged)
    assert all(rel == "looplab/search/wrap.py" for _key, rel, _line in offenders)
    # The seven offenders + the three that decide about the ceiling (re-raise, name it, `contain`).
    # The factory, the method, the generic name, the carrier and `Shell.execute` are NOT paid.
    assert count == 10, count


# ------------------------------------------------------------------ 3. contain()

def test_contain_counts_and_never_raises_for_an_ordinary_failure():
    reset_containment_counts()
    try:
        raise ValueError("boom")
    except ValueError as exc:
        contain("unit test", exc)
    contain("unit test")
    assert containment_counts() == {"unit test": 2}


def test_contain_refuses_the_budget_stop():
    """Adopting the helper at a site is adopting the funnel."""
    with pytest.raises(BudgetExceeded):
        try:
            raise BudgetExceeded("spend ceiling")
        except BudgetExceeded as exc:
            contain("would swallow the budget stop", exc)


def test_contain_stamps_the_enclosing_span(tmp_path):
    """Driven through a real Tracer: the count and the event land on the durable span record."""
    from looplab.core.tracing import JsonlSpanExporter, Tracer

    tr = Tracer(JsonlSpanExporter(tmp_path / "spans.jsonl"), run_id="r")
    with tr.span("operation", kind="operation") as sp:
        try:
            raise RuntimeError("secret sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234")
        except RuntimeError as exc:
            contain("driven", exc)
            contain("driven again", exc)
        assert sp.attributes[CONTAINED_ATTR] == 2
    tr.shutdown()
    rows = [__import__("json").loads(ln) for ln in (tmp_path / "spans.jsonl").read_text().splitlines()]
    row = next(r for r in rows if r.get("name") == "operation")
    assert row["attributes"][CONTAINED_ATTR] == 2
    events = [e for e in row["events"] if e["name"] == CONTAINED_EVENT]
    assert [e["reason"] for e in events] == ["driven", "driven again"]
    assert all(e["exc"] == "RuntimeError" for e in events)


def test_contain_is_a_no_op_stamp_outside_any_span():
    assert tracing.current_span_handle() is None
    contain("nowhere")           # must not raise


def test_resilient_counts_its_containments():
    from looplab.agents.tool_loop import resilient

    reset_containment_counts()
    assert resilient(lambda: 1 / 0, lambda: "safe", reason="strategist") == "safe"
    assert containment_counts() == {"strategist": 1}
    with pytest.raises(BudgetExceeded):
        resilient(lambda: (_ for _ in ()).throw(BudgetExceeded("stop")), lambda: "safe")


# A spend stop rarely arrives bare where it is contained. An `anyio` task group wraps whatever
# escaped it (`ExceptionGroup`), and a handler that translates a failure chains the original under
# its own. `core/errors.py::budget_stop_leaf` is the one reading of "is the ceiling in here", and
# the CLI's `run_finished {"reason": "budget_exhausted"}` and the eval drain already ask it; the
# containment helpers asked `isinstance(exc, BudgetExceeded)` and so absorbed both shapes into
# their fallback (review 2026-09-22, CORE-07).
def _grouped_stop() -> BaseException:
    return ExceptionGroup("task group", [ValueError("sibling"), BudgetExceeded("spend ceiling")])


def _chained_stop() -> BaseException:
    try:
        try:
            raise BudgetExceeded("spend ceiling")
        except BudgetExceeded as inner:
            raise RuntimeError("translated by a middle layer") from inner
    except RuntimeError as exc:
        return exc


_WRAPPED_STOPS = pytest.mark.parametrize("make", [_grouped_stop, _chained_stop],
                                         ids=["exception-group", "chained"])


@_WRAPPED_STOPS
def test_contain_refuses_a_wrapped_budget_stop(make):
    """MUTATION: restore `isinstance(exc, BudgetExceeded)` in `_is_budget_stop` -> red."""
    stop = make()
    with pytest.raises(type(stop)) as raised:
        contain("would swallow a wrapped budget stop", stop)
    assert raised.value is stop          # the object in flight, group and chain intact


@_WRAPPED_STOPS
def test_resilient_refuses_a_wrapped_budget_stop(make):
    """`resilient`'s own `except BudgetExceeded: raise` cannot match a group or a translation; its
    blind handler reaches `contain`, which must refuse — never the fallback."""
    from looplab.agents.tool_loop import resilient

    stop = make()
    fallbacks: list = []

    def attempt():
        raise stop

    with pytest.raises(type(stop)):
        resilient(attempt, lambda: fallbacks.append("fallback"))
    assert fallbacks == []


@_WRAPPED_STOPS
def test_forced_structured_refuses_a_wrapped_budget_stop(make, monkeypatch):
    """The salvage's `on_fail` is a paid re-ask at two of its three callers; a wrapped ceiling must
    end the run here exactly as a bare one does."""
    from pydantic import BaseModel

    from looplab.core import parse as parse_mod

    class _Out(BaseModel):
        x: int = 0

    stop = make()

    def _raise(*_a, **_k):
        raise stop

    monkeypatch.setattr(parse_mod, "parse_structured", _raise)
    salvaged: list = []
    with pytest.raises(type(stop)):
        parse_mod.forced_structured(object(), [], _Out, "tool_call",
                                    on_fail=lambda exc: salvaged.append(exc))
    assert salvaged == []


def test_an_ordinary_failure_still_degrades_through_every_containment_helper(monkeypatch):
    """The other half of the rule: only the CEILING is refused. A group of ordinary failures and an
    ordinary chain still reach their fallbacks."""
    from pydantic import BaseModel

    from looplab.agents.tool_loop import resilient
    from looplab.core import parse as parse_mod
    from looplab.core.containment import refuse_budget_stop

    ordinary = ExceptionGroup("task group", [ValueError("a"), OSError("b")])
    contain("ordinary group", ordinary)                      # must not raise
    refuse_budget_stop(ordinary)                             # must not raise
    refuse_budget_stop(None)                                 # must not raise

    def attempt():
        raise ordinary

    assert resilient(attempt, lambda: "safe") == "safe"

    class _Out(BaseModel):
        x: int = 0

    def _raise(*_a, **_k):
        raise ordinary

    monkeypatch.setattr(parse_mod, "parse_structured", _raise)
    assert parse_mod.forced_structured(object(), [], _Out, "tool_call",
                                       on_fail=lambda exc: "salvaged") == "salvaged"


# ------------------------------------------------------------------ 4. timings

def _timings_output(tmp_path, spans: list[dict]) -> str:
    import json

    from typer.testing import CliRunner

    from looplab.cli import app

    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "spans.jsonl").write_text("\n".join(json.dumps(s) for s in spans) + "\n")
    return CliRunner().invoke(app, ["timings", str(run_dir)]).output


def _span(**over) -> dict:
    base = {"span_id": "a", "trace_id": "t", "name": "evaluate", "kind": "operation",
            "start": 1.0, "duration_s": 2.0, "attributes": {"node_id": 1}, "events": []}
    base.update(over)
    return base


def test_timings_reports_contained_failures_and_is_byte_identical_without_them(tmp_path):
    clean = _timings_output(tmp_path / "clean", [_span()])
    assert "contained failures" not in clean
    stamped = _timings_output(tmp_path / "stamped", [_span(
        attributes={"node_id": 1, CONTAINED_ATTR: 2},
        events=[{"name": CONTAINED_EVENT, "reason": "watchdog tick", "exc": "AttributeError"},
                {"name": CONTAINED_EVENT, "reason": "watchdog tick", "exc": "AttributeError"}])])
    assert "contained failures: 2 across 1 span(s)" in stamped
    assert "2 × watchdog tick (AttributeError)" in stamped
    # Everything before the roll-up is the historical report byte for byte.
    assert stamped.split("\ncontained failures")[0] == clean


# ------------------------------------------------------------------ the linter is configured

def test_the_linter_is_configured_for_exactly_this_rule():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.ruff]" in text and "[tool.ruff.lint]" in text
    assert re.search(r'select\s*=\s*\["BLE"\]', text), "BLE is the one rule; no style rule is enabled"
    assert re.search(r'"ruff>=[0-9.]+"', text), "ruff belongs to the dev extras"
    assert not (ROOT / ".ruff.toml").exists(), "one config home, pyproject"


def test_the_number_claude_md_states_is_the_number_this_census_derives():
    """THE COUNT COMES FROM THE PARSER, NEVER A PERSON — CLAUDE.md's own rule, applied to the one
    number in CLAUDE.md that describes THIS census. It said 670 while the tree held 715: written
    once by hand, wrong within a day, and read by every agent turn before a single file is opened.

    A guard and not a re-derivation at read time, because the sentence has to be readable as prose;
    what must not happen is the two diverging silently."""
    claude = (PKG.parent / "CLAUDE.md").read_text(encoding="utf-8-sig")
    stated = re.search(r"house posture \((\d+) such handlers\)", claude)
    assert stated, "CLAUDE.md no longer states the containment count — restate it or drop this guard"
    assert int(stated.group(1)) == len(list(_blind_handlers())), (
        f"CLAUDE.md says {stated.group(1)} blind handlers; the census derives "
        f"{len(list(_blind_handlers()))} — update the sentence")
