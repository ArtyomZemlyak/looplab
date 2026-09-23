"""The cross-run store registry is the TREE's list, both ways (review 2026-09-22, ENG3-07).

`engine/memory_stores.py::MEMORY_STORES` exists because no module listed what lives under
`memory_dir`: five partial lists did, the deletion cascade walked one of them, and two stores whose
every row names the run that wrote it (`lesson_utility.jsonl`, `regime_contrast.jsonl`) were on none
— so a deleted run went on steering later runs through them. A registry that a new store can bypass
is the same failure with one more file, so this holds it to the source, two ways:

* UNREGISTERED is red. Every name joined onto a `memory_dir`-rooted path anywhere in `looplab/` (as
  a literal, or a module-level string constant) must be a registry row, and so must every bare
  `*.jsonl` literal that is not one of the run-directory files declared below with its home. The
  second census exists because the first only sees the idioms it can root — `self.dir / "x.jsonl"`
  on a provider whose `dir` came from `memory_dir` is invisible to it.
* STALE is red. Every row's declared `writer` must still exist, still call a write primitive, and
  still NAME its store — or, where the name arrives as a parameter, the function that binds the
  path must (`PATH_BINDERS`, itself two-way: a row whose writer names its store directly again has
  no business being exempted).

AST, not substrings (CLAUDE.md "A guard test must not be satisfiable by a COMMENT"): the scanners are
exercised on a synthetic package at the end, where a store named only in a comment and a path joined
under a RUN directory must both be ignored. What this cannot prove is that a named write executes —
that is `tests/test_run_deletion_memory_cascade.py::test_a_cascaded_deletion_leaves_no_row_of_the_run_in_any_cascaded_store`,
which drives every registered store through the real deletion route.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from looplab.engine import memory_stores as ms
from looplab.engine.memory_stores import CASCADED, MEMORY_STORES, POLICIES, PRESERVED
from tests._source_scan import PKG, iter_trees

REGISTRY_FILE = PKG / "engine" / "memory_stores.py"

# Bare `*.jsonl` literals that are NOT cross-run stores, each with the directory it lives in. Two-way:
# a name here the tree no longer spells is a stale row.
NOT_MEMORY_JSONL = {
    "events.jsonl": "the run's event log (run directory)",
    "spans.jsonl": "the run's trace spans (run directory)",
    "spans.index.jsonl": "the run's span index (run directory)",
    ".spans-append.jsonl": "the run's span append staging file (run directory)",
    "chat.jsonl": "the run's operator chat (run directory)",
    "sft.jsonl": "`looplab export-sft`'s default output (run directory)",
    "messages.jsonl": "an assistant session's transcript (`<run root>/assistant/<session>/`)",
    "harness.v1.jsonl": "the agent-trajectory bench fixture (`tests/data/agent_trajectory/`)",
}

# The closed vocabulary of calls that WRITE a store. Generic spellings (`add`, `save`, `write`) are
# admitted because the store classes use them (`JsonlCaseLibrary.add`, `ConceptCapsuleStore.add`,
# `ExploitSuite.save`, an exclusive-create handle's `write`); this is checked only on a function the
# registry DECLARES as the writer, so the looseness can hide a wrong citation, never an unregistered
# store.
WRITE_CALLS = frozenset({
    "append_jsonl_bytes_locked", "append_governance", "durable_governance_append",
    "replace_jsonl_rows_atomic_preserving_quarantine", "atomic_write_text", "atomic_write_bytes",
    "write_jsonl_atomic", "write_auto_skill", "save", "add", "write",
})

# Rows whose writer is HANDED its path, and the function that binds that path. For the three
# paid-curation histories the name comes from `governance_health.curation_ledger_file(kind)` — the
# registry's own `curation_kind` table, pinned equal below — so the binder is the function that asks
# it; the claim receipts and the abstraction cache are handed a path a factory builds.
PATH_BINDERS = {
    "concept_curation_log.jsonl":
        "engine/curation_protocol.py::CurationProtocolMixin._run_finalize_steward",
    "claim_curation_log.jsonl":
        "engine/curation_protocol.py::CurationProtocolMixin._run_finalize_steward",
    "task_facets_curation_log.jsonl":
        "engine/curation_protocol.py::CurationProtocolMixin._run_finalize_steward",
    ".curation_invocations": "engine/curation_protocol.py::CurationProtocolMixin._curation_claim_path",
    "memora_cache.json": "agents/providers.py::_memora_cache_path",
}

# The stores whose rows NAME the run that wrote them and that a deletion nonetheless leaves — each a
# stated exception to "a run's own rows go with it", which is the default the finding was about.
# Two-way, so flipping a run-owned store to PRESERVED (the one-word edit that would re-open ENG3-07)
# is a red test until the exception is written down here.
RUN_NAMING_PRESERVED = {
    "concept_curation_log.jsonl": "the audit of a PAID decision; the at-most-once gate reads it",
    "claim_curation_log.jsonl": "the audit of a PAID decision; the at-most-once gate reads it",
    "task_facets_curation_log.jsonl": "the audit of a PAID decision; the at-most-once gate reads it",
    ".curation_invocations": "the receipt a paid steward call is gated on; removing it re-pays",
}

_BARE_JSONL = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]*\.jsonl$")


# ------------------------------------------------------------------------------------ the scanners

def _terminal(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "<text>"` — how `concept_tidy.RATIFICATION_LEDGER` and
    `curation_protocol._CURATION_CLAIM_DIR` spell their stores."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value.value
    return out


def _rooted(expr: ast.AST, aliases: set[str]) -> bool:
    """Is `expr` a path under the cross-run memory dir? `memory_dir` (a parameter, `self.memory_dir`,
    `self._e.memory_dir`, `settings.memory_dir`), a local bound to one, `Path(...)` of one, its
    `.absolute()`/`.resolve()`/`.expanduser()`, or a conditional either branch of which is one."""
    if isinstance(expr, (ast.Name, ast.Attribute)) and _terminal(expr) == "memory_dir":
        return True
    if isinstance(expr, ast.Name) and expr.id in aliases:
        return True
    if isinstance(expr, ast.Call):
        name = _terminal(expr.func)
        if name == "Path" and expr.args:
            return _rooted(expr.args[0], aliases)
        if name in ("absolute", "resolve", "expanduser") and isinstance(expr.func, ast.Attribute):
            return _rooted(expr.func.value, aliases)
    if isinstance(expr, ast.IfExp):
        return _rooted(expr.body, aliases) or _rooted(expr.orelse, aliases)
    return False


def _scopes(tree: ast.Module):
    """Each function (with its nested closures, which read its locals), plus the module's own
    statements — never the whole module as one scope, where a `base` bound to `memory_dir` in one
    function would root an unrelated `base` in another."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield [n for stmt in node.body for n in ast.walk(stmt)]
    yield [n for stmt in tree.body
           if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
           for n in ast.walk(stmt)]


