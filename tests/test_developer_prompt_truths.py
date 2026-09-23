"""The Developer's prompts say only what holds (review 2026-09-22, Q-1 — the prompt contract census).

The repo Developer's phases and the dataset task's script brief were rendered through the shipped
code (the `looplab.agents.agent.drive_tool_loop` seam, the real `drive_tool_loop` for the bounce) and
read against the registries and the tools each call really has. Four statements did not hold:

  * the STAGES phase says "`expect` has two parts" and describes `files` and `assert` — prose and
    `declare_stages` schema both — while `runtime/command_eval.py::STAGE_EXPECT_KEYS` is the closed
    TRIPLE `(files, assert, numeric)` and `validate_stages` accepts `expect.numeric` from exactly the
    manifest this phase writes (doc 52 row 24): the one contract the engine checks WITHOUT a model
    was never offered to the role that declares the stages — and the write tools' own
    `declare_stages`, the route a repair re-declares the list through, described `expect` the same
    two-part way;
  * the implement/repair system prompt renders "=== CANONICAL COMMANDS (from the repo README …) ==="
    with an EMPTY body whenever the README yields no recipe — the RESULTS header beside it has been
    conditional all along;
  * a refused `declare_stages` / `done` is bounced with "Fix it and call it again with a valid,
    COMPLETE idea — never an empty one." — the Researcher's wording, reaching every loop that passes
    a validator, and with a doubled period whenever the validator's own sentence ends in one;
  * the dataset task's brief says packages are "auto-installed and the run retried, so build the
    model the idea actually calls for … rather than downgrading it to sklearn just to avoid an
    import", and then ends "If a library is missing, fall back to one that is available rather than
    crashing" — the one instruction that defeats the auto-install it was just told about, because an
    import the script catches never crashes and so never triggers the install.

`Settings.prompt_truths_developer` renders each truthfully. OFF — every constructor default, and a
run whose snapshot predates the field — is the historical bytes.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

from looplab.adapters.repo_task import EvalSpec, LLMRepoDeveloper, RepoTask
from looplab.adapters.repo_write_tools import RepoWriteTools
from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings, settings_from_snapshot
from looplab.core.hardware import AUTO_INSTALL_PROMISE
from looplab.core.models import Idea
from looplab.runtime.command_eval import STAGE_EXPECT_KEYS, validate_stages
from looplab.runtime.numeric_contract import NUMERIC_OPS, validate_numeric

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "repo_fixture"
_M = {"kind": "stdout_json", "key": "metric"}
_IDEA = Idea(operator="draft", params={"lr": 0.1}, rationale="a change")
_COMMANDS_HEADER = ("=== CANONICAL COMMANDS (from the repo README — adapt paths to absolute + your "
                    "hyperparameters) ===\n")

# THE HISTORICAL BYTES, measured on origin/master `4c02f1da` (2026-09-23) before any line moved: the
# stages-phase user turn for `_task()` / `_IDEA` with the two machine-dependent splices (the wall
# budget and the device-count note) held empty, and the `declare_stages` emit spec.
_HISTORICAL_STAGES_USER_SHA256 = "01d0e31ebdf51ce91cc03f161d96860cd857dbf472e7838cca4953c4be3abfe8"
_HISTORICAL_STAGES_SPEC_SHA256 = "05204b7da2bc0e9de7dcfd43be48c484a63242b5cfa61d4c438a3dd4c89009d8"
# The dataset brief with the auto-install capability sentence, and without it (the offline stack),
# with the dataset's ABSOLUTE PATH replaced by `<DATA>` before hashing (`_path_free`): the brief
# names the file it reads, so the raw bytes differ with the checkout directory. The first pins were
# taken over the raw bytes in one checkout and failed in every other; these were re-measured at the
# merge on origin/master `b87067fa`, the tree before this change.
_HISTORICAL_DATASET_BRIEF_CAPS_SHA256 = "d109adeb029851632812f961a6c3cdfa527fc3df7954f916bcfe7df59f865d1a"
_HISTORICAL_DATASET_BRIEF_NONE_SHA256 = "8cf3f6d3ce44faf0a83e100f38536cefd97b5984cef38fceaab65012ae711e81"
# The write tools' `declare_stages` spec (no operator stages), measured on the same commit.
_HISTORICAL_WRITE_DECLARE_SHA256 = "1725270dc6f079bd36ddf8b492a77221d911a0a53620a4847e859fd882e84332"
_FALLBACK_SENTENCE = ("If a library is missing, fall back to one that is available rather than "
                      "crashing.")


def _sha(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _path_free(brief: str, task) -> str:
    """The brief with the one machine-dependent splice — the dataset's absolute path — held fixed."""
    path = str(task._primary_path())
    assert brief.count(path) == 1, "the brief names its dataset exactly once"
    return brief.replace(path, "<DATA>")


