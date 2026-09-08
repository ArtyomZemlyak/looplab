"""Every underscore-private name imported ACROSS a package boundary is declared here (doc 25
XP-01 / TO-09).

An underscore normally licenses the owning module to rename freely. Twenty-six of them are load
bearing across packages — `tools/` reaches into `engine.memory` and `engine.claims`, `serve/` into
`events.traceview`, four packages into `events.eventstore.interprocess_lock` — and every one of
those imports is FUNCTION-LOCAL (deliberately, to keep the import graph acyclic). So a rename
produces no import-time error at all. `CrossRunTools.execute` then swallows the ImportError into
the generic "(cross-run tool unavailable)" string, and the affected tools simply stop answering.

This is the silent-rename failure class the registries in CLAUDE.md exist to prevent, applied to
the one surface that had no registry. Two directions, both required:

* every declared edge still RESOLVES — renaming a private that another package imports is a red
  test naming both ends, not a tool that quietly answers "unavailable";
* every edge in the tree is DECLARED — adding a new cross-package private dependency means saying
  so here, which is the moment to ask whether it should be public instead.

The registry is a list of debts, not a design. The review's preferred fix is to promote what
`tools/` actually needs into a public read-model API; until then this makes the debt visible and
its breakage loud.
"""
from __future__ import annotations

import ast
import collections
import importlib
from pathlib import Path

import pytest

from _source_scan import iter_sources, iter_trees

_PKG = Path(__file__).resolve().parents[1] / "looplab"