def memory_joins(pkg: Path = PKG) -> dict[str, set[str]]:
    """`{store name: {"<file>:<line>"}}` for every `<memory_dir path> / <name>` in `pkg`."""
    found: dict[str, set[str]] = {}
    for path, tree in iter_trees(pkg):
        consts = _module_constants(tree)
        for nodes in _scopes(tree):
            aliases: set[str] = set()
            grew = True
            while grew:                                   # locals bound to locals bound to it
                grew = False
                for node in nodes:
                    if isinstance(node, ast.Assign) and _rooted(node.value, aliases):
                        targets = [t for t in node.targets if isinstance(t, ast.Name)]
                    elif isinstance(node, ast.NamedExpr) and _rooted(node.value, aliases):
                        targets = [node.target]
                    else:
                        continue
                    for target in targets:
                        if target.id not in aliases:
                            aliases.add(target.id)
                            grew = True
            for node in nodes:
                if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
                        and _rooted(node.left, aliases)):
                    continue
                right = node.right
                if isinstance(right, ast.Constant) and isinstance(right.value, str):
                    name = right.value
                elif isinstance(right, ast.Name) and right.id in consts:
                    name = consts[right.id]
                else:
                    continue                              # a computed name: see the literal census
                found.setdefault(name.split("/")[0], set()).add(
                    f"{path.relative_to(pkg.parent).as_posix()}:{node.lineno}")
    return found


def bare_jsonl_literals(pkg: Path = PKG) -> dict[str, set[str]]:
    """`{name: {"<file>:<line>"}}` for every string constant in `pkg` that is a bare `*.jsonl`."""
    found: dict[str, set[str]] = {}
    for path, tree in iter_trees(pkg):
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and _BARE_JSONL.match(node.value):
                found.setdefault(node.value, set()).add(
                    f"{path.relative_to(pkg.parent).as_posix()}:{node.lineno}")
    return found


def _resolve(citation: str) -> tuple[ast.Module, ast.AST]:
    """`<module>.py::<Class.method>` -> (the module's tree, the function's node)."""
    rel, qualname = citation.split("::")
    tree = ast.parse((PKG / rel).read_text(encoding="utf-8-sig"), filename=rel)
    scope: list = tree.body
    node = None
    for part in qualname.split("."):
        node = next((n for n in scope if isinstance(
            n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == part), None)
        assert node is not None, f"{citation}: no `{part}` in {rel} (renamed, moved or deleted)"
        scope = node.body
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)), f"{citation} is not a function"
    return tree, node


