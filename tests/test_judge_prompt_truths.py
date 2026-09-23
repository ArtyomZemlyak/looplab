"""The loop's three judges are told only what their own call offers and what can actually arrive
(review 2026-09-22, Q-1 — the prompt contract census).

`agents/unified_agent.py`'s pilot, crash-triage judge and repair critic were rendered through the
shipped methods over the documented `drive_tool_loop` seam and read against the registries and the
toolset each call really has. Four statements did not hold:

  * the triage prompt's OPENING sentence says the error "is tagged with its kind: crash, timeout,
    oom, diverged, stalled, or needs_failed" — `oom` is a kind the engine has had no way to tag
    since 2026-08-20 (`failure_diagnosis.ENGINE_FINAL_REASONS` + `DIAGNOSABLE_ENGINE_REASONS` are
    every kind `_failure_reason` and the watchdogs produce), and the list leaves out six kinds the
    DEFAULT `inline_repair_reasons` hands this judge (setup, no_metric, drift, expect_failed,
    check_failed, not_learning). TAT-07 fixed the two diagnosis lists and left this one "for a
    decision" (doc 66 §6 item 12): the decision is that it names what can ARRIVE — the engine's own
    kinds, filtered by the run's repair gate — in the vocabulary's own order and with no count;
  * "Two kinds are the ENGINE's own watchdogs" — the engine's watchdog kills are three
    (`diverged`, `stalled` and the training monitor's `not_learning`, docs/guide/concepts.md), and a
    `not_learning` stop arrives on this very call with nothing saying what it is;
  * "`list_dir`/`find_files`/`grep`/`read_file` are rooted at the node's own workdir" is
    unconditional, while those four scouts reach the loop only when `diagnosis_tools` built them
    (`repair_log_tools` on — its LEGACY row is off — and a workdir that exists);
  * all three judges are handed `_state_brief(..., for_proposal=False)`, whose concept lines say
    "use delta mode only when…" or "You MUST set `concept_mode=\"full\"`…" — an authoring contract
    no judge can honour: none of the three emit schemas has a concept field. `for_proposal=False`
    already drops the board's CLAIM contracts for exactly that reason; the authoring sentence was
    missed.

`Settings.prompt_truths_judges` renders all four truthfully. OFF — every constructor default, and a
run whose snapshot predates the field — is the historical request byte for byte.

Everything is driven through the shipped methods over `looplab.agents.agent.drive_tool_loop`; no
client is called and no endpoint is reached.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from looplab.agents.state_brief import (CONCEPT_AUTHORING_CONTEXT_LINE,
                                        CONCEPT_AUTHORING_UNSAFE_LINE, _state_brief)
from looplab.agents.unified_agent import UnifiedAgent
from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings, settings_from_snapshot
from looplab.core.models import FAILURE_REASONS, NON_REPAIRABLE_REASONS, REPAIRABLE_REASONS
from looplab.engine.failure_diagnosis import (DIAGNOSABLE_ENGINE_REASONS, ENGINE_FINAL_REASONS,
                                              judge_prompt_truths_enabled)

ROOT = Path(__file__).resolve().parents[1]

# THE HISTORICAL BYTES, measured on origin/master `4c02f1da` (2026-09-23) before any line moved:
# the whole triage system prompt and user turn for `_drive`'s ask (the same two digests
# `tests/test_triage_kind_vocabulary.py` pins, from the same ask), and the two pieces this change
# splits (`_TRIAGE_SYSTEM_HEAD`, `_TRIAGE_SYSTEM_TAIL`) — a digest rather than `==` against the class
# attribute, because the attribute is what this change re-assembles.
_HISTORICAL_SYSTEM_SHA256 = "3d6ec5a69f775c45fbc66a5aab76291aa8d2c01bf1533a3cab678cba932dcf59"
_HISTORICAL_USER_SHA256 = "868d5f46db7ca2975e5a3e0235c0958d0a43cd762f3139cb85055f1b178739e0"
_HISTORICAL_HEAD_SHA256 = "c5411be3479c7547a3afe273500d1959b8f07ac18fdf7b7cc8938e21f2021733"
_HISTORICAL_TAIL_SHA256 = "e92f029bed678b382d3e4286c53340ac76e570b60f5fade24892c5c1df7f0fb7"

_NODE = type("N", (), {"id": 7, "code": "import torch\nprint(1)"})()
_ERROR = "[failure kind: check_failed]\nTraceback\nValueError: boom"
_SCOUTS = ("list_dir", "find_files", "grep", "read_file")
_OPENING = re.compile(r"\(the error is tagged with its kind(?:: (?P<tags>[^)]*))?\)")
_WATCHDOG_SENTENCE = re.compile(r"^    .*ENGINE's own watchdogs.*$", re.M)
_SIZE_WORD = r"(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _Tools:
    """A per-call toolset offering exactly `names` — what `diagnosis_tools` hands `triage_crash`."""

    def __init__(self, names):
        self._names = tuple(names)

    def specs(self):
        return [{"type": "function", "function": {"name": n, "description": n,
                                                  "parameters": {"type": "object"}}}
                for n in self._names]

    def execute(self, name, args):
        return ""


def _capture(monkeypatch):
    from looplab.agents import agent as agent_mod
    seen: dict = {}

    def fake_loop(client, tools, messages, emit_spec, **opts):
        seen.update(messages=messages, emit_spec=emit_spec, tools=tools)
        return opts["fallback"](messages)

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    return seen


def _agent(**ctor):
    return UnifiedAgent(researcher=object(), developer=object(), pilot_client=object(), **ctor)


def _triage(monkeypatch, agent=None, *, tools=None, brief="", **ctor):
    seen = _capture(monkeypatch)
    agent = agent or _agent(**ctor)
    agent.triage_crash(_NODE, _ERROR, 1, history="REPAIR HISTORY", stages_passed=1,
                       attempts_left=3, tools=tools, brief=brief)
    assert seen, "the ask never reached the tool loop"
    return seen["messages"][0]["content"], seen["messages"][1]["content"], seen


def _offered(seen) -> set:
    names = set()
    for spec in (seen["tools"].specs() if seen["tools"] is not None else []):
        names.add((spec.get("function") or {}).get("name"))
    names.add(seen["emit_spec"]["function"]["name"])
    return names


def _arriving(repair_reasons=REPAIRABLE_REASONS) -> list:
    """The kinds that can reach this judge: every kind the engine itself tags, through the run's
    repair gate, in the closed vocabulary's order. Derived here from the registries, never copied."""
    engine = set(ENGINE_FINAL_REASONS) | set(DIAGNOSABLE_ENGINE_REASONS)
    return [r for r in FAILURE_REASONS
            if r in engine and r in repair_reasons and r not in NON_REPAIRABLE_REASONS]


