"""The already-answered block: what it publishes, what it must never publish, and the coverage
of the tools it was built for.

These drive the PROPERTY (tier 1 of the guard-test ladder in CLAUDE.md) rather than pinning the
prompt's text: every assertion here builds real providers over real (empty) directories and reads
what a real `CompositeTools` merge produces. A pin on the wording would go green against a block
that had stopped covering half its tools.
"""
from __future__ import annotations

import json

import pytest

import looplab.agents as looplab_agents
from looplab.agents.answered_by_context import answered_by_context
from looplab.agents.tool_loop import CompositeTools
from looplab.core.models import RunState
from looplab.tools._base import coerce_inventory, collect_inventory, render_inventory
from looplab.tools.cross_run_tools import CrossRunTools
from looplab.tools.knowledge_tools import KnowledgeTools
from looplab.tools.run_tools import AllRunsTools, DataTools, RunTools, SiblingRunTools


class _RepoTask:
    """A task whose subject is source code: no dataset surface at all (the AlgoTune/repo shape)."""


def _cold_start_toolset(tmp_path):
    """The provider set a cold-start run actually composes, over empty stores."""
    memory = tmp_path / "memory"
    memory.mkdir()
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    runs = tmp_path / "runs"
    runs.mkdir()
    state = RunState(goal="g", direction="max")
    run_tools = RunTools()
    run_tools.bind_state(state)
    return CompositeTools([
        run_tools,
        DataTools(_RepoTask()),
        CrossRunTools(memory, audience="portfolio"),
        KnowledgeTools(str(knowledge)),
        SiblingRunTools(runs),
        AllRunsTools(runs),
    ])


# The tools that produced the empty tail this whole mechanism exists for, measured 2026-08-19 over
# six cold-start runs (138 of 227 calls returned nothing). Each must be answerable from the prompt,
# or the block is publishing rows for the tools nobody was wasting calls on.
_MEASURED_EMPTY_TAIL = (
    "read_asset", "cross_run_search", "read_concept_tree", "data_schema", "list_themes",
    "list_notes", "data_profile", "list_experiments", "cross_run_prior_attempts", "grep",
    "cross_run_atlas", "cross_run_concept_map", "find_analogous_across_runs",
    "find_concept_slugs", "cross_run_claims", "read_run_experiment", "read_research_memo",
    "read_sibling_experiment",
)


def test_every_tool_of_the_measured_empty_tail_is_answered_by_the_block(tmp_path):
    rows = collect_inventory(_cold_start_toolset(tmp_path))
    missing = [name for name in _MEASURED_EMPTY_TAIL if name not in rows]
    assert not missing, f"no inventory row for {missing}; these are the tools the block exists for"


def test_a_cold_start_publishes_zero_and_not_unknown_for_every_empty_store(tmp_path):
    """An empty store is a KNOWN emptiness. Reporting it as UNKNOWN would be safe but useless —
    the model is told it may still be worth a call, which is the behaviour being removed."""
    rows = collect_inventory(_cold_start_toolset(tmp_path))
    not_zero = {name: value for name, value in rows.items()
                if name in _MEASURED_EMPTY_TAIL and value != 0}
    assert not not_zero, f"expected a decisive 0 on a cold start, got {not_zero}"


def test_the_block_never_names_a_tool_the_toolset_does_not_route(tmp_path):
    """Every row must name a tool the model can actually call.

    `CrossRunTools.inventory` answers for all eight of its tools whether or not `specs()` published
    them, so this is a live filter and not a tautology."""
    tools = _cold_start_toolset(tmp_path)
    routed = {(spec.get("function") or {}).get("name") for spec in tools.specs()}
    stray = sorted(set(collect_inventory(tools)) - routed)
    assert not stray, f"published a count for unroutable tool(s): {stray}"


def test_a_shadowed_provider_never_supplies_the_count_for_a_name_it_cannot_serve():
    """First-wins routing and first-wins inventory must be the SAME first."""
    class First:
        def specs(self):
            return [{"type": "function", "function": {"name": "dup", "parameters": {}}}]

        def execute(self, name, args):
            return "first"

        def inventory(self):
            return {"dup": 0}

    class Second:
        def specs(self):
            return [{"type": "function", "function": {"name": "dup", "parameters": {}}}]

        def execute(self, name, args):
            return "second"

        def inventory(self):
            return {"dup": 99}

    tools = CompositeTools([First(), Second()])
    assert tools.execute("dup", {}) == "first"
    assert tools.inventory() == {"dup": 0}, "the count must come from the provider that answers"


