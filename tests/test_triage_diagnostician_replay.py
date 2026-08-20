"""Guards for `tools/triage_diagnostician_replay.py` — the paid arm's harness.

WHAT A GUARD HERE IS FOR. The harness produces ONE number, and the two ways it can produce a wrong
one are silent: it can ask a model about a fact the engine already holds (which would flatter or
damn the diagnostician on rows production never puts to it), and it can let the corpus's own LABEL
reach the prompt (which would measure leakage, not diagnosis). Neither shows up as an error.

Every assertion below has an input that makes it fail, and every count distinguishes "all passed"
from "nothing was checked": the stub records the calls it received, and the tests assert on that
number rather than on the absence of a wrong answer.

Fully offline: `drive_tool_loop` is the documented monkeypatch seam, so no client is constructed
and no endpoint is reached.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS_PATH = REPO_ROOT / "tools" / "triage_diagnostician_replay.py"


def _harness():
    spec = importlib.util.spec_from_file_location("triage_diagnostician_replay", HARNESS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


H = _harness()


# --------------------------------------------------------------------------------------------
# A corpus row, hand-built so a test can decide what the engine's own answer will be.

def _row(*, case_id="run-x/n0/s10", stderr="Traceback\nValueError: boom", exit_code=1,
         status="fail", reads=None, log_tail=None, truth="crash", stages=None):
    return {
        "case_id": case_id,
        "evidence": {
            "at_classification": {
                "stderr_tail": stderr, "stderr_tail_chars": len(stderr), "exit_code": exit_code,
                "failed_stage": "train", "failed_stage_status": status,
                "stages": stages if stages is not None else [
                    {"name": "train", "status": status, "exit_code": exit_code}],
                "stages_recorded": True, "stages_passed": 0, "eval_seconds": 1.0,
            },
            "on_demand": {
                "triage_log_reads": list(reads or []),
                "stage_log": {"paired_to_this_attempt": bool(log_tail), "path": None,
                              "bytes": 10, "tail": log_tail, "nonfinite_loss_hits": 0,
                              "oom_markers": []},
            },
        },
        "label": {"reason": truth, "basis": "terminal_exception"},
        "recorded": {"reason": "no_metric", "reason_from": "event", "triage_action": "repair",
                     "rationale": "", "node_terminal_reason": None},
        "provenance": {"run": "run-x", "node_id": 0, "attempt": 1, "seq": 10, "ts": 0.0,
                       "event": "node_repaired", "terminal": False, "triage_span": None,
                       "run_best_metric": None},
        "cause_notes": {"params_rejected_by_stage": [], "declared_params": []},
    }


class _Recorder:
    """The stub provider. It COUNTS, so a test can tell "the model agreed" from "nothing ran"."""

    def __init__(self, emit: dict):
        self.emit = emit
        self.prompts: list[list[dict]] = []
        self.emit_specs: list[dict] = []

    def loop(self, client, tools, messages, emit_spec, *, finalize, fallback, **kwargs):
        self.prompts.append(messages)
        self.emit_specs.append(emit_spec)
        return finalize(dict(self.emit))

    @property
    def calls(self) -> int:
        return len(self.prompts)


class _StubContext:
    """`RunContext`'s surface, with no run directory and no network.

    Deliberately NOT a subclass: a subclass that forgot to override one method would silently reach
    the real filesystem, which is the thing this stub exists to keep out of a unit test."""

    def __init__(self, recorder: _Recorder, monkeypatch, *, inline_repair_attempts=4):
        from looplab.agents.unified_agent import UnifiedAgent
        import looplab.agents.agent as agent_module
        monkeypatch.setattr(agent_module, "drive_tool_loop", recorder.loop, raising=True)
        self.settings = SimpleNamespace(inline_repair_attempts=inline_repair_attempts,
                                        agent_max_turns=0, agent_time_budget_s=0.0)
        # The SHIPPED agent, with a truthy pilot client so `triage_crash` does not take its
        # "no pilot model" early return. Nothing is ever sent through it: the loop is stubbed.
        self._agent = UnifiedAgent(researcher=None, developer=None,
                                   pilot_client=object(), pilot_tools=None, prompts=None,
                                   agent_max_turns=0, agent_time_budget_s=0.0, loop_opts={})

    def state_at(self, _seq):
        return SimpleNamespace(nodes={}, pending_hints=[])

    def events_before(self, _seq):
        return []

    def new_agent(self):
        return self._agent, SimpleNamespace(accountant=SimpleNamespace(
            calls=0, prompt_tokens=0, completion_tokens=0, spent=0.0, priced_calls=0,
            peak_prompt=0))


def _emit(kind="oom", source="log", locator="train.log", quote="CUDA out of memory"):
    return {"action": "repair", "failure_kind": kind, "evidence_source": source,
            "evidence_locator": locator, "evidence_quote": quote, "rationale": "stub"}


# --------------------------------------------------------------------------------------------

def test_a_diagnosable_row_reaches_the_model_and_the_models_kind_is_the_answer(monkeypatch,
                                                                              tmp_path):
    rec = _Recorder(_emit(kind="oom"))
    ctx = _StubContext(rec, monkeypatch)
    out = H.diagnose_row(_row(), ctx, "durable", tmp_path)
    # THE COUNT FIRST: without it, an `oom` answer could equally mean the harness never ran.
    assert rec.calls == 1
    assert out["asked"] is True
    assert out["engine_reason"] == "crash"          # what the ENGINE said, kept beside the answer
    assert out["reason"] == "oom"                   # what the DIAGNOSTICIAN said, and it won
    assert out["reason_source"] == "triage"


def test_an_engine_final_row_is_never_put_to_the_model(monkeypatch, tmp_path):
    """The ownership rule, driven rather than restated.

    The stub is armed to answer `oom`. If the harness asked, `oom` is what the row would carry —
    so this test fails loudly on a harness that asks about a fact the engine holds, instead of
    passing because nothing happened."""
    rec = _Recorder(_emit(kind="oom"))
    ctx = _StubContext(rec, monkeypatch)
    # `expect_failed`: a stage contract the engine decided by stat'ing the filesystem.
    row = _row(exit_code=0, status="expect_failed", truth="expect_failed")
    out = H.diagnose_row(row, ctx, "durable", tmp_path)
    assert rec.calls == 0                           # nothing was asked
    assert out["asked"] is False
    assert out["reason"] == "expect_failed"
    assert out["reason_source"] == "engine"


def test_a_kind_outside_the_vocabulary_is_recorded_as_unclassified(monkeypatch, tmp_path):
    rec = _Recorder(_emit(kind="gpu_exploded"))
    ctx = _StubContext(rec, monkeypatch)
    out = H.diagnose_row(_row(), ctx, "durable", tmp_path)
    assert rec.calls == 1                           # it WAS asked — this is a non-answer, not a skip
    assert out["reason"] == "unclassified"
    assert out["reason_source"] == "undiagnosed"


def test_the_harness_drives_the_shipped_emit_schema_and_not_one_of_its_own(monkeypatch, tmp_path):
    """A number produced by a lookalike measures the lookalike. The two things that make this the
    production diagnostician are the system prompt and the answer vocabulary; assert both are the
    objects `agents/unified_agent.py` and `engine/failure_diagnosis.py` own."""
    from looplab.agents.unified_agent import UnifiedAgent
    from looplab.engine.failure_diagnosis import DIAGNOSED_FAILURE_REASONS, EVIDENCE_SOURCES

    rec = _Recorder(_emit())
    ctx = _StubContext(rec, monkeypatch)
    H.diagnose_row(_row(), ctx, "durable", tmp_path)
    assert rec.calls == 1
    system = rec.prompts[0][0]["content"]
    assert system == UnifiedAgent._TRIAGE_SYSTEM
    props = rec.emit_specs[0]["function"]["parameters"]["properties"]
    assert props["failure_kind"]["enum"] == list(DIAGNOSED_FAILURE_REASONS)
    assert props["evidence_source"]["enum"] == list(EVIDENCE_SOURCES)


def test_the_widened_arm_adds_the_on_demand_evidence_and_the_durable_one_does_not(monkeypatch,
                                                                                 tmp_path):
    marker = "torch.OutOfMemoryError: Tried to allocate 8.79 GiB"
    row = _row(reads=[marker], log_tail="step 3 loss=0.4")
    for arm, expected in (("durable", False), ("widened", True)):
        rec = _Recorder(_emit())
        ctx = _StubContext(rec, monkeypatch)
        H.diagnose_row(row, ctx, arm, tmp_path)
        assert rec.calls == 1, arm
        user = rec.prompts[0][1]["content"]
        assert ("Traceback" in user), arm          # the durable tail is in BOTH arms
        assert (marker in user) is expected, arm   # the on-demand read is in exactly one


def test_neither_the_label_nor_the_recorded_reason_can_reach_the_prompt(monkeypatch, tmp_path):
    """A bench that leaks its answer key measures the leak. Corrupting both columns must not move
    one byte of what the model is shown."""
    rec_a = _Recorder(_emit())
    H.diagnose_row(_row(truth="crash"), _StubContext(rec_a, monkeypatch), "durable", tmp_path)
    leaked = _row(truth="oom")
    leaked["label"]["basis"] = "oom_marker_in_evidence"
    leaked["recorded"]["reason"] = "oom"
    leaked["recorded"]["rationale"] = "Genuine CUDA OOM at step 0"
    rec_b = _Recorder(_emit())
    H.diagnose_row(leaked, _StubContext(rec_b, monkeypatch), "durable", tmp_path)
    assert rec_a.calls == 1 and rec_b.calls == 1
    assert rec_a.prompts[0] == rec_b.prompts[0]


def test_a_row_with_no_replayable_result_is_unanswered_and_not_a_wrong_answer(monkeypatch,
                                                                             tmp_path):
    rec = _Recorder(_emit())
    ctx = _StubContext(rec, monkeypatch)
    row = _row(exit_code=None, status="fail", stderr="a progress bar with no traceback")
    row["label"]["terminal_exception"] = None
    out = H.diagnose_row(row, ctx, "durable", tmp_path)
    assert rec.calls == 0
    assert out["reason"] is None
    assert out["skipped"] == "no_replayable_result"


def test_the_citation_is_re_resolved_against_the_node_workdir(monkeypatch, tmp_path):
    """The engine's own `evidence_citation_resolves`, wired to the workdir this harness computes.

    Three answers, and the test drives all three: a file that is there, one that is not, and a
    citation into the error text (which is not filesystem-shaped and must stay `None` rather than
    being counted as a failure to resolve)."""
    workdir = tmp_path / "run-x" / "nodes" / "node_0"
    workdir.mkdir(parents=True)
    (workdir / "train.log").write_text("x", encoding="utf-8")
    cases = {"train.log:12": True, "nope.log": False}
    for locator, expected in cases.items():
        rec = _Recorder(_emit(source="log", locator=locator))
        out = H.diagnose_row(_row(), _StubContext(rec, monkeypatch), "durable", tmp_path)
        assert rec.calls == 1, locator
        assert out["evidence_resolved"] is expected, locator
        assert out["workdir_exists"] is True
    rec = _Recorder(_emit(source="error", locator="", quote="ValueError: boom"))
    out = H.diagnose_row(_row(), _StubContext(rec, monkeypatch), "durable", tmp_path)
    assert out["evidence_resolved"] is None


def test_a_transport_failure_is_not_scored_as_a_diagnosis(monkeypatch, tmp_path):
    """`resilient` turns a raised loop into the `unanswerable` verdict, which is not an AGENT
    action — so the row must record `unclassified`, never the engine's residual dressed up as
    agreement."""
    import looplab.agents.agent as agent_module

    calls = {"n": 0}

    def _boom(*_a, **_kw):
        calls["n"] += 1
        raise RuntimeError("endpoint gone")

    rec = _Recorder(_emit())
    ctx = _StubContext(rec, monkeypatch)
    monkeypatch.setattr(agent_module, "drive_tool_loop", _boom, raising=True)
    out = H.diagnose_row(_row(), ctx, "durable", tmp_path)
    assert calls["n"] >= 1                       # it really tried
    assert out["reason"] == "unclassified"
    assert out["reason_source"] == "undiagnosed"


def test_the_credential_tier_is_selected_whole(monkeypatch, tmp_path):
    """Half a pair from the environment and half from a file is the one thing
    `serve/settings_store.py` refuses; the harness's own selector must refuse it too."""
    env_file = tmp_path / ".env"
    env_file.write_text("LOOPLAB_LLM_API_KEY=from-file\n"
                        "LOOPLAB_LLM_API_KEY_BASE_URL=https://file.example/v1\n",
                        encoding="utf-8")
    monkeypatch.delenv("LOOPLAB_LLM_API_KEY", raising=False)
    monkeypatch.delenv("LOOPLAB_LLM_API_KEY_BASE_URL", raising=False)
    values, source = H.credential_pair(env_file, tmp_path)
    assert source.startswith("dotenv:")
    assert values == {"llm_api_key": "from-file",
                      "llm_api_key_base_url": "https://file.example/v1"}
    # A key in the environment selects the WHOLE environment tier — the file's binding must not be
    # merged into it, even though that would produce a usable pair.
    monkeypatch.setenv("LOOPLAB_LLM_API_KEY", "from-env")
    values, source = H.credential_pair(env_file, tmp_path)
    assert source == "environment"
    assert values == {"llm_api_key": "from-env", "llm_api_key_base_url": ""}