def _opening_tags(system: str) -> list:
    m = _OPENING.search(system)
    assert m, "the opening no longer states the tag"
    raw = (m.group("tags") or "").replace(", or ", ", ").replace(" or ", ", ")
    return [t.strip() for t in raw.split(",") if t.strip()]


def _run_state():
    from looplab.core.models import RunState
    return RunState(goal="minimize (x-3)^2", direction="min")


# ------------------------------------------------------------------ OFF: the historical request

def test_off_is_the_historical_request_byte_for_byte(monkeypatch):
    """Every constructor default, and what a pre-field run resumes into. MUTATION: move one byte of
    the triage prompt's split pieces, or apply any ON rendering while OFF -> red."""
    system, user, _ = _triage(monkeypatch)
    assert _sha(system) == _HISTORICAL_SYSTEM_SHA256
    assert _sha(user) == _HISTORICAL_USER_SHA256
    assert _sha(UnifiedAgent._TRIAGE_SYSTEM_HEAD) == _HISTORICAL_HEAD_SHA256
    assert _sha(UnifiedAgent._TRIAGE_SYSTEM_TAIL) == _HISTORICAL_TAIL_SHA256
    # Explicitly OFF is the same request as OFF by default, whatever toolset the call brings.
    assert _triage(monkeypatch, prompt_truths_judges=False)[:2] == (system, user)
    brief = _state_brief(_run_state(), None, for_proposal=False)
    assert CONCEPT_AUTHORING_CONTEXT_LINE in brief
    _, user_off, _ = _triage(monkeypatch, brief=brief, tools=_Tools(["read_log"]))
    assert user_off.startswith(brief + "\n")