def _task(command=("python", "main.py")):
    return RepoTask(id="r", goal="g", direction="max", editable_path=str(FIXTURE),
                    edit_surface=["*.py"], protect=[],
                    eval=EvalSpec(command=list(command), metric=_M))


def _dev(**kw):
    return LLMRepoDeveloper(object(), _task(), **kw)


def _stages_user(monkeypatch, dev) -> str:
    monkeypatch.setattr(LLMRepoDeveloper, "_time_budget_note", lambda self: "")
    monkeypatch.setattr(LLMRepoDeveloper, "_gpu_footprint_note", lambda self, idea: "")
    ev, has_cmd = dev._cmd_context()
    return dev._stages_user(_IDEA, ev, has_cmd)


def _expect_bullets(text: str) -> dict:
    """{part: its bullet line} for the `expect` list in the stages user turn, in order."""
    block = text.split("GIVE EVERY STAGE AN `expect`", 1)[1].split("A numeric bar", 1)[0]
    return {m.group(1): m.group(0) for m in re.finditer(r"^  • `([a-z]+)`:.*$", block, re.M)}


def _expect_schema(spec) -> dict:
    items = spec["function"]["parameters"]["properties"]["stages"]["items"]
    return items["properties"]["expect"]["properties"]


def _capture_phases(monkeypatch):
    import looplab.agents.agent as agent_mod
    seen: list = []

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        seen.append({"name": emit_spec["function"]["name"], "messages": list(messages),
                     "opts": opts, "tools": tools.specs() if tools is not None else []})
        if emit_spec["function"]["name"] == "declare_stages":
            return finalize({"stages": [{"name": "train", "command": ["python", "train.py"]}]})
        return finalize({"summary": "s"})

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    return seen


# ------------------------------------------------------------------ OFF: the historical bytes

def test_off_is_the_historical_stages_turn_and_schema(monkeypatch):
    """Every constructor default, and what a pre-field run resumes into. MUTATION: move one byte of
    the split `expect` pieces, or offer `numeric` while OFF -> red."""
    dev = _dev()
    assert _sha(_stages_user(monkeypatch, dev)) == _HISTORICAL_STAGES_USER_SHA256
    assert _sha(dev._stages_emit_spec()) == _HISTORICAL_STAGES_SPEC_SHA256
    assert _stages_user(monkeypatch, _dev(prompt_truths=False)) == _stages_user(monkeypatch, dev)


def test_the_defects_are_measured_on_the_historical_bytes(monkeypatch):
    dev = _dev()
    user = _stages_user(monkeypatch, dev)
    # "two parts" — the registry has three, and the validator ACCEPTS the third from this manifest.
    assert "`expect` has two parts" in user
    assert list(_expect_bullets(user)) == ["files", "assert"] != list(STAGE_EXPECT_KEYS)
    assert set(_expect_schema(dev._stages_emit_spec())) == {"files", "assert"}
    clean, err = validate_stages([{"name": "train", "command": ["python", "t.py"],
                                   "expect": {"numeric": [{"key": "epochs_completed", "op": ">=",
                                                           "value": 3}]}}])
    assert err is None and clean[0]["expect"]["numeric"]
    # The empty CANONICAL COMMANDS section (the fixture has no README recipe).
    seen = _capture_phases(monkeypatch)
    _dev(plan_decompose=False).implement(_IDEA)
    system = next(c for c in seen if c["name"] == "done")["messages"][0]["content"]
    assert _COMMANDS_HEADER + "\n\n=== REPOSITORY SOURCE" in system


