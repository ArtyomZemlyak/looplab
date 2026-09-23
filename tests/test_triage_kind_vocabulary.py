"""The crash-triage prompt names the failure kinds the REGISTRIES admit, and counts neither list
(review 2026-09-22, TAT-07; doc 50 AG-06).

`agents/unified_agent.py::_TRIAGE_SYSTEM` spelled its two failure-kind lists by hand, and both had
drifted from `engine/failure_diagnosis.py` — the registries the engine actually decides with:

  * the kinds the engine hands on for a diagnosis: "It is one of four", with `oom` among them — a
    kind the engine has had NO way to say since 2026-08-20 (`DIAGNOSABLE_ENGINE_REASONS` is
    `crash` / `no_metric` / `check_failed`; `oom` is answer-only);
  * the kinds the diagnostician may answer: "from these five:", then SIX bullets, then "Choose from
    those only." — while the emit schema's enum is `DIAGNOSED_FAILURE_REASONS`, seven members, and
    its own field description recommends the one the list leaves out (`check_false_positive`);
  * and the user turn asked for a diagnosis "if the kind tagged above is crash/oom/no_metric" —
    naming the kind that is never tagged and omitting `check_failed`, the one tag the
    `not_learning` and `diverged` bullets are ABOUT.

`Settings.triage_kinds_from_registry` renders both lists and that condition FROM the registries,
with no count word, so the next registry change cannot make the prompt false again. OFF — every
constructor default, and a run whose snapshot predates the field — is the historical prompt byte
for byte, because a prompt is a contract.

Everything is driven through the shipped `UnifiedAgent.triage_crash` over the documented
`looplab.agents.agent.drive_tool_loop` seam: no client is called and no endpoint is reached.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from looplab.agents.unified_agent import UnifiedAgent
from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings, settings_from_snapshot
from looplab.core.models import FAILURE_REASONS
from looplab.core.prompts import PromptStore
from looplab.engine.failure_diagnosis import (DIAGNOSABLE_ENGINE_REASONS, DIAGNOSED_CONTEXT_BOUND,
                                              DIAGNOSED_FAILURE_REASONS,
                                              kinds_from_registry_enabled)

ROOT = Path(__file__).resolve().parents[1]

# THE HISTORICAL BYTES, measured on the tree this flag was written against (origin/master
# `b2048ba3`, 2026-09-23; the same bytes on `f8cc4e98`) BEFORE any line of the prompt moved: the
# whole system prompt, and the whole user turn for the one ask `_drive` makes. A digest rather
# than `== _TRIAGE_SYSTEM`, because the class attribute is the thing this change re-assembled out of
# three pieces — comparing it with itself would prove nothing about the bytes a pre-field run was
# launched with.
_HISTORICAL_SYSTEM_SHA256 = "3d6ec5a69f775c45fbc66a5aab76291aa8d2c01bf1533a3cab678cba932dcf59"
_HISTORICAL_USER_SHA256 = "868d5f46db7ca2975e5a3e0235c0958d0a43cd762f3139cb85055f1b178739e0"

_NODE = type("N", (), {"id": 7, "code": "import torch\nprint(1)"})()
_ERROR = "[failure kind: check_failed]\nTraceback\nValueError: boom"
_BULLET = re.compile(r"^  - '([a-z_]+)'.*$", re.M)
_ANSWER_LIST_OPENS = "Answer with the kind that is TRUE"
# A cardinal that could state the SIZE of one of these lists, and the phrases that stated one.
_SIZE_WORD = (r"(?:\d+|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
              r"fourteen|fifteen)")
_LIST_SIZE = re.compile(r"\b(?:one of|these|those)\s+" + _SIZE_WORD + r"\b", re.I)


def _drive(monkeypatch, agent=None, **ctor):
    """One triage ask through the shipped method: `(system, user, emit_spec)` as the model gets
    them."""
    from looplab.agents import agent as agent_mod
    seen: dict = {}

    def fake_loop(client, tools, messages, emit_spec, **opts):
        seen.update(messages=messages, emit_spec=emit_spec)
        return opts["fallback"](messages)

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    if agent is None:
        agent = UnifiedAgent(researcher=object(), developer=object(), pilot_client=object(), **ctor)
    agent.triage_crash(_NODE, _ERROR, 1, history="REPAIR HISTORY", stages_passed=1,
                       attempts_left=3)
    assert seen, "the ask never reached the tool loop — nothing below would be checked"
    return seen["messages"][0]["content"], seen["messages"][1]["content"], seen["emit_spec"]


def _kind_lists(system: str) -> tuple[str, str]:
    """The engine-tag list and the answer list, as the prompt renders them.

    STATABLE rather than searched for: in either mode the prompt is `_TRIAGE_SYSTEM_HEAD` + the two
    lists + `_TRIAGE_SYSTEM_TAIL`, and the flag moves only the middle."""
    head, tail = UnifiedAgent._TRIAGE_SYSTEM_HEAD, UnifiedAgent._TRIAGE_SYSTEM_TAIL
    assert system.startswith(head) and system.endswith(tail)
    block = system[len(head):len(system) - len(tail)]
    tags, sep, answers = block.partition(_ANSWER_LIST_OPENS)
    assert sep, "the answer list lost its opening"
    return tags, sep + answers


def _bullets(text: str) -> dict[str, str]:
    """`{kind: its whole bullet line}` in the order the list names them, refusing a repeat."""
    found = [(m.group(1), m.group(0)) for m in _BULLET.finditer(text)]
    kinds = [kind for kind, _ in found]
    assert len(kinds) == len(set(kinds)), f"a kind is bulleted twice: {kinds}"
    return dict(found)


def _asked(user: str) -> list[str]:
    match = re.search(r"if the kind tagged above is (\S+), the failure_kind", user)
    assert match, "the user turn no longer asks for a diagnosis conditionally"
    return match.group(1).split("/")


def _uncounted(intro: str) -> str:
    """A list intro with its size taken out: "one of four" -> "one of these", "these five" ->
    "these". The two historical phrasings are the only two it has to know."""
    intro = re.sub(r"\bone of\s+" + _SIZE_WORD + r"\b", "one of these", intro, flags=re.I)
    return re.sub(r"\bthese\s+" + _SIZE_WORD + r"\b", "these", intro, flags=re.I)


# ------------------------------------------------------------------ OFF: the historical prompt

def test_off_is_the_historical_prompt_byte_for_byte(monkeypatch):
    """Every constructor default, and what a pre-field run resumes into. MUTATION: move one byte
    of `_TRIAGE_SYSTEM`'s three pieces, or render the user turn's condition from the registry
    while OFF -> red."""
    system, user, _ = _drive(monkeypatch)
    assert hashlib.sha256(system.encode("utf-8")).hexdigest() == _HISTORICAL_SYSTEM_SHA256
    assert hashlib.sha256(user.encode("utf-8")).hexdigest() == _HISTORICAL_USER_SHA256
    assert system == UnifiedAgent._TRIAGE_SYSTEM
    assert _asked(user) == ["crash", "oom", "no_metric"]
    # OFF said explicitly is the same prompt as OFF by default.
    assert _drive(monkeypatch, triage_kinds_from_registry=False)[:2] == (system, user)


def test_the_defect_is_measured_on_the_historical_prompt(monkeypatch):
    """What the flag exists to end, measured on the OFF bytes rather than restated — so a reader
    can see the three disagreements are real, and this file stops describing a prompt that has
    moved underneath it."""
    system, user, spec = _drive(monkeypatch)
    tags, answers = (_bullets(part) for part in _kind_lists(system))
    enum = spec["function"]["parameters"]["properties"]["failure_kind"]["enum"]
    assert "oom" in tags and "oom" not in DIAGNOSABLE_ENGINE_REASONS
    assert sorted(set(enum) - set(answers)) == ["check_false_positive"]
    assert _LIST_SIZE.findall(system) == ["one of four", "these five"]
    assert "oom" in _asked(user) and "check_failed" not in _asked(user)


# ------------------------------------------------------------------ ON: from the registries

def test_on_names_every_admissible_kind_and_no_other(monkeypatch):
    """THE DEFECT. MUTATION: render either list from its words table instead of its registry, or
    keep the historical block while ON -> red."""
    system, user, spec = _drive(monkeypatch, triage_kinds_from_registry=True)
    tags, answers = (_bullets(part) for part in _kind_lists(system))
    # The kinds the engine hands on for a diagnosis — and `oom` is never one of them.
    assert sorted(tags) == sorted(DIAGNOSABLE_ENGINE_REASONS)
    assert "oom" not in tags
    # The kinds the diagnostician may answer, which "Choose from those only." now tells the truth
    # about: exactly the enum the answer is read against, `check_false_positive` included.
    enum = spec["function"]["parameters"]["properties"]["failure_kind"]["enum"]
    assert sorted(answers) == sorted(DIAGNOSED_FAILURE_REASONS) == sorted(enum)
    assert set(tags) | set(answers) <= set(FAILURE_REASONS)
    # And the user turn asks for a diagnosis on exactly the tags that are diagnosable.
    assert _asked(user) == list(DIAGNOSABLE_ENGINE_REASONS)


def test_on_counts_neither_list(monkeypatch):
    """A count is what went stale twice: "one of four" and "from these five" were each true of an
    older registry. MUTATION: put a number word back in either intro -> red."""
    system, user, _ = _drive(monkeypatch, triage_kinds_from_registry=True)
    for intro in (UnifiedAgent._TRIAGE_TAG_INTRO, UnifiedAgent._TRIAGE_ANSWER_INTRO):
        assert not re.search(r"\b" + _SIZE_WORD + r"\b", intro, re.I), intro
    assert not _LIST_SIZE.search(system) and not _LIST_SIZE.search(user)


def test_the_lists_follow_the_registry_they_are_handed():
    """FROM the registry, in both directions: a kind the registry gains is NAMED — bare, until it is
    given words, which the next test refuses to ship — and a kind it loses is dropped whatever the
    words table still holds. Either way no size is stated."""
    grown = UnifiedAgent._triage_kind_lists(DIAGNOSABLE_ENGINE_REASONS + ("gpu_lost",),
                                            DIAGNOSED_FAILURE_REASONS + ("gpu_lost",))
    tags, sep, answers = grown.partition(_ANSWER_LIST_OPENS)
    assert "  - 'gpu_lost'\n" in tags and "  - 'gpu_lost'\n" in sep + answers
    assert not _LIST_SIZE.search(grown)
    shrunk = UnifiedAgent._triage_kind_lists(("crash",), ("oom", "crash"))
    tags, sep, answers = shrunk.partition(_ANSWER_LIST_OPENS)
    assert list(_bullets(tags)) == ["crash"]
    assert list(_bullets(sep + answers)) == ["oom", "crash"]


def test_every_admissible_kind_has_its_own_words_and_no_stale_words_remain():
    """Two-way, so the bare fallback above never ships and no description outlives its kind."""
    assert set(UnifiedAgent._TRIAGE_TAG_MEANS) == set(DIAGNOSABLE_ENGINE_REASONS)
    assert set(UnifiedAgent._TRIAGE_ANSWER_MEANS) == set(DIAGNOSED_FAILURE_REASONS)


def test_on_moves_the_lists_and_the_condition_and_no_other_word(monkeypatch):
    """"No other prompt wording changes": the text around the lists is the historical text, every
    kind both versions name keeps its historical bullet byte for byte, each intro differs only by
    its count, the one new bullet is worded by the shipped schema description, the schema itself is
    not this flag's, and the user turn differs only in its condition."""
    off_system, off_user, off_spec = _drive(monkeypatch)
    system, user, spec = _drive(monkeypatch, triage_kinds_from_registry=True)
    assert spec == off_spec
    assert user.replace("/".join(DIAGNOSABLE_ENGINE_REASONS), "crash/oom/no_metric") == off_user
    for off_part, on_part in zip(_kind_lists(off_system), _kind_lists(system)):
        off_lines, on_lines = _bullets(off_part), _bullets(on_part)
        shared = set(off_lines) & set(on_lines)
        assert shared, "nothing in common — this comparison would pass vacuously"
        assert {k: on_lines[k] for k in shared} == {k: off_lines[k] for k in shared}
        off_intro, on_intro = off_part.split("\n", 1)[0], on_part.split("\n", 1)[0]
        assert off_intro != on_intro and _uncounted(off_intro) == on_intro
    words = _bullets(_kind_lists(system)[1])["check_false_positive"].split("': ", 1)[1]
    assert words in spec["function"]["parameters"]["properties"]["failure_kind"]["description"]