def test_the_defects_are_measured_on_the_historical_request(monkeypatch):
    """What the flag exists to end, measured on the OFF bytes rather than restated."""
    system, _, seen = _triage(monkeypatch, tools=None)
    tags = _opening_tags(system)
    # `oom` is named although no producer can tag it, and six kinds that DO arrive are missing.
    assert "oom" in tags and "oom" not in set(ENGINE_FINAL_REASONS) | set(DIAGNOSABLE_ENGINE_REASONS)
    assert sorted(set(_arriving()) - set(tags)) == sorted(
        ["check_failed", "drift", "expect_failed", "no_metric", "not_learning", "setup"])
    # "Two kinds are the ENGINE's own watchdogs" — while `not_learning` is a watchdog kill that
    # arrives here too, and is named nowhere in that sentence.
    watch = _WATCHDOG_SENTENCE.search(system).group(0)
    assert watch.lstrip().startswith("Two kinds are") and "'not_learning'" not in watch
    assert "not_learning" in ENGINE_FINAL_REASONS and "not_learning" in _arriving()
    # The four workdir scouts are named while this call offers none of them.
    assert all(f"`{t}`" in system for t in _SCOUTS)
    assert not set(_SCOUTS) & _offered(seen)


def test_the_judges_brief_carries_an_authoring_contract_no_judge_schema_holds(monkeypatch):
    """The historical half of the fourth defect: the concept-authoring line reaches all three
    judges while none of their emit schemas has a concept field."""
    brief = _state_brief(_run_state(), None, for_proposal=False)
    assert CONCEPT_AUTHORING_CONTEXT_LINE in brief.split("\n")
    _, user, seen = _triage(monkeypatch, brief=brief)
    props = seen["emit_spec"]["function"]["parameters"]["properties"]
    # "use delta mode only when…" is an instruction about `concept_mode`, the Idea field; the partial
    # variant names the field outright. Neither field exists in this call's schema.
    assert CONCEPT_AUTHORING_CONTEXT_LINE in user and "delta mode" in CONCEPT_AUTHORING_CONTEXT_LINE
    assert "concept_mode" in CONCEPT_AUTHORING_UNSAFE_LINE
    assert not any("concept" in p for p in props)


# ------------------------------------------------------------------ ON: what arrives, what is offered

def test_on_the_opening_names_exactly_the_kinds_that_can_arrive(monkeypatch):
    """THE DEFECT. MUTATION: render the opening from `FAILURE_REASONS` unfiltered, keep `oom`, or
    drop the repair-gate filter -> red."""
    system, _, _ = _triage(monkeypatch, prompt_truths_judges=True)
    tags = _opening_tags(system)
    assert tags == _arriving()
    assert "oom" not in tags and "rules_violation" not in tags
    assert not re.search(r"\b" + _SIZE_WORD + r" kinds\b", system.split("YOU ARE ALSO")[0], re.I)


def test_on_the_opening_follows_the_runs_own_repair_gate(monkeypatch):
    """A kind the run does not repair never reaches this call, so it is not named — the list is the
    run's, not the default's. MUTATION: ignore `triage_repair_reasons` -> red."""
    narrow = ("crash", "timeout", "oom", "rules_violation")
    system, _, _ = _triage(monkeypatch, prompt_truths_judges=True, triage_repair_reasons=narrow)
    assert _opening_tags(system) == ["crash", "timeout"] == _arriving(narrow)
    # No watchdog kind arrives through that gate, so no watchdog sentence is rendered at all.
    assert not _WATCHDOG_SENTENCE.search(system)


def test_on_the_watchdog_sentence_names_every_watchdog_kind_that_arrives(monkeypatch):
    """Three kinds, each with its own words, and no count. The two historical kinds keep their
    words byte for byte. MUTATION: drop `not_learning` from the words table, or put a count back ->
    red."""
    system, _, _ = _triage(monkeypatch, prompt_truths_judges=True)
    watch = _WATCHDOG_SENTENCE.search(system).group(0)
    named = re.findall(r"'([a-z_]+)' means", watch)
    assert named == ["diverged", "stalled", "not_learning"]
    assert set(named) <= set(ENGINE_FINAL_REASONS)
    assert not re.search(r"\b(?:two|three|both|neither)\b", watch, re.I)
    off = _WATCHDOG_SENTENCE.search(_triage(monkeypatch)[0]).group(0)
    for kind in ("diverged", "stalled"):
        words = UnifiedAgent._TRIAGE_WATCHDOG_MEANS[kind]
        assert f"'{kind}' means {words}" in watch and f"'{kind}' means {words}" in off
    # A gate that admits only one watchdog kind names only that one.
    one, _, _ = _triage(monkeypatch, prompt_truths_judges=True,
                        triage_repair_reasons=("crash", "stalled"))
    assert re.findall(r"'([a-z_]+)' means", _WATCHDOG_SENTENCE.search(one).group(0)) == ["stalled"]