def test_the_bounce_calls_a_manifest_an_idea_on_the_historical_request():
    """Driven through the REAL loop: a validator that refuses once, then accepts."""
    tool_messages = _bounce(reject_prompt=None)
    assert tool_messages[0] == ("Your `declare_stages` was NOT accepted: `stages` must be a "
                                "non-empty array.. Fix it and call it again with a valid, COMPLETE "
                                "idea — never an empty one.")


def test_the_dataset_brief_contradicts_its_own_capability_sentence_when_off():
    from looplab.adapters.dataset_task import DatasetTask
    from looplab.core.hardware import runtime_capabilities_brief
    task = DatasetTask(goal="g", data_path=str(ROOT / "examples/dataset_example/data.csv"))
    caps = runtime_capabilities_brief(auto_install=True, gpu=None)
    brief = task._brief(caps)
    assert _sha(_path_free(brief, task)) == _HISTORICAL_DATASET_BRIEF_CAPS_SHA256
    assert _sha(_path_free(task._brief(None), task)) == _HISTORICAL_DATASET_BRIEF_NONE_SHA256
    assert "auto-installed" in brief and "downgrading it to sklearn" in brief
    assert brief.endswith(_FALLBACK_SENTENCE)


# ------------------------------------------------------------------ ON

def test_on_the_expect_contract_is_the_registry_in_prose_and_schema(monkeypatch):
    """THE DEFECT. Every `STAGE_EXPECT_KEYS` member is a bullet AND a schema property, in the
    registry's order, and no count is stated. MUTATION: render from a hand list, or keep "two
    parts" -> red."""
    dev = _dev(prompt_truths=True)
    user = _stages_user(monkeypatch, dev)
    bullets = _expect_bullets(user)
    assert list(bullets) == list(STAGE_EXPECT_KEYS)
    assert list(_expect_schema(dev._stages_emit_spec())) == list(STAGE_EXPECT_KEYS)
    assert not re.search(r"\b(?:two|three|both)\b", user.split("GIVE EVERY STAGE AN `expect`")[1]
                         .split("  • `files`")[0], re.I)
    # The two historical bullets keep their words byte for byte.
    off = _expect_bullets(_stages_user(monkeypatch, _dev()))
    assert bullets["files"] == off["files"] and bullets["assert"] == off["assert"]


def test_on_the_numeric_part_is_described_by_its_own_validator(monkeypatch):
    """The schema's operator enum IS `NUMERIC_OPS`, the prose names the fail-closed rule, and the
    example the prose shows is a declaration the validator ACCEPTS — an example that did not
    validate would teach a refusal. MUTATION: widen the enum, or change the example -> red."""
    dev = _dev(prompt_truths=True)
    numeric = _expect_schema(dev._stages_emit_spec())["numeric"]
    assert numeric["items"]["properties"]["op"]["enum"] == list(NUMERIC_OPS)
    assert set(numeric["items"]["required"]) == {"key", "op", "value"}
    bullet = _expect_bullets(_stages_user(monkeypatch, dev))["numeric"]
    example = json.loads(re.search(r"(\[\{.*?\}\])", bullet).group(1))
    rels, err = validate_numeric("train", example)
    assert err is None and rels
    assert "never prints" in bullet and "FAILS" in bullet


def _expect_parts(stages_description: str) -> list:
    """The top-level part names inside `expect?:{…}` of a `declare_stages` description."""
    clause = stages_description.split("expect?:{", 1)[1].split("}, role?", 1)[0]
    parts, depth, cur = [], 0, ""
    for ch in clause:
        depth += (ch in "[{") - (ch in "]}")
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    return [re.match(r"\s*([a-z]+)", p).group(1) for p in parts + [cur]]


def _write_declare_description(tools_specs) -> str:
    spec = next(s for s in tools_specs if s["function"]["name"] == "declare_stages")
    return spec["function"]["parameters"]["properties"]["stages"]["description"]