def test_a_context_bound_kind_states_its_context_on_its_bullet(monkeypatch):
    """`DIAGNOSED_CONTEXT_BOUND` is the registry that admits `diverged` only under a tagged
    `check_failed`; a bullet naming the kind without its bound would invite the answer
    `diagnosed_failure_reason` then refuses as out of vocabulary."""
    system, _, _ = _drive(monkeypatch, triage_kinds_from_registry=True)
    answers = _bullets(_kind_lists(system)[1])
    for kind, contexts in DIAGNOSED_CONTEXT_BOUND.items():
        for context in contexts:
            assert f"'{context}'" in answers[kind], (kind, context)


@pytest.mark.parametrize("flag", [False, True])
def test_a_prompt_store_override_still_wins(monkeypatch, tmp_path, flag):
    """`render(prompts, "triage_system", default)` — the flag decides the DEFAULT, never whether an
    operator's `triage_system.md` is read. The user turn is not a PromptStore key, so its condition
    still follows the flag. MUTATION: build the ON prompt around `render()` -> red."""
    (tmp_path / "triage_system.md").write_text("HOUSE TRIAGE PROMPT", encoding="utf-8")
    agent = UnifiedAgent(researcher=object(), developer=object(), pilot_client=object(),
                         prompts=PromptStore(str(tmp_path)), triage_kinds_from_registry=flag)
    system, user, _ = _drive(monkeypatch, agent=agent)
    assert system == "HOUSE TRIAGE PROMPT"
    assert _asked(user) == (list(DIAGNOSABLE_ENGINE_REASONS) if flag
                            else ["crash", "oom", "no_metric"])


