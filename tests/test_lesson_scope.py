"""One visibility predicate and one tokenizer over `lessons.jsonl` (doc 25 TO-07).

`agents/factory.py` gives a bound agent BOTH `CrossRunTools` and `MemoryTools` whenever `memory_dir`
and `cross_run_read_tools` are set. `CrossRunTools` invested heavily in fail-closed scoping of
`lessons.jsonl` — unknown polarity hidden, the run's own rows excluded as a replacement-generation
fence, foreign task families reachable only through a strict goal-fingerprint overlap — and
`MemoryTools.search_lessons` read the same file with none of it. Every row one tool deliberately
withheld was retrievable one tool over, in the same toolset, in the same turn.

The two also tokenized differently (`[^\\W_]+` casefold vs an ASCII `[a-z0-9@._]+`), so the same
query matched different lesson sets depending on which tool the model happened to call.

Both are now `trust/cross_run.py`: `LessonScope` and `scope_terms`.
"""
from __future__ import annotations

import json

import pytest

from looplab.tools.cross_run_tools import CrossRunTools
from looplab.tools.memory_tools import MemoryTools
from looplab.trust.cross_run import LessonScope, scope_terms


class _State:
    def __init__(self, *, run_id="run-live", task_id="task-a", direction="min",
                 goal="reduce validation loss on the tabular benchmark"):
        self.run_id = run_id
        self.task_id = task_id
        self.direction = direction
        self.goal = goal
        self.nodes = {}


def _lesson(**over) -> dict:
    row = {"run_id": "run-old", "task_id": "task-a", "direction": "min",
           "statement": "smaller batches converged faster"}
    row.update(over)
    return row


# --------------------------------------------------------------------------------------------
# The predicate
# --------------------------------------------------------------------------------------------

def test_an_unbound_scope_is_portfolio_wide():
    """A CLI or human audit has no live run to be scoped against; narrowing it would hide evidence
    from the OPERATOR rather than from an agent."""
    scope = LessonScope()
    assert scope.allows(_lesson()) is True
    assert scope.allows(_lesson(direction=None)) is True
    assert scope.allows(_lesson(task_id="something-else")) is True


def test_a_bound_scope_hides_a_row_with_unknown_polarity():
    """Fail-closed on missing/malformed `direction`: a lesson learned while MAXIMIZING is advice in
    the wrong direction for a minimizing run, and an exact task id must not manufacture polarity."""
    scope = LessonScope.of(_State())
    assert scope.allows(_lesson()) is True
    for bad in (None, "", "minimize", "MIN", 0, ["min"]):
        assert scope.allows(_lesson(direction=bad)) is False, bad


def test_a_bound_scope_hides_the_opposite_direction():
    scope = LessonScope.of(_State(direction="min"))
    assert scope.allows(_lesson(direction="max")) is False


def test_a_bound_scope_hides_the_runs_own_rows():
    """The replacement-generation fence. A row this run wrote is not PRIOR evidence — feeding it back
    would let a run corroborate itself."""
    scope = LessonScope.of(_State(run_id="run-live"))
    assert scope.allows(_lesson(run_id="run-live")) is False
    assert scope.allows(_lesson(run_id="run-old")) is True


def test_a_foreign_task_needs_a_strict_fingerprint_overlap():
    """A single generic word is not a security scope: cross-task transfer needs at least two salient
    terms covering half the smaller side."""
    scope = LessonScope.of(_State(task_id="task-a", goal="reduce validation loss tabular"))
    foreign = dict(_lesson(task_id="task-b"))

    assert scope.allows(foreign) is False, "no fingerprint at all -> exact task only"
    assert scope.allows({**foreign, "fingerprint": ["validation"]}) is False, "one term is not scope"
    assert scope.allows({**foreign, "fingerprint": ["validation", "tabular"]}) is True
    assert scope.allows({**foreign, "fingerprint": ["unrelated", "words", "entirely"]}) is False


