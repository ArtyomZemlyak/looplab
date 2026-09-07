"""The proposal cues are a registry, and their ORDER is the contract (doc 25 EC-08).

`_set_complexity_hint` was a 255-line linear assembler: fourteen independent cue blocks, each doing
`hint += ...` plus `steering.append({...})` inline, so every new engine signal grew the same
function and no cue could be exercised on its own. Each cue is now a method returning
`(hint_fragment, steering_entries)` and `PROPOSAL_CUES` lists them in order.

Order is a contract twice over — `hint` is prompt text the Researcher reads top-down, and `steering`
becomes the Card's public `_steering_context` list — so a reordering changes behaviour even though
every fragment is byte-identical. The registry is also a duck-typed seam of exactly the kind
CLAUDE.md requires a two-way guard for: a cue method not listed is silently dead, and a listed name
that does not resolve raises only when that gate happens to be on.

The refactor itself was verified differentially against the pre-split implementation over 400
randomised gate combinations (identical `_complexity_hint`, `_steering_context`, `_sweep_hint` and
`_cross_run_advisory_receipt`); that harness needs both versions of the module, so what is pinned
HERE is the structure that keeps it true.
"""
from __future__ import annotations

import ast
import inspect
import re

import pytest

from looplab.core.models import RunState
from looplab.engine.proposal_cues import ProposalCuesMixin

# The cue order as it ships. A literal on purpose: this IS the prompt's section order and the Card's
# steering order, so a change here must be a deliberate re-pin, not a silent consequence.
EXPECTED_ORDER = (
    "_cue_complexity",
    "_cue_eval_budget",
    # RE-PINNED 2026-08-29, deliberately. `_cue_llm_budget` sits immediately after the eval-seconds
    # cue because it is the same sentence about a different currency, and it goes BEFORE everything
    # that describes what to build: a proposer has to know what it can afford before it reads what
    # went wrong last time. Measured reason: 50 of 50 finished probe runs end on `budget_exhausted`
    # and 0 on eval seconds, while `propose`/`repropose`/`plan` saw a money figure in 0 of 7,006
    # resolved prompts and `plan_step` saw one in 72.8 % of 8,298.
    "_cue_llm_budget",
    "_cue_experiment_time_budget",
    "_cue_gpu_contract",
    "_cue_failure_reflection",
    "_cue_watchdog_reflection",
    "_cue_trust_reflection",
    "_cue_fault_localization",
    "_cue_feature_engineering",
    "_cue_reflection_prior",
    "_cue_research_memo",
    "_cue_cross_run_advisory",
    "_cue_cross_run_tools",
    "_cue_concept_authoring",
    "_cue_concept_slug_reuse",
)


class _Host(ProposalCuesMixin):
    """An engine-shaped host with every cue gated OFF, so a test can turn exactly one back on."""

    def __init__(self):
        self.researcher = type("R", (), {})()
        self._complexity_cue = False
        self._budget_aware = False
        self.max_eval_seconds = None
        self._repo_spec = {}
        self._gpu_ids = None
        self._eval_parallel = 1
        self.timeout = 30.0
        self._eval_spec = {}
        self._failure_reflection = False
        self._watchdog_reflection = False
        self._localize_faults = False
        self._feature_engineering = False
        self.task_has_columns = False
        self._assets = None
        self._prior_note_text = ""
        self._advisory_lock = None
        self._cross_run_advisory_receipt = {}
        self._concept_run_base = False
        self._cross_run_read_tools = False
        self.memory_dir = ""
        self._concept_pivot = False
        self._prefer_sweep = False
        self._novelty_stance = "off"
        self._strategy_fidelity = None

    # Stubbed so this file tests the REGISTRY, not the two text builders (which have their own
    # tests); both are empty in the default, advisory-off configuration anyway.
    def _cross_run_advisory_text(self, state):
        return ""

    def _cross_run_pointer_text(self):
        return ""

    def _stamp_novelty_hint(self, state, stance, researcher=None):
        pass


def _cue_methods_in_source() -> set:
    """Every `def _cue_*` the module defines, read off the source rather than `dir()` — a method
    inherited from somewhere else is not this registry's business."""
    tree = ast.parse(inspect.getsource(ProposalCuesMixin))
    return {node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name.startswith("_cue_")}


# ------------------------------------------------------------------ the registry is two-way

def test_every_cue_method_is_registered():
    """An unregistered cue is DEAD: the driver only calls what the tuple names, so the signal it
    renders silently stops reaching the Researcher — with no error anywhere."""
    unlisted = _cue_methods_in_source() - set(ProposalCuesMixin.PROPOSAL_CUES)
    assert unlisted == set(), (
        f"{sorted(unlisted)} defined but not in PROPOSAL_CUES, so the cue never renders")