# ------------------------------------------------------------------ the switch

def test_the_flag_is_on_for_new_runs_and_off_for_a_pre_field_snapshot():
    """A pre-field snapshot resumes into the prompt it was launched with; a new run gets the
    registries. MUTATION: drop the LEGACY row -> red."""
    assert Settings().triage_kinds_from_registry is True
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["triage_kinds_from_registry"] is False
    legacy = Settings().masked_snapshot()
    for field in LEGACY_CONFIG_SNAPSHOT_DEFAULTS:
        legacy.pop(field, None)
    legacy.pop("config_snapshot_schema", None)
    assert settings_from_snapshot(legacy).triage_kinds_from_registry is False
    assert settings_from_snapshot(Settings().masked_snapshot()).triage_kinds_from_registry is True


def test_one_reader_and_every_default_off_path_reads_off():
    assert kinds_from_registry_enabled(Settings()) is True
    assert kinds_from_registry_enabled(Settings(triage_kinds_from_registry=False)) is False
    assert kinds_from_registry_enabled(object()) is False          # a duck-typed stub
    # The construction path tests use to drive one judge (`object.__new__`) reads the class default.
    assert UnifiedAgent.__new__(UnifiedAgent)._triage_kinds_from_registry is False


@pytest.mark.parametrize("case", ["on", "off", "pre_field_snapshot"])
def test_the_run_settings_reach_the_prompt_through_the_factory(monkeypatch, case):
    """Settings -> `agents/factory.py::build_unified_agent` -> the facade -> the rendered prompt,
    driven end to end. MUTATION: drop the factory's keyword -> the `on` case is red."""
    from looplab.adapters.tasks import build_unified_agent, load_task

    if case == "pre_field_snapshot":
        snapshot = Settings(backend="llm", unified_agent=True).masked_snapshot()
        for field in LEGACY_CONFIG_SNAPSHOT_DEFAULTS:
            if field != "unified_agent":
                snapshot.pop(field, None)
        snapshot.pop("config_snapshot_schema", None)
        settings = settings_from_snapshot(snapshot)
    else:
        settings = Settings(backend="llm", unified_agent=True,
                            triage_kinds_from_registry=(case == "on"))
    task = load_task(ROOT / "examples" / "code_regression_task.json")
    agent = build_unified_agent(task, settings)
    system, user, _ = _drive(monkeypatch, agent=agent)
    guard = UnifiedAgent._TRIAGE_EVIDENCE_GUARD
    system = system[:-len(guard)] if system.endswith(guard) else system
    if case == "on":
        assert "check_false_positive" in _bullets(_kind_lists(system)[1])
        assert _asked(user) == list(DIAGNOSABLE_ENGINE_REASONS)
    else:
        assert system == UnifiedAgent._TRIAGE_SYSTEM
        assert _asked(user) == ["crash", "oom", "no_metric"]