def test_the_write_tools_declare_stages_names_every_expect_part_on(monkeypatch):
    """The route a REPAIR re-declares the manifest through ("passing the FULL corrected ordered
    list"). OFF — the default, and an instance that never ran `__init__` — is the historical spec by
    sha256, two parts; ON its `expect?:{…}` names every `STAGE_EXPECT_KEYS` member, so a list rebuilt
    from that shape keeps a `numeric` contract. Driven through `_run`, so the Developer's own flag is
    what reaches the tool. MUTATION: keep the two-part shape ON, or drop the Developer's keyword -> red."""
    off = RepoWriteTools(["*.py"], []).specs()
    assert _sha(next(s for s in off if s["function"]["name"] == "declare_stages")) == \
        _HISTORICAL_WRITE_DECLARE_SHA256
    assert _expect_parts(_write_declare_description(off)) == ["files", "assert"]
    assert RepoWriteTools.__new__(RepoWriteTools)._prompt_truths is False
    on = RepoWriteTools(["*.py"], [], prompt_truths=True).specs()
    assert _expect_parts(_write_declare_description(on)) == list(STAGE_EXPECT_KEYS)
    seen = _capture_phases(monkeypatch)
    for flag in (True, False):
        seen.clear()
        _dev(plan_decompose=False, prompt_truths=flag).repair(_IDEA, "", "boom")
        (call,) = seen
        assert ("numeric" in _expect_parts(_write_declare_description(call["tools"]))) is flag


def test_on_an_empty_commands_section_is_not_rendered(monkeypatch, tmp_path):
    """MUTATION: render the header unconditionally again -> red; a README recipe still renders it
    byte for byte."""
    seen = _capture_phases(monkeypatch)
    _dev(plan_decompose=False, prompt_truths=True).implement(_IDEA)
    system = next(c for c in seen if c["name"] == "done")["messages"][0]["content"]
    assert "CANONICAL COMMANDS" not in system and "=== REPOSITORY SOURCE" in system
    # A repo whose README yields a recipe keeps the section exactly as it always read.
    import shutil
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    (repo / "README.md").write_text("Train it:\npython train.py --epochs 3\n", encoding="utf-8")
    task = RepoTask(id="r", goal="g", direction="max", editable_path=str(repo),
                    edit_surface=["*.py"], protect=[], eval=EvalSpec(command=["python", "main.py"],
                                                                     metric=_M))
    for flag in (True, False):
        seen.clear()
        LLMRepoDeveloper(object(), task, plan_decompose=False, prompt_truths=flag).implement(_IDEA)
        system = next(c for c in seen if c["name"] == "done")["messages"][0]["content"]
        assert _COMMANDS_HEADER + "# Train it:\npython train.py --epochs 3\n\n" in system


def test_on_every_validated_phase_bounces_without_the_researchers_idea(monkeypatch):
    """The stages, the build and the repair sessions each pass a `reject_prompt` that does not call
    their emit an idea; OFF passes none (the loop's historical message, keyword absent). MUTATION:
    drop the keyword at one site, or pass it while OFF -> red."""
    seen = _capture_phases(monkeypatch)
    _dev(plan_decompose=False, prompt_truths=True).implement(_IDEA)
    _dev(plan_decompose=False, prompt_truths=True).repair(_IDEA, "", "boom")
    by_name: dict = {}
    for call in seen:
        if call["opts"].get("validate") is not None:
            by_name.setdefault(call["name"], []).append(call["opts"].get("reject_prompt"))
    assert set(by_name) == {"declare_stages", "done"}
    for prompts in by_name.values():
        assert all(p and "idea" not in p.lower() for p in prompts)
    seen.clear()
    _dev(plan_decompose=False).implement(_IDEA)
    assert all("reject_prompt" not in c["opts"] for c in seen)


def test_on_the_real_loop_bounces_without_the_idea_or_the_double_period():
    refusal = _bounce(reject_prompt="Your `{emit}` was NOT accepted: {err} Fix the manifest and "
                                    "call `{emit}` again.")[0]
    assert refusal == ("Your `declare_stages` was NOT accepted: `stages` must be a non-empty array. "
                       "Fix the manifest and call `declare_stages` again.")
    # A validator sentence with no final stop gets exactly one.
    refusal = _bounce(reject_prompt="{err} Again.", err="no stop here")[0]
    assert refusal == "no stop here. Again."