def test_a_provider_that_raises_contributes_nothing_rather_than_a_zero():
    class Broken:
        def specs(self):
            return [{"type": "function", "function": {"name": "boom", "parameters": {}}}]

        def execute(self, name, args):
            return ""

        def inventory(self):
            raise RuntimeError("nope")

    assert collect_inventory(Broken()) == {}
    assert answered_by_context(CompositeTools([Broken()])) == ""


def test_a_provider_without_the_hook_is_silent_and_the_block_disappears():
    class Legacy:
        def specs(self):
            return [{"type": "function", "function": {"name": "old", "parameters": {}}}]

        def execute(self, name, args):
            return ""

    assert collect_inventory(Legacy()) == {}
    assert answered_by_context(CompositeTools([Legacy()])) == ""
    assert answered_by_context(None) == ""


@pytest.mark.parametrize("value", [True, False, -1, "", "   ", None, 1.5])
def test_ill_formed_inventory_values_are_dropped_not_rendered(value):
    """A bool would render as the count 1 and a negative count claims fewer than none."""
    assert coerce_inventory({"t": value}) == {}


def test_unknown_renders_as_unknown_and_a_count_renders_as_a_number():
    out = render_inventory({"a": 0, "b": 7, "c": "unreadable store: OSError"})
    assert "a=0" in out and "b=7" in out
    assert "c=UNKNOWN(unreadable store: OSError)" in out
    assert "c=0" not in out, "an uncountable store must never be published as an empty one"


def test_an_unavailable_concept_projection_is_unknown_and_never_a_zero(monkeypatch):
    """`run_tools._themes` refuses to call an empty projection an empty taxonomy; the count must
    refuse the same thing, or the block asserts what the tool declines to assert."""
    state = RunState(goal="g", direction="max")
    tools = RunTools()
    tools.bind_state(state)

    class _Projection:
        status = "unavailable"
        reasons = ("test",)
        memberships: dict = {}
        run_base: tuple = ()
        trusted_memberships: dict = {}

    monkeypatch.setattr(RunTools, "_concept_projection", staticmethod(lambda st: _Projection()))
    rows = tools.inventory()
    for name in ("list_themes", "read_concept_tree", "concept_nodes", "node_concepts"):
        assert isinstance(rows[name], str), f"{name} must be UNKNOWN, not a count"
        assert "unavailable" in rows[name]


def test_an_unreadable_cross_run_store_is_unknown_not_empty(tmp_path, monkeypatch):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "lessons.jsonl").write_text("{}\n", encoding="utf-8")

    real_open = type(memory).open

    def _boom(self, *a, **k):
        if self.name == "lessons.jsonl":
            raise OSError("denied")
        return real_open(self, *a, **k)

    monkeypatch.setattr(type(memory), "open", _boom)
    rows = CrossRunTools(memory, audience="portfolio").inventory()
    assert isinstance(rows["cross_run_search"], str) and "unreadable" in rows["cross_run_search"]
    # `cross_run_prior_attempts` reads only the capsule store, which is still readable.
    assert rows["cross_run_prior_attempts"] == 0


# ---- the spend ceiling ---------------------------------------------------------------------
#
# `llm_budget_usd` ships in the same change because it is the same question asked about money
# rather than about calls: what does this run get, and how is that comparable to another loop's.


def test_the_spend_ceiling_reaches_the_accountant_and_zero_means_no_limit():
    from looplab.core.config import Settings
    from looplab.core.llm import make_llm_client

    common = dict(llm_model="m", llm_base_url="http://localhost:1/v1", llm_api_key="k")
    assert make_llm_client(Settings(**common)).accountant.limit is None, (
        "0.0 must stay the historical no-limit behaviour, not a ceiling of zero")
    assert make_llm_client(Settings(llm_budget_usd=0.25, **common)).accountant.limit == 0.25


def test_the_ceiling_stops_the_run_rather_than_degrading_it():
    """`BudgetExceeded` is a hard stop every agent path propagates; the accountant must raise it
    AT the limit, not past it, and must commit the spend it is refusing to exceed."""
    from looplab.core.llm import BudgetExceeded, CostAccountant

    acc = CostAccountant(limit=0.10)
    acc.add(0.04)
    acc.add(0.05)
    assert acc.remaining() == pytest.approx(0.01)
    with pytest.raises(BudgetExceeded):
        acc.add(0.02)
    assert acc.remaining() == 0.0, "a refused call still spent what it spent"