def _names_store(tree: ast.Module, fn: ast.AST, name: str) -> bool:
    """Does `fn`'s body spell `name` — as a string constant, or through a module-level constant of
    its own module or of the registry module (`memory_stores.<NAME>`)?"""
    consts = {**_module_constants(tree), **_module_constants(
        ast.parse(REGISTRY_FILE.read_text(encoding="utf-8")))}
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and node.value == name:
            return True
        if isinstance(node, (ast.Name, ast.Attribute)) and consts.get(_terminal(node)) == name:
            return True
    return False


def _calls(fn: ast.AST) -> set[str]:
    return {_terminal(node.func) for node in ast.walk(fn) if isinstance(node, ast.Call)}


# ------------------------------------------------------------------------------ the registry itself

def test_every_row_states_a_policy_it_can_keep():
    names = [store.name for store in MEMORY_STORES]
    assert len(names) == len(set(names)), "one row per store"
    for store in MEMORY_STORES:
        assert store.policy in POLICIES, store.name
        assert store.label and store.key and store.writer, store.name
        assert re.fullmatch(r"[a-z_/]+\.py::[A-Za-z_][A-Za-z0-9_.]*", store.writer), store.writer
        if store.policy == CASCADED:
            # A cascaded store whose rows name no run can never be matched: its purge would report
            # success having removed nothing.
            assert store.names_run, f"{store.name} is cascaded but its rows name no run"
            assert not store.group, f"{store.name}: a group is how a PRESERVED store is disclosed"
        else:
            assert store.group and store.reason, f"{store.name} is preserved without saying why"
    reasons: dict[str, set[str]] = {}
    for store in MEMORY_STORES:
        if store.policy == PRESERVED:
            reasons.setdefault(store.group, set()).add(store.reason)
    assert all(len(r) == 1 for r in reasons.values()), (
        f"a preserved group is disclosed with ONE reason: {reasons}")
    ledgers = [store.ledger for store in MEMORY_STORES if store.ledger]
    assert len(ledgers) == len(set(ledgers))
    kinds = [store.curation_kind for store in MEMORY_STORES if store.curation_kind]
    assert len(kinds) == len(set(kinds))
    assert all(store.ledger for store in MEMORY_STORES if store.curation_kind), (
        "a paid-curation history is a strict governance ledger")


def test_a_store_whose_rows_name_their_run_goes_with_it_unless_the_exception_is_stated():
    run_named = {store.name for store in MEMORY_STORES if store.names_run}
    kept = {store.name for store in MEMORY_STORES
            if store.names_run and store.policy == PRESERVED}
    assert kept == set(RUN_NAMING_PRESERVED), (
        "a store whose rows name the run that wrote them is CASCADED unless RUN_NAMING_PRESERVED "
        f"says why not: preserved {sorted(kept)}, stated {sorted(RUN_NAMING_PRESERVED)}")
    # The two stores the finding found on no list, pinned by name: the decision, not just the rule.
    assert {"lesson_utility.jsonl", "regime_contrast.jsonl"} <= run_named - kept


def test_the_partial_lists_are_views_of_the_registry():
    """The five lists that used to be kept by hand now EQUAL the registry — and are ASSIGNED from
    it, so a hand-copied literal that happens to agree today cannot come back and drift tomorrow."""
    from looplab.engine import cross_run_context, governance_health
    from looplab.serve import memory_cascade

    assert memory_cascade.CASCADED_TIERS == ms.cascaded_tiers()
    assert memory_cascade.PRESERVED_TIERS == ms.preserved_tiers()
    assert governance_health._GOVERNANCE_LEDGER_FILES == ms.governance_ledger_files()
    assert governance_health._CURATION_LEDGER_SCOPES == ms.curation_ledger_scopes()
    assert governance_health._GOVERNED_SOURCE_NAMES == frozenset(ms.governed_source_names())
    assert cross_run_context.CROSS_RUN_SOURCE_NAMES == ms.governed_source_names()

    views = {
        "serve/memory_cascade.py": {"CASCADED_TIERS": "cascaded_tiers",
                                    "PRESERVED_TIERS": "preserved_tiers"},
        "engine/governance_health.py": {"_GOVERNANCE_LEDGER_FILES": "governance_ledger_files",
                                        "_CURATION_LEDGER_SCOPES": "curation_ledger_scopes",
                                        "_GOVERNED_SOURCE_NAMES": "governed_source_names"},
        "engine/cross_run_context.py": {"CROSS_RUN_SOURCE_NAMES": "governed_source_names"},
    }
    for rel, wanted in views.items():
        tree = ast.parse((PKG / rel).read_text(encoding="utf-8"))
        assigned = {}
        for node in tree.body:
            target = (node.targets[0] if isinstance(node, ast.Assign) else
                      node.target if isinstance(node, ast.AnnAssign) else None)
            if isinstance(target, ast.Name) and target.id in wanted:
                assigned[target.id] = _calls(node.value)
        for view, accessor in wanted.items():
            assert accessor in assigned.get(view, set()), (
                f"{rel}::{view} must be assigned from memory_stores.{accessor}(), not spelled out")