# consumer package -> provider module -> the private names it imports.
CROSS_PACKAGE_PRIVATE_IMPORTS: dict[str, dict[str, tuple[str, ...]]] = {
    "adapters": {
        # The BACK-COMPAT re-export of the composition root that split out of this module
        # (doc 25 RA-01). These are private-by-convention helpers whose names dozens of call
        # sites and tests already spell as `looplab.adapters.tasks._make_abstractor` &c., so the
        # re-export has to carry them or the split stops being invisible.
        "looplab.agents.factory": ("_agent_model", "_make_abstractor", "_memora_cache_path",
                                   "_set_role_client", "_shared_providers"),
        "looplab.runtime.sandbox": ("_last_json_dict",),
        "looplab.tools.reposcout": ("_SKIP_DIRS",),
        # `EvalSpec.metric["subject"]` is validated by the SAME rule as `expect.files` and `needs`
        # — one definition of "a workdir-relative declaration path" (doc 35's whole point is that
        # these are projections of one relation, not three lints). Declared rather than promoted:
        # the name is private because `command_eval` owns the rule and nothing outside the
        # declaration family should be reaching for it, and this registry is what makes a future
        # rename a red test instead of a submit-time validator that silently stops running.
        "looplab.runtime.command_eval": ("_validate_rel_paths",),
        # STRUCK OFF 2026-08-21: `adapters/repo_developer.py` no longer splices
        # `agents.roles._CONTEXT_BEFORE_TOOLS_RULE`. Not a refactor — the clause was removed from
        # that prompt on evidence (A/B'd over three models it moved nothing, while the same
        # knowledge published as DATA took cold-start tool calls 41.3 -> 17.7), and the role's two
        # byte-for-byte prompt contracts forbid an unconditional suffix. The seam is gone, so the
        # debt shrinks here rather than being carried as a comment.
    },
    "agents": {
        "looplab.core.llm": ("_reasoning_of",),
        "looplab.runtime.sandbox": ("_kill_tree",),
        # A9 (docs/60): the path-keyed read nudge tells the model how many PAGES the file it
        # keeps re-reading actually is, and the page width has to be the reader's OWN — a
        # second copy of `RESULT_CAP - 400` in the loop is a number that goes stale the day
        # the reader's budget moves, in a sentence whose whole job is to be arithmetic the
        # model can act on. Declared rather than promoted: the constant is private because
        # `reposcout` owns the page, and this registry is what makes a rename a red test.
        "looplab.tools.reposcout": ("_MAX_READ",),
    },
    "cli": {
        "looplab.adapters.tasks": ("_make_abstractor",),
        # PROMOTED 2026-09-08 (doc 25 XP-01/TO-09 §6.6): the claim/lesson source views and the
        # portfolio concept overview are public names on `engine/knowledge_views.py`, so `cli/`
        # imports a declared read model instead of two engine privates.
    },
    "engine": {
        "looplab.agents.roles": ("_state_brief",),
        # THE TRUNCATION BIT, and its own docstring is why it is imported rather than re-derived:
        # "Split out so a caller that owes its operator a truncation receipt can read the fact from
        # the one place that knows it, instead of inferring it from lengths." `engine/memory.py`'s
        # skill card is such a caller — it reconstructed the fact from `len(body)` and shipped a
        # clipped snippet denying it was clipped, in the direction `core/redact.py` names as a
        # shipped bug. Declared rather than promoted, on the registry's own question: the name is
        # private because the PUBLIC spelling is deliberately the plain `str` (~30 call sites
        # persist that return value directly), and a caller should reach for the tuple only when it
        # genuinely owes a receipt.
        "looplab.core.redact": ("_redact_persisted",),
        # WHO A DROP RECEIPT IS ATTRIBUTED TO, replayed over raw events by the card reopen gate
        # WHO A DROP RECEIPT IS ATTRIBUTED TO, replayed over raw events by the card reopen gate
        # rather than re-derived. Its own docstring is the reason it is imported and not copied:
        # "ONE spelling, because three readers ask it and they must not drift" — the reopen gate is
        # the fourth, and a private-by-convention rule that three readers already share is exactly
        # what this registry exists to make renameable-with-a-red-test instead of promotable.
        "looplab.events.card_ledger": ("_drop_author",),
        # The finalize-scope read side moved DOWN to `events/` so `search` could stop importing the
        # engine (doc 25 XP-07). Its two public names are the cluster's API; these three are the
        # cluster's own internals, and `engine/finalize.py` — the module they moved OUT of — still
        # calls them directly (`_scope_has_step` 13 times). Declared rather than promoted: they are
        # private on purpose, and this registry is what turns a future rename into a red test
        # instead of a silent break.
        "looplab.events.finalize_scope": ("_adjacent_claim", "_finalize_begun", "_scope_has_step"),
        # ADDED at the 2026-08-31 merge, and it is master's `6262f3a1` paying a debt it opened
        # without declaring: `engine/card_reservation.py` imports `_drop_author` so the engine's
        # retire idempotence replays the FOLD's own drop/reopen rule instead of re-inventing it —
        # that function's docstring is explicit that there must be "ONE spelling, because three
        # readers ask it and they must not drift", and the retire scan is now a fourth. Declared
        # rather than promoted for exactly the reason the docstring above gives: the name is
        # private because the ledger owns the rule, and this row is what makes a rename a red test
        # instead of an engine that silently re-retires a card the operator reopened.
        "looplab.events.card_ledger": ("_drop_author",),
        "looplab.runtime.command_eval": ("_LABEL_KEYS", "_PRED_KEYS", "_as_list"),
        # THE ONE FILE-DIGEST RULE, sampling above `SAMPLE_ABOVE` with the mode in the preimage so a
        # sampled entry can never collide with a fully-read one. `engine/workspace.py`'s build-delta
        # digest is the third reader of it; `runtime/stage_identity.py` was the second and is
        # in-package. Declared rather than promoted because the rule belongs to `metric_subject` —
        # it is how a metric's REFERENT is bound, and a caller reaching for it sideways is exactly
        # what this registry exists to make renameable-with-a-red-test.
        "looplab.runtime.metric_subject": ("_sha256",),
        "looplab.runtime.sandbox": ("_run_argv", "_to_float"),
        # `looplab.search.concept_graph._experiment_nodes` left this list on 2026-08-05: doc 25 SE-09
        # PROMOTED it (and `_node_text`) to `concept_tagging.experiment_nodes` / `.node_text`. Four
        # modules imported it — three inside `search`, plus `engine/novelty.py` across this boundary —
        # so the underscore was claiming a freedom to rename that had already been spent. This is the
        # outcome the registry's own docstring asks for ("the moment to ask whether it should be
        # public instead"), which is why the entry is gone rather than re-pointed at the new module.
        "looplab.tools.vectorstore": ("_cosine",),
    },
    "judgebench": {
        # The BENCH MEASURES this function, so it has to be able to CALL it. `_failure_reason` is
        # the whole subject of `judgebench/triage_score.py::head_replay_candidate` — the arm that
        # replays today's classifier over the recorded corpus — and there is no public wrapper that
        # takes a bare `res` and returns a reason. Declared rather than promoted for the reason the
        # registry's docstring asks about: the name is private because the ENGINE owns when it runs
        # and nothing on a run's execution path should reach it sideways, and judgebench is not on
        # one (it is a developer tool over the operator's local `runs/`, deliberately not a
        # `looplab` subcommand). What the registry buys here is exactly what the bench is for: a
        # rename becomes a red test instead of a bench that silently stops measuring anything.
        "looplab.engine.triage": ("_failure_reason",),
        # The bench's `Gate` asks whether an answer would have reached an INTERVENTION, and the
        # confidence conjunct is the one that separates the corpus's five false stops from its 49
        # confident true ones. Re-implementing `confidence >= threshold` there would be a second
        # copy of a rule with two live traps in it — a model that answers the STRING `'0.9'` (19 of
        # the 450 recorded confidences), and `min(1.0, nan) == 1.0`, i.e. a non-finite value
        # comparing True — and a bench that scores a stop the engine would refuse is measuring the
        # wrong target in the expensive direction. Declared rather than promoted: the name is
        # private because the engine owns the rule, and this registry is what makes a rename a red
        # test instead of a bench that silently drifts from the gate it claims to model.
        "looplab.engine.train_monitor": ("_normalize_monitor_confidence",),
    },
    "search": {
        # The discarded-prefetch predicate moved DOWN to `core/models.py` on 2026-08-05 so the FOLD
        # could reuse it (`events` may not import `search`, and a second replay-side copy of "did
        # this node spend budget" is exactly the disagreement that broke Card speculation).
        # `card_selection` re-exports all three names it used to define; this is the private one, and
        # the re-export is kept rather than dropped because the name was spellable through
        # `card_selection` yesterday and a monkeypatch on the old path must not become a silent
        # no-op. Declared, not promoted: it is `is_unevaluated_speculative_discard`'s internal proof
        # step, not an API.
        "looplab.core.models": ("_durable_speculative_lifecycle",),
    },
    "serve": {
        "looplab.agents.roles": ("_CONCEPT_AUTHORING_GUIDANCE",),
        # `_windows_move_write_through` left this list when doc 25 SC-05 moved the durable
        # no-replace rename INTO atomicio as the public `durable_no_replace_rename` — the two serve
        # callers no longer reach past the package boundary to assemble it themselves.
        "looplab.core.atomicio": ("_ensure_strict_parent",),
        # PROMOTED 2026-09-08 (doc 25 XP-01): the three claim-assessment views this router used to
        # reach for are public on `engine/knowledge_views.py`.
        "looplab.events.traceview": ("_bounded_tail", "_cap_span_io", "_cap_str", "_finite_number",
                                     "_normalize_span", "_normalized_id", "_projection_counter",
                                     "_response_projection", "_tree"),
    },
    "tools": {
        "looplab.core": ("_pathsafe",),
        "looplab.core.gitenv": ("_GIT_CRED_KEY_MARKERS", "_GIT_IDENTITY"),
        # PROMOTED 2026-09-08 — the thirteen capsule/claim read-model privates and the purge
        # sentinel this package used to import by their underscore names are now the public
        # `engine/knowledge_views.py` surface (doc 25 XP-01/TO-09 §6.6). This was the registry's
        # own stated preference ("the moment to ask whether it should be public instead") and the
        # widest debt on the list, so the rows are GONE rather than re-pointed; the two-way guard
        # moved to `tests/test_knowledge_views.py`, which pins the surface AND that no consumer
        # outside `engine/` reaches around it. `looplab.events.eventstore._interprocess_lock` left
        # with them, promoted to the public `interprocess_lock`.
        # `looplab.serve.engine_proc` stood here with four names, and stands here no longer: the
        # run-lifecycle primitives moved DOWN to `looplab/engine/run_lifecycle.py` and came out
        # PUBLIC (doc 25 XP-03, closed 2026-09-08), so `tools/` neither reaches up nor reaches a
        # private name. Four rows deleted, none added — the shape a shrink-only debt is meant to
        # take.
    },
}