def test_no_limit_never_raises():
    from looplab.core.llm import CostAccountant

    acc = CostAccountant()
    for _ in range(50):
        acc.add(1.0)
    assert acc.remaining() is None


# ---- the other half of the fix: the tools' own answers must be TERMINAL --------------------
#
# A correct count is defeated by an answer the model can read as a near-miss. Both of these were
# measured retrying against a published zero.


def test_read_asset_states_the_class_of_the_emptiness_not_a_near_miss():
    answer = DataTools(_RepoTask()).execute("read_asset", {"name": "solver.py"})
    assert "NO data assets at all" in answer
    assert "no name will change that" in answer
    # It must also close the door the retries were actually looking for.
    assert "reads source files" in answer


def test_an_empty_cross_run_store_says_so_rather_than_blaming_the_query(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    answer = CrossRunTools(memory, audience="portfolio").execute(
        "cross_run_search", {"query": "svm", "intent": "explore"})
    assert "store is EMPTY" in answer
    assert "no query will match" in answer


def test_every_agent_side_toolset_is_composed_through_the_one_helper():
    """`Settings.hide_empty_tools` is implemented on `CompositeTools`, so a call site that builds
    one by hand silently opts its whole phase out of the flag.

    That is not hypothetical. `make_deep_researcher` spelled the composition out itself while its
    own comment claimed it used "the same capability assembly as the Researcher/Strategist" — and
    the deep-research phase is where essentially every tool call of a cold-start run happens, so a
    run launched with the flag ON recorded `hide_empty_tools: true` in its config snapshot and was
    still offered every empty tool.

    AST, not a substring (CLAUDE.md tier 3): a commented-out `CompositeTools(providers)` must not
    fail this, and a live one must not pass it.
    """
    import ast
    import pathlib

    root = pathlib.Path(looplab_agents.__file__).parent
    offenders = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "CompositeTools"
                    and not any(kw.arg == "hide_empty_tools" for kw in node.keywords)):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "these build a toolset without going through `compose_tools`, so they cannot honour "
        f"hide_empty_tools: {offenders}")


def test_schema_and_profile_do_not_refer_the_model_into_equally_empty_tools():
    """A referral is only useful if the referent has something.

    `data_schema` used to answer "try read_asset or data_profile" on a task with no data surface at
    all — sending the model to two tools that are empty for the SAME reason, while the prompt was
    already publishing zeros for all three."""
    tools = DataTools(_RepoTask())
    for name in ("data_schema", "data_profile"):
        answer = tools.execute(name, {})
        assert "NO data assets at all" in answer, name
        assert "try read_asset" not in answer, name


def test_an_empty_knowledge_base_says_so_rather_than_blaming_the_pattern(tmp_path):
    knowledge = tmp_path / "kb"
    knowledge.mkdir()
    tools = KnowledgeTools(str(knowledge))
    for name, args in (("list_notes", {}), ("grep", {"pattern": "svm"})):
        answer = tools.execute(name, args)
        assert "NO knowledge notes at all" in answer, name
        assert "operator-authored" in answer, name
        # Scoped, not blanket: `kb_search` also reads the case store, so this sentence must not
        # claim there is nothing to read at all (see `tests/test_partials_wired.py`).
        assert "kb_search" in answer, name


def test_a_populated_knowledge_base_keeps_the_pattern_wording(tmp_path):
    """The terminal sentence must not swallow the ordinary 'your pattern missed' answer."""
    knowledge = tmp_path / "kb"
    knowledge.mkdir()
    (knowledge / "note.md").write_text("hello svm", encoding="utf-8")
    tools = KnowledgeTools(str(knowledge))
    assert tools.execute("list_notes", {}).strip() == "note.md"
    assert tools.execute("grep", {"pattern": "zzzz-no-such-thing"}) == "(no matches)"


def test_a_non_empty_store_keeps_the_query_wording(tmp_path):
    """The empty-store sentence must not swallow the ordinary 'nothing matched' answer."""
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "lessons.jsonl").write_text(
        json.dumps({"id": "x", "text": "unrelated", "scope": "shared"}) + "\n", encoding="utf-8")
    answer = CrossRunTools(memory, audience="portfolio").execute(
        "cross_run_search", {"query": "zzzz-no-such-thing", "intent": "explore"})
    assert "store is EMPTY" not in answer