def test_on_the_dataset_brief_drops_only_the_contradicting_sentence():
    """With the auto-install sentence, the fallback line goes; without it (the offline stack, where
    a missing package really cannot arrive) the line is true and stays. MUTATION: drop it always,
    or keep it under the caps -> red."""
    from looplab.adapters.dataset_task import DatasetTask
    from looplab.core.hardware import runtime_capabilities_brief
    task = DatasetTask(goal="g", data_path=str(ROOT / "examples/dataset_example/data.csv"))
    caps = runtime_capabilities_brief(auto_install=True, gpu=None)
    on = task._brief(caps, prompt_truths=True)
    assert _FALLBACK_SENTENCE not in on
    assert on == task._brief(caps).replace(" " + _FALLBACK_SENTENCE, "")
    assert task._brief(None, prompt_truths=True) == task._brief(None)
    _, developer = task.llm_roles(object(), runtime_caps=caps, prompt_truths=True)
    assert developer.brief == on
    assert task.llm_roles(object(), runtime_caps=caps)[1].brief == task._brief(caps)


# ------------------------------------------------------------------ the switch

def test_the_flag_is_on_for_new_runs_and_off_for_a_pre_field_snapshot():
    assert Settings().prompt_truths_developer is True
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["prompt_truths_developer"] is False
    legacy = Settings().masked_snapshot()
    for field in LEGACY_CONFIG_SNAPSHOT_DEFAULTS:
        legacy.pop(field, None)
    legacy.pop("config_snapshot_schema", None)
    assert settings_from_snapshot(legacy).prompt_truths_developer is False


def test_one_reader_and_every_default_off_path_reads_off():
    from looplab.agents.developer_backends import developer_prompt_truths_enabled
    assert developer_prompt_truths_enabled(Settings()) is True
    assert developer_prompt_truths_enabled(Settings(prompt_truths_developer=False)) is False
    assert developer_prompt_truths_enabled(object()) is False
    assert LLMRepoDeveloper.__new__(LLMRepoDeveloper)._prompt_truths is False


@pytest.mark.parametrize("flag", [True, False])
def test_the_run_settings_reach_both_developers_through_the_factory(monkeypatch, flag):
    """Settings -> `make_roles` -> the repo Developer AND the dataset brief. MUTATION: drop either
    factory keyword -> red."""
    from looplab.adapters.tasks import load_task, make_roles
    import looplab.agents.factory as factory
    monkeypatch.setattr(factory, "make_llm_client", lambda *a, **k: object())
    settings = Settings(backend="llm", unified_agent=False, researcher_tools=False,
                        prompt_truths_developer=flag, memora=False, web_search=False)
    repo = load_task(ROOT / "examples" / "repo_task.json")
    _, developer = make_roles(repo, settings)
    assert getattr(developer, "_prompt_truths", None) is flag
    monkeypatch.chdir(ROOT)
    import looplab.core.hardware as hardware
    monkeypatch.setattr(hardware, "detect_gpu", lambda: None)   # the caps sentence, not the box
    data = load_task(ROOT / "examples" / "dataset_task.json")
    _, developer = make_roles(data, settings.model_copy(update={"auto_install_deps": True,
                                                                "trust_mode": "trusted_local"}))
    brief = developer.brief
    assert AUTO_INSTALL_PROMISE in brief                  # the run DID promise the install
    assert (_FALLBACK_SENTENCE in brief) is (not flag)


# ------------------------------------------------------------------ the loop half, driven

def _bounce(*, reject_prompt, err="`stages` must be a non-empty array."):
    """ONE refused emit through the REAL `drive_tool_loop`, then an accepted one; the tool messages
    the loop sent back."""
    from looplab.agents.tool_loop import drive_tool_loop

    class _Model:
        def __init__(self):
            self.turns = [[{"id": "a", "type": "function", "function": {
                              "name": "declare_stages", "arguments": "{}"}}],
                          [{"id": "b", "type": "function", "function": {
                              "name": "declare_stages", "arguments": "{\"ok\": 1}"}}]]
            self.tool_messages: list = []

        def chat(self, messages, tools=None, tool_choice="auto", **_kw):
            self.tool_messages = [m["content"] for m in messages if m.get("role") == "tool"]
            return {"content": "", "tool_calls": self.turns.pop(0)}

    model = _Model()
    spec = {"type": "function", "function": {"name": "declare_stages", "description": "d",
                                             "parameters": {"type": "object"}}}
    kwargs = {} if reject_prompt is None else {"reject_prompt": reject_prompt}
    drive_tool_loop(model, None, [{"role": "user", "content": "go"}], spec,
                    finalize=lambda a: a, fallback=lambda m: None,
                    validate=lambda a: None if a.get("ok") else err, **kwargs)
    return model.tool_messages