_DECLARED = {(consumer, module, name)
             for consumer, modules in CROSS_PACKAGE_PRIVATE_IMPORTS.items()
             for module, names in modules.items() for name in names}


def _actual_edges() -> set[tuple[str, str, str]]:
    edges: set[tuple[str, str, str]] = set()
    for path, tree in iter_trees(_PKG):
        consumer = path.relative_to(_PKG).as_posix().split("/")[0]
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if not node.module.startswith("looplab."):
                continue
            provider = node.module.split(".")[1]
            if provider == consumer:              # inside one package a private stays private
                continue
            for alias in node.names:
                if alias.name.startswith("_"):
                    edges.add((consumer, node.module, alias.name))
    return edges


@pytest.mark.parametrize("consumer,module,name", sorted(_DECLARED))
def test_every_declared_private_seam_still_resolves(consumer, module, name):
    """The direction that catches a rename.

    These imports are function-local, so nothing fails at import time; the consumer just starts
    raising ImportError deep inside a tool call that swallows it. Resolving each name here turns
    that into a red test that names both ends."""
    provider = importlib.import_module(module)
    assert hasattr(provider, name), (
        f"{module}.{name} is gone, but {consumer}/ still imports it. Renaming a private that "
        "another PACKAGE depends on needs the consumer updated in the same change (or the name "
        "promoted to a public read-model API — see doc 25 XP-01).")