def test_a_namespaced_fingerprint_token_does_not_dilute_the_overlap():
    """Only BARE tokens count. `kind:repo`-style entries are provenance, never goal vocabulary, so
    they can only ever be counted on the denominator side — and there they would make a genuinely
    on-topic row LOOK unrelated purely because its writer recorded more provenance."""
    scope = LessonScope.of(
        _State(task_id="task-a", goal="reduce validation loss on the tabular benchmark quickly"))
    on_topic = _lesson(task_id="task-b",
                       fingerprint=["validation", "tabular", "kind:repo", "task:x", "phase:y"])
    assert scope.allows(on_topic) is True

    # The same two salient terms, now genuinely outnumbered by BARE ones: that is a real dilution
    # and must hide the row, so the rule is "ignore provenance", not "ignore the denominator".
    diluted = _lesson(task_id="task-b",
                      fingerprint=["validation", "tabular", "audio", "speech", "diarization"])
    assert scope.allows(diluted) is False


def test_an_exact_task_needs_no_fingerprint():
    scope = LessonScope.of(_State(task_id="task-a"))
    assert scope.allows(_lesson(task_id="task-a")) is True


def test_a_state_without_identity_still_binds_closed():
    """Binding must not silently degrade to portfolio-wide just because the state is sparse — that
    would make the tightest case (a run with no task id) the most permissive."""
    scope = LessonScope.of(_State(task_id="", goal=""))
    assert scope.bound is True
    assert scope.allows(_lesson()) is False, "no task identity and no goal terms -> nothing matches"


# --------------------------------------------------------------------------------------------
# Both providers answer with it
# --------------------------------------------------------------------------------------------

def _memory_dir(tmp_path, rows):
    (tmp_path / "lessons.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return tmp_path


def test_search_lessons_hides_what_the_sibling_hides(tmp_path):
    """The finding itself: two tools, one ledger, one answer."""
    rows = [
        _lesson(statement="visible prior lesson about batches"),
        _lesson(run_id="run-live", statement="this run's own output about batches"),
        _lesson(direction="max", statement="opposite-direction advice about batches"),
        _lesson(direction=None, statement="unknown-polarity advice about batches"),
        _lesson(task_id="task-z", statement="foreign task advice about batches"),
    ]
    memory = MemoryTools(str(_memory_dir(tmp_path, rows)))
    memory.bind_state(_State())
    out = memory.execute("search_lessons", {"query": "batches"})

    assert "visible prior lesson" in out
    for hidden in ("this run's own output", "opposite-direction advice",
                   "unknown-polarity advice", "foreign task advice"):
        assert hidden not in out, hidden


def test_an_unbound_memory_provider_still_sees_the_whole_ledger(tmp_path):
    """The CLI/human path must not be narrowed — this is the direction that would LOSE evidence."""
    rows = [_lesson(direction="max", statement="opposite-direction advice about batches"),
            _lesson(direction=None, statement="unknown-polarity advice about batches")]
    memory = MemoryTools(str(_memory_dir(tmp_path, rows)))
    out = memory.execute("search_lessons", {"query": "batches"})
    assert "opposite-direction advice" in out and "unknown-polarity advice" in out


def test_binding_a_none_state_leaves_the_provider_portfolio_wide(tmp_path):
    memory = MemoryTools(str(_memory_dir(tmp_path, [_lesson(direction="max",
                                                            statement="max advice about batches")])))
    memory.bind_state(None)
    assert "max advice" in memory.execute("search_lessons", {"query": "batches"})


def test_the_empty_result_says_whether_a_scope_was_applied(tmp_path):
    """An operator debugging "why did the agent not see my lesson" needs to know a filter ran."""
    memory = MemoryTools(str(_memory_dir(tmp_path, [_lesson(direction="max")])))
    memory.bind_state(_State())
    assert "visible to this run" in memory.execute("search_lessons", {"query": "batches"})

    unbound = MemoryTools(str(_memory_dir(tmp_path, [_lesson(direction="max")])))
    assert "visible to this run" not in unbound.execute("search_lessons", {"query": "nothingmatches"})


def test_recall_notes_uses_the_same_scope_as_lessons(tmp_path):
    """Meta-note writers now persist direction/fingerprint provenance, so the adjacent tool may no
    longer reopen the foreign-task/self-run rows its sibling deliberately hides."""
    (tmp_path / "meta_notes.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in [
            {"run_id": "run-old", "task_id": "task-a", "direction": "min",
             "note": "visible winners used smaller batches"},
            {"run_id": "run-old", "task_id": "task-z", "direction": "min",
             "note": "foreign winners used bigger batches"},
            {"run_id": "run-live", "task_id": "task-a", "direction": "min",
             "note": "this run echoed batches"},
            {"run_id": "run-old", "task_id": "task-a", "direction": "max",
             "note": "opposite direction batches"},
        ]),
        encoding="utf-8")
    memory = MemoryTools(str(tmp_path))
    memory.bind_state(_State())
    out = memory.execute("recall_notes", {"query": "batches"})
    assert "visible winners" in out
    for hidden in ("foreign winners", "this run echoed", "opposite direction"):
        assert hidden not in out