def test_on_the_workdir_scouts_are_named_only_when_the_call_offers_them(monkeypatch):
    """MUTATION: render the scout clause unconditionally, or key it on ANY one scout -> red."""
    scouts = _Tools(["read_log", "metric_series", *_SCOUTS])
    with_them, _, seen = _triage(monkeypatch, prompt_truths_judges=True, tools=scouts)
    assert set(_SCOUTS) <= _offered(seen)
    assert "READ THE PROGRAM THIS EVAL ACTUALLY RAN" in with_them
    assert all(f"`{t}`" in with_them for t in _SCOUTS)
    for tools in (None, _Tools(["read_log", "metric_series"]), _Tools(["read_log", "read_file"])):
        without, _, seen = _triage(monkeypatch, prompt_truths_judges=True, tools=tools)
        assert "READ THE PROGRAM THIS EVAL ACTUALLY RAN" not in without
        for t in _SCOUTS:
            assert f"`{t}`" not in without or t in _offered(seen)
        assert "read the stage logs), then call `triage_crash` exactly once" in without


def test_on_no_judge_is_handed_the_concept_authoring_contract(monkeypatch):
    """All three judges, both authoring lines. The concept DATA line stays — it is context, not an
    instruction. MUTATION: filter the brief in one of the three methods only -> red."""
    from looplab.core.models import RunState
    brief = "\n".join(["Goal: g", "UNTRUSTED_RECORDED_CONCEPT_DATA={}",
                       CONCEPT_AUTHORING_UNSAFE_LINE, CONCEPT_AUTHORING_CONTEXT_LINE, "Search so far"])
    agent = _agent(prompt_truths_judges=True)
    _, triage_user, _ = _triage(monkeypatch, agent=agent, brief=brief)
    seen = _capture(monkeypatch)
    agent.choose_action(RunState(goal="g"), [{"kind": "draft"}, {"kind": "improve", "parent_id": 0}],
                        {"kind": "draft"}, brief=brief)
    pilot_user = seen["messages"][1]["content"]
    seen = _capture(monkeypatch)
    agent.repair_critic(_NODE, brief=brief, trajectory="attempt 1: cause = crash", attempt=2)
    critic_user = seen["messages"][1]["content"]
    for user in (triage_user, pilot_user, critic_user):
        assert CONCEPT_AUTHORING_UNSAFE_LINE not in user
        assert CONCEPT_AUTHORING_CONTEXT_LINE not in user
        assert "UNTRUSTED_RECORDED_CONCEPT_DATA={}" in user and "Search so far" in user
    # OFF, every judge still carries both, exactly where the brief put them.
    agent_off = _agent()
    seen = _capture(monkeypatch)
    agent_off.choose_action(RunState(goal="g"), [{"kind": "draft"}, {"kind": "improve",
                                                                      "parent_id": 0}],
                            {"kind": "draft"}, brief=brief)
    assert seen["messages"][1]["content"].startswith(brief + "\nLegal actions:")


def test_on_moves_only_the_four_pieces_and_nothing_else(monkeypatch):
    """No other word of any judge's request moves: the triage system prompt differs from OFF only
    in the opening list, the watchdog sentence and the scout clause; its user turn only by the
    dropped brief lines; the emit schemas and the pilot/critic system prompts are untouched."""
    brief = _state_brief(_run_state(), None, for_proposal=False)
    off_sys, off_user, off_seen = _triage(monkeypatch, brief=brief)
    on_sys, on_user, on_seen = _triage(monkeypatch, brief=brief, prompt_truths_judges=True)
    assert on_seen["emit_spec"] == off_seen["emit_spec"]
    strip = (lambda s: _WATCHDOG_SENTENCE.sub("", _OPENING.sub("()", s))
             .replace(UnifiedAgent._TRIAGE_SCOUTS_HISTORICAL, ""))
    assert strip(on_sys) == strip(off_sys)
    kept = "\n".join(line for line in brief.split("\n")
                     if line not in (CONCEPT_AUTHORING_UNSAFE_LINE, CONCEPT_AUTHORING_CONTEXT_LINE))
    assert on_user == off_user.replace(brief, kept, 1)
    assert UnifiedAgent._PILOT_SYSTEM and UnifiedAgent._REPAIR_CRITIC_SYSTEM   # untouched classes


def test_it_composes_with_the_registry_kind_lists(monkeypatch):
    """TAT-07's flag moves the middle of the same prompt; the two compose in either order and each
    keeps its own pieces."""
    both, _, _ = _triage(monkeypatch, prompt_truths_judges=True, triage_kinds_from_registry=True)
    kinds_only, _, _ = _triage(monkeypatch, triage_kinds_from_registry=True)
    assert _opening_tags(both) == _arriving()
    assert UnifiedAgent._TRIAGE_ANSWER_INTRO in both and UnifiedAgent._TRIAGE_ANSWER_INTRO in kinds_only
    assert _opening_tags(kinds_only) == ["crash", "timeout", "oom", "diverged", "stalled",
                                         "needs_failed"]