def test_no_undeclared_cross_package_private_import_exists():
    """The direction that catches a new debt.

    A new private cross-package import is not forbidden — it is a decision, and this is where it
    gets made explicitly instead of by autocomplete."""
    undeclared = sorted(_actual_edges() - _DECLARED)
    assert not undeclared, (
        "these cross-package private imports are not declared in "
        "CROSS_PACKAGE_PRIVATE_IMPORTS:\n  "
        + "\n  ".join(f"{consumer}/ <- {module}.{name}" for consumer, module, name in undeclared)
        + "\nDeclare them, or import a public name instead.")


def test_the_registry_does_not_carry_edges_that_no_longer_exist():
    """A stale entry is worse than no entry: it makes the surface look bigger than it is, and it
    keeps a name alive that the owning module could otherwise rename freely."""
    stale = sorted(_DECLARED - _actual_edges())
    assert not stale, (
        "these declared seams are gone from the tree — drop them from the registry so the debt "
        "shrinks visibly:\n  "
        + "\n  ".join(f"{consumer}/ <- {module}.{name}" for consumer, module, name in stale))


def test_the_scan_can_actually_see_a_cross_package_private_import():
    """A scan that matches nothing is indistinguishable from one whose walk is broken."""
    edges = _actual_edges()
    assert edges, "the AST walk found no cross-package private imports at all"
    # `events.eventstore.interprocess_lock` used to be the exemplar here — four packages outside
    # `events` took it, and this assertion said "either the walk regressed or the name was finally
    # promoted". It was promoted on 2026-09-08, so the exemplar moves to the widest surviving debt,
    # which keeps the check honest about a broken walk without pinning a debt anyone is paying.
    traceview_privates = {name for consumer, module, name in edges
                          if module == "looplab.events.traceview" and consumer == "serve"}
    assert len(traceview_privates) >= 9, (
        f"expected serve/ to depend on nine events.traceview privates, saw "
        f"{sorted(traceview_privates)} — either the walk regressed or they were finally promoted")


def test_the_widest_debts_are_the_ones_the_review_named():
    """Keeps the registry honest about WHERE the pressure is, so promoting to a public API can be
    prioritized by size rather than by whoever trips over it first."""
    per_provider = collections.Counter(
        module for _consumer, module, _name in _DECLARED)
    assert per_provider["looplab.events.traceview"] >= 9, (
        "serve/ leans on nine traceview privates — doc 25 SR-* proposes a public projection API")
    # `looplab.engine.memory` was the OTHER pinned debt — seven capsule read-model privates reached
    # by `tools/` and `cli/`, XP-01's primary promotion candidate. It was promoted on 2026-09-08 to
    # `engine/knowledge_views.py`, so the pin inverts: the capsule/claim read model must NOT come
    # back as a private cross-package surface. A `>= 7` assertion kept here would have to be
    # weakened to `>= 0`, which is not a pin at all.
    assert not [module for module in per_provider
                if module in ("looplab.engine.memory", "looplab.engine.claims",
                              "looplab.engine.concept_capsules", "looplab.engine.claims_health")], (
        "the cross-run read model is public (engine/knowledge_views.py) — a new private import of "
        "it is a regression of doc 25 XP-01, not a new debt to declare")