def test_the_sibling_delegates_to_the_same_object(tmp_path):
    tools = CrossRunTools(str(tmp_path))
    assert isinstance(tools._scope, LessonScope)
    tools.bind_state(_State())
    row = _lesson()
    assert tools._in_scope(row, source="lesson") == tools._scope.allows(row)
    assert tools._in_scope(_lesson(direction="max"), source="lesson") is False


def test_the_sibling_exposes_the_scope_as_one_source_of_truth(tmp_path):
    """The five loose attributes a dozen call sites read are properties over `_scope`, not a copy."""
    tools = CrossRunTools(str(tmp_path))
    tools.bind_state(_State(run_id="r1", task_id="t1", direction="max", goal="raise the f1 score"))
    assert (tools._bound, tools._run_id, tools._task_id, tools._direction) == (True, "r1", "t1", "max")
    assert tools._scope_terms == scope_terms("raise the f1 score")
    with pytest.raises(AttributeError):
        tools._direction = "min"          # a write would create the second copy this replaced


def test_a_capsule_still_needs_a_complete_fingerprint_for_foreign_task_visibility(tmp_path):
    """The one source-specific rule that stays on the provider: capsules can be written with a capped
    or legacy-unknown fingerprint, and a truncated one must never authorize a foreign task."""
    tools = CrossRunTools(str(tmp_path))
    tools.bind_state(_State(task_id="task-a", goal="reduce validation loss tabular"))
    capsule = _lesson(task_id="task-b", fingerprint=["validation", "tabular"])
    assert tools._scope.allows(capsule) is True, "the general predicate would admit it"
    assert tools._in_scope(capsule, source="capsule") is False, "no completeness metadata"
    assert tools._in_scope(capsule, source="lesson") is True


def test_an_exact_task_capsule_skips_the_completeness_check(tmp_path):
    """Exact task identity is authoritative and does not need the fuzzy fingerprint at all."""
    tools = CrossRunTools(str(tmp_path))
    tools.bind_state(_State(task_id="task-a"))
    assert tools._in_scope(_lesson(task_id="task-a"), source="capsule") is True


# --------------------------------------------------------------------------------------------
# One tokenizer
# --------------------------------------------------------------------------------------------

def test_both_providers_tokenize_identically():
    from looplab.tools import cross_run_tools, memory_tools
    for text in ("run_id and a.b", "Ünïcode Wörds", "batch_size vs learning-rate", "a bb ccc dddd"):
        assert cross_run_tools._toks(text) == memory_tools._toks(text) == scope_terms(text), text


def test_the_tokenizer_drops_the_noise_words_that_are_not_scope():
    assert scope_terms("a bb ccc") == frozenset({"ccc"}), "tokens of 3+ chars only"
    assert scope_terms("") == frozenset()


def test_neither_provider_keeps_its_own_word_pattern():
    """The private word pattern that made the two disagree is gone, not merely unused.

    Checked over the AST, not the text: this file and both modules DESCRIBE the old `[a-z0-9@._]+`
    class in their comments, so a substring scan matches its own explanation.
    """
    import ast
    import inspect

    from looplab.tools import cross_run_tools, memory_tools
    for module in (cross_run_tools, memory_tools):
        tree = ast.parse(inspect.getsource(module))
        patterns = [ast.unparse(node.value) for node in ast.walk(tree)
                    if isinstance(node, ast.Assign) and "re.compile" in ast.unparse(node.value)]
        assert not any("\\W" in p or "a-z0-9" in p for p in patterns), (module.__name__, patterns)

        toks = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "_toks")
        assert ast.unparse(toks.body[-1]) == "return scope_terms(" + toks.args.args[0].arg + ")", (
            f"{module.__name__} re-grew a private tokenizer")