@pytest.mark.parametrize("flag", [False, True])
def test_a_prompt_store_override_still_wins(monkeypatch, tmp_path, flag):
    """The flag decides the DEFAULT `render()` is handed, never whether `triage_system.md` is read.
    The brief filter is not a PromptStore key, so it still follows the flag."""
    from looplab.core.prompts import PromptStore
    (tmp_path / "triage_system.md").write_text("HOUSE TRIAGE PROMPT", encoding="utf-8")
    agent = UnifiedAgent(researcher=object(), developer=object(), pilot_client=object(),
                         prompts=PromptStore(str(tmp_path)), prompt_truths_judges=flag)
    brief = _state_brief(_run_state(), None, for_proposal=False)
    system, user, _ = _triage(monkeypatch, agent=agent, brief=brief)
    assert system == "HOUSE TRIAGE PROMPT"
    assert (CONCEPT_AUTHORING_CONTEXT_LINE in user) is (not flag)


# ------------------------------------------------------------------ the switch

def test_the_flag_is_on_for_new_runs_and_off_for_a_pre_field_snapshot():
    assert Settings().prompt_truths_judges is True
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["prompt_truths_judges"] is False
    legacy = Settings().masked_snapshot()
    for field in LEGACY_CONFIG_SNAPSHOT_DEFAULTS:
        legacy.pop(field, None)
    legacy.pop("config_snapshot_schema", None)
    assert settings_from_snapshot(legacy).prompt_truths_judges is False
    assert settings_from_snapshot(Settings().masked_snapshot()).prompt_truths_judges is True


def test_one_reader_and_every_default_off_path_reads_off():
    assert judge_prompt_truths_enabled(Settings()) is True
    assert judge_prompt_truths_enabled(Settings(prompt_truths_judges=False)) is False
    assert judge_prompt_truths_enabled(object()) is False            # a duck-typed stub
    bare = UnifiedAgent.__new__(UnifiedAgent)                          # the `object.__new__` path
    assert bare._prompt_truths_judges is False
    assert tuple(bare._triage_repair_reasons) == tuple(REPAIRABLE_REASONS)


@pytest.mark.parametrize("case", ["on", "off", "pre_field_snapshot"])
def test_the_run_settings_reach_the_judges_through_the_factory(monkeypatch, case):
    """Settings -> `agents/factory.py::build_unified_agent` -> the facade -> the rendered request,
    end to end, INCLUDING the run's own repair gate. MUTATION: drop either factory keyword -> red."""
    from looplab.adapters.tasks import build_unified_agent, load_task

    narrow = ["crash", "timeout", "not_learning"]
    if case == "pre_field_snapshot":
        snapshot = Settings(backend="llm", unified_agent=True,
                            inline_repair_reasons=narrow).masked_snapshot()
        for field in LEGACY_CONFIG_SNAPSHOT_DEFAULTS:
            if field not in ("unified_agent", "inline_repair_reasons"):
                snapshot.pop(field, None)
        snapshot.pop("config_snapshot_schema", None)
        settings = settings_from_snapshot(snapshot)
    else:
        settings = Settings(backend="llm", unified_agent=True, inline_repair_reasons=narrow,
                            prompt_truths_judges=(case == "on"))
    task = load_task(ROOT / "examples" / "code_regression_task.json")
    agent = build_unified_agent(task, settings)
    system, _, _ = _triage(monkeypatch, agent=agent)
    if case == "on":
        assert _opening_tags(system) == ["crash", "timeout", "not_learning"]
    else:
        assert _opening_tags(system) == ["crash", "timeout", "oom", "diverged", "stalled",
                                         "needs_failed"]
        assert "Two kinds are the ENGINE's own watchdogs" in system


def test_the_words_table_names_only_engine_watchdog_kinds():
    """Every kind the watchdog sentence can name is one the ENGINE owns — a diagnosed-only kind
    there would re-create the `oom` defect one sentence down."""
    assert set(UnifiedAgent._TRIAGE_WATCHDOG_MEANS) <= set(ENGINE_FINAL_REASONS)
    assert set(UnifiedAgent._TRIAGE_WATCHDOG_MEANS) == {"diverged", "stalled", "not_learning"}
    json.dumps(UnifiedAgent._TRIAGE_WATCHDOG_MEANS)                    # plain strings only