def test_every_registered_name_resolves_to_a_cue_method():
    """The other direction. A typo'd or renamed entry raises AttributeError only on the run where
    that gate happens to be on — which is exactly the run nobody is watching."""
    for name in ProposalCuesMixin.PROPOSAL_CUES:
        assert callable(getattr(ProposalCuesMixin, name, None)), f"{name} does not resolve"
    assert set(ProposalCuesMixin.PROPOSAL_CUES) <= _cue_methods_in_source()


def test_the_registry_has_no_duplicates():
    """A duplicated entry renders its fragment twice and appends its steering entry twice."""
    listed = list(ProposalCuesMixin.PROPOSAL_CUES)
    assert len(listed) == len(set(listed))


def test_the_cue_order_is_pinned():
    assert tuple(ProposalCuesMixin.PROPOSAL_CUES) == EXPECTED_ORDER, (
        "the cue order changed. `hint` is prompt text read top-down and `steering` is the Card's "
        "public context list, so this is a behaviour change: re-pin deliberately")


# ------------------------------------------------------------------ the driver stays a driver

def test_the_driver_does_not_assemble_cues_inline_again():
    """The finding's shape. The loop appends fragments; a `hint +=` outside it is a cue growing back
    into the function the registry exists to keep small."""
    source = inspect.getsource(ProposalCuesMixin._set_complexity_hint)
    appends = re.findall(r"^\s*hint \+= (.*)$", source, flags=re.M)
    assert appends == ["_fragment"], f"_set_complexity_hint concatenates inline again: {appends}"
    # Two steering appends legitimately remain, and neither is a cue: they come AFTER the
    # `_complexity_hint` stamp because they contribute no hint TEXT — `sweep` stamps its own
    # `_sweep_hint` attribute, and `strategy` records the stance/fidelity the Strategist chose.
    # Pinned by kind rather than banned outright, so a new TEXT cue appending inline still fails.
    tail_kinds = re.findall(r"steering\.append\(\{\"kind\": \"(\w+)\"", source)
    tail_kinds += re.findall(r"^\s*steering\.append\((strategy_cue)\)", source, flags=re.M)
    assert tail_kinds == ["sweep", "strategy_cue"], (
        f"_set_complexity_hint appends steering inline again ({tail_kinds}); a cue returns its "
        "own entries")


def test_the_driver_calls_every_registered_cue_exactly_once(monkeypatch):
    calls = []
    host = _Host()
    for name in ProposalCuesMixin.PROPOSAL_CUES:
        monkeypatch.setattr(_Host, name,
                            (lambda n: lambda self, s, p, r: (calls.append(n), ("", []))[1])(name),
                            raising=False)

    host._set_complexity_hint(RunState(), None)

    assert calls == list(ProposalCuesMixin.PROPOSAL_CUES)


# ------------------------------------------------------------------ shape and gating

@pytest.mark.parametrize("name", EXPECTED_ORDER)
def test_a_gated_off_cue_contributes_nothing(name):
    """Each cue must return the empty pair rather than None when its gate is off — the driver
    concatenates and extends unconditionally, so a None would raise instead of no-op."""
    fragment, entries = getattr(_Host(), name)(RunState(), None, _Host().researcher)

    assert fragment == "", f"{name} renders text with its gate off"
    assert entries == [], f"{name} stamps steering with its gate off"


def test_all_cues_off_produces_an_empty_hint_and_no_steering():
    host = _Host()

    host._set_complexity_hint(RunState(), None)

    assert host.researcher._complexity_hint == ""
    assert host.researcher._steering_context == []


def test_one_cue_on_contributes_only_its_own_fragment():
    """The complexity cue is the cheapest to turn on alone; with no parent it counts roots."""
    host = _Host()
    host._complexity_cue = True

    host._set_complexity_hint(RunState(), None)

    assert host.researcher._complexity_hint == (
        "\nComplexity guidance: this branch already has 0 sibling experiment(s); "
        "propose a minimal baseline.")
    assert host.researcher._steering_context == [
        {"kind": "complexity", "siblings": 0, "level": "minimal"}]


def test_the_fragments_concatenate_in_registry_order(monkeypatch):
    """Not merely that each cue runs, but that its text lands where the registry says. A driver that
    collected fragments into a set, or sorted them, would still pass every test above."""
    host = _Host()
    count = len(ProposalCuesMixin.PROPOSAL_CUES)
    for index, name in enumerate(ProposalCuesMixin.PROPOSAL_CUES):
        # A real `kind` with a per-cue `siblings` counter: `normalize_steering_context` enforces a
        # CLOSED vocabulary and drops the whole snapshot on an unknown kind, so invented kinds would
        # make this assert an empty list against an empty list and prove nothing.
        monkeypatch.setattr(
            _Host, name,
            (lambda i: lambda self, s, p, r: (
                f"<{i}>", [{"kind": "complexity", "siblings": i, "level": "minimal"}]))(index),
            raising=False)

    host._set_complexity_hint(RunState(), None)

    assert host.researcher._complexity_hint == "".join(f"<{i}>" for i in range(count))
    assert [entry["siblings"] for entry in host.researcher._steering_context] == list(range(count))