def test_every_cascaded_store_has_a_keep_rule_the_cascade_walks(tmp_path):
    """`_tier_rules` raises KeyError on a cascaded row with no predicate — every survey and every
    purge. Driven here so that is a red test and never a production 500."""
    from looplab.serve.memory_cascade import RunIdentity, _tier_rules

    rules = _tier_rules(tmp_path, RunIdentity("r", "u"))
    assert [(name, label) for name, label, _rule in rules] == list(ms.cascaded_tiers())


# --------------------------------------------------------------------------- the tree, both ways

def test_every_store_joined_under_memory_dir_is_registered():
    registered = {store.name for store in MEMORY_STORES}
    joined = memory_joins()
    assert {"lessons.jsonl", "skills", "memora_cache.json"} <= set(joined), (
        "control: the scanner must see the idioms the writers actually use")
    unregistered = {name: sorted(sites) for name, sites in joined.items()
                    if name not in registered and not name.endswith(".lock")}
    assert not unregistered, (
        "stores written under memory_dir with no row in engine/memory_stores.py::MEMORY_STORES — "
        "add one, with its deletion policy and writer (a `.lock` is a rendezvous, not a store): "
        f"{unregistered}")


def test_every_bare_jsonl_literal_is_a_registered_store_or_a_declared_run_file():
    registered = {store.name for store in MEMORY_STORES}
    literals = bare_jsonl_literals()
    stray = {name: sorted(sites)[:3] for name, sites in literals.items()
             if name not in registered and name not in NOT_MEMORY_JSONL}
    assert not stray, (
        "a JSONL file name no registry names: a cross-run store gets a MEMORY_STORES row; a file "
        f"that lives elsewhere gets a NOT_MEMORY_JSONL entry saying where: {stray}")
    stale = sorted(set(NOT_MEMORY_JSONL) - set(literals))
    assert not stale, f"NOT_MEMORY_JSONL names files the tree no longer spells: {stale}"
    assert not set(NOT_MEMORY_JSONL) & registered


@pytest.mark.parametrize("store", MEMORY_STORES, ids=lambda store: store.name)
def test_every_registered_store_is_still_written_by_its_declared_writer(store):
    tree, writer = _resolve(store.writer)
    assert _calls(writer) & WRITE_CALLS, (
        f"{store.writer} no longer calls any write primitive — a row naming a store nothing "
        "writes is a stale row")
    direct = _names_store(tree, writer, store.name)
    binder = PATH_BINDERS.get(store.name)
    if binder is None:
        assert direct, f"{store.writer} no longer names {store.name}"
        return
    assert not direct, (
        f"{store.writer} names {store.name} itself: delete its PATH_BINDERS exemption")
    binder_tree, binder_fn = _resolve(binder)
    if store.curation_kind:
        assert "curation_ledger_file" in _calls(binder_fn), binder
    else:
        assert _names_store(binder_tree, binder_fn, store.name), f"{binder} no longer names it"


def test_path_binders_only_exempt_registered_stores():
    assert set(PATH_BINDERS) <= {store.name for store in MEMORY_STORES}


# ----------------------------------------------------------------------- the scanners, on purpose

def test_the_scanners_see_code_and_not_prose(tmp_path):
    """The house rule for a source guard: exercised on a synthetic tree, where a store named only in
    a comment and a file joined under a RUN directory must both be ignored."""
    pkg = tmp_path / "fakepkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "stores.py").write_text(
        "from pathlib import Path\n"
        "RECEIPTS = 'ghost_receipts.jsonl'\n"
        "def writer(memory_dir):\n"
        "    base = Path(memory_dir)\n"
        "    # base / 'comment_only.jsonl'\n"
        "    (base / 'ghost.jsonl').write_text('')\n"
        "    (Path(memory_dir) / RECEIPTS).write_text('')\n"
        "    def nested():\n"
        "        return base / 'closure.jsonl'\n"
        "    return nested\n"
        "def unrelated(run_dir):\n"
        "    base = run_dir\n"
        "    return base / 'not_memory.jsonl'\n",
        encoding="utf-8")
    assert set(memory_joins(pkg)) == {"ghost.jsonl", "ghost_receipts.jsonl", "closure.jsonl"}
    assert set(bare_jsonl_literals(pkg)) == {
        "ghost.jsonl", "ghost_receipts.jsonl", "closure.jsonl", "not_memory.jsonl"}