# --- the tools -> serve inversion (doc 25 XP-03 / TO-03) --------------------------------------

def test_the_run_mutating_tool_takes_its_lifecycle_primitives_by_injection():
    """`tools/` sits BELOW `serve/` in the package map, and `serve/assistant.py` constructs
    `RunControlTools` — so reaching up into `serve` from the tool closed a cycle that only
    function-local imports were keeping open.

    The primitives are an explicit `RunLifecycleFns` argument, and since 2026-09-08 the DEFAULT no
    longer reaches upward either: the five live in `looplab/engine/run_lifecycle.py`, below both
    packages, and `serve/engine_proc` + `serve/run_files` re-export them. Both halves are driven
    here — an injected provider is used verbatim, and the default resolves without `serve`.
    """
    from looplab.tools.run_control_tools import RunControlTools, RunLifecycleFns

    calls = []

    class _Lock:
        def __enter__(self):
            calls.append("lock")
            return self

        def __exit__(self, *_exc):
            return False

    injected = RunLifecycleFns(
        engine_alive=lambda _rd: calls.append("alive") or False,
        fresh_resume_launch_pending=lambda _rd: calls.append("resume") or False,
        fresh_run_launch_pending=lambda _rd: calls.append("run") or False,
        run_lifecycle_lock=lambda _rd: _Lock(),
        run_config_write_lock=lambda _p: _Lock(),
    )
    tools = RunControlTools("runs", lifecycle=injected)
    assert tools.lifecycle() is injected, "an injected provider must be used verbatim"

    # ...and with nothing injected the default resolves the SAME five callables the server uses,
    # so the historical behaviour of every existing caller is unchanged — but out of the module
    # below both packages. Compared by identity against `serve/engine_proc`'s re-exports, because
    # "the same implementation" is the property; "an importable name exists" is not.
    default = RunControlTools("runs").lifecycle()
    assert isinstance(default, RunLifecycleFns)
    from looplab.serve import engine_proc, run_files
    assert default.engine_alive is engine_proc._engine_alive
    assert default.fresh_resume_launch_pending is engine_proc._fresh_resume_launch_pending
    assert default.fresh_run_launch_pending is engine_proc._fresh_run_launch_pending
    assert default.run_lifecycle_lock is engine_proc._run_lifecycle_lock
    assert default.run_config_write_lock is run_files.run_config_write_lock


def test_no_upward_import_of_serve_is_left_anywhere_in_tools():
    """The inversion is COMPLETE (doc 25 XP-03, closed 2026-09-08): `serve` may not be named by an
    import anywhere in `tools/`, at module level or inside a function.

    This used to allow exactly one site — the `lifecycle` default provider — which made the debt
    reviewable without paying it: every caller that did not inject (the assistant's own default
    path included) still took the edge. The primitives moved DOWN to `engine/run_lifecycle.py`
    instead, so the allowance is gone and the rule is now unconditional.
    """
    offenders = []
    for path, source in iter_sources(_PKG / "tools"):
        for node in ast.walk(ast.parse(source)):
            module = getattr(node, "module", None) if isinstance(node, ast.ImportFrom) else None
            names = ([a.name for a in node.names] if isinstance(node, ast.Import) else [])
            reached = [m for m in [module, *names]
                       if m and (m == "looplab.serve" or m.startswith("looplab.serve."))]
            for m in reached:
                offenders.append(f"{path.relative_to(_PKG.parent)}:{node.lineno}: {m}")
    assert not offenders, (
        "tools/ imports serve/ again — pass the dependency in, or move it down beside "
        f"looplab/engine/run_lifecycle.py:\n  " + "\n  ".join(offenders))