def test_the_workdir_is_the_nodes_own_and_not_the_run_root(tmp_path):
    row = _row()
    assert H.workdir_for(row, tmp_path) == tmp_path / "run-x" / "nodes" / "node_0"


@pytest.mark.parametrize("arm", H.ARMS)
def test_both_arms_use_the_benchs_own_evidence_window(arm):
    """The arms are `triage_score.replay_result`'s two windows and not a second definition written
    here, so a score from this harness is comparable to `--arm frozen` / `--arm frozen-widened`."""
    from looplab.judgebench import triage_score

    marker = "OutOfMemoryError"
    row = _row(reads=[marker])
    res = triage_score.replay_result(row, widened=(arm == "widened"))
    assert (marker in res.stderr) is (arm == "widened")


def test_the_corpus_handoff_count_is_what_the_harness_will_ask_about():
    """The bench's own `--arm live` handoff count and the harness's ask set must be the same rows.

    If they drift, the harness is either paying for rows production decides structurally or
    skipping rows production puts to a model — and the score would be about a different program."""
    from looplab.engine.failure_diagnosis import DIAGNOSABLE_ENGINE_REASONS
    from looplab.engine.triage import _failure_reason
    from looplab.judgebench import triage_corpus, triage_score

    dataset = triage_corpus.DEFAULT_DATASET
    if not Path(dataset).exists():
        pytest.skip("committed corpus absent")
    rows = triage_corpus.read_dataset(dataset)["rows"]
    report = triage_score.score_dataset(rows, triage_score.live_engine_candidate(),
                                        name="live", live=True)
    asked = 0
    for row in rows:
        res = triage_score.replay_result(row)
        if res is not None and _failure_reason(res) in DIAGNOSABLE_ENGINE_REASONS:
            asked += int((row.get("label") or {}).get("reason")
                         not in (None, triage_corpus.LABEL_UNKNOWN))
    assert asked > 0                                     # not "nothing was checked"
    assert asked == report.diagnosable_handoff[1]
