"""One line a candidate prints — or one file it writes — must not take its node's terminal, or the
whole run, down with it.

Found on 2026-09-26 by the critic passes over doc 68's protocol facet and then over the first two
fixes: candidate output is JSON the engine parses and partly carries onto the node's payloads (the
settle row, then `node_evaluated`), and JSON parses values the rest of the pipeline refuses. Each
vector below was DRIVEN to `node_failed engine_error` with the run PAUSED before this change:

  * a `{"trials": [...]}` entry nested ~254 deep, an integer past 64 bits in one, a lone surrogate
    in its error, its param names or its extras' names — all legal JSON, all refused by the event
    store's orjson;
  * a line nested past ~1,000 levels (`RecursionError` out of `json.loads`) or an integer literal
    past 4,300 digits (a plain `ValueError`) — out of the scan every stdout reader shares, and the
    first out of `command_eval.declared_failure_reason`, which runs on every command-eval result;
  * a 401-digit integer as the metric (`OverflowError` out of `core/parse.py::to_float`), and a lone
    surrogate in an auto-captured extra metric's NAME;
  * a 2**64 `train_batch_size` or `global_step` in `trainer_state.json`, which rode onto the settle
    row and failed its append BEFORE the terminal;
  * the same three JSON shapes in a FILE the candidate writes: a `file_json` metric, `host_score`
    predictions on the repo tier, the generic host grade on the solution tier;
  * and a directory NAME that is not UTF-8 — a `trainer_state.json` under one, a `subject_glob` match
    under one — which comes back from `Path.glob` holding lone surrogates.

Driven here through the real engine, the real event store and the real fold, on both the solution
tier and the repo command tier.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from pydantic import BaseModel

from looplab.adapters.repo_task import EvalSpec, NoOpRepoDeveloper, RepoParamResearcher, RepoTask
from looplab.adapters.toytask import ToyTask
from looplab.core.models import Idea, NodeStatus
from looplab.engine.eval_dispatch import EvalDispatchMixin
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.runtime.sandbox import (TRIAL_NOT_A_NUMBER, SubprocessSandbox, _last_json_dict,
                                     json_line_trials, trial_record)
from looplab.search.policy import GreedyTree
from tests._posix_gates import BYTES_FILENAMES, POSIX_ONLY_OS_CALLS
from tests.factories import make_engine

ROOT = Path(__file__).resolve().parents[1]
TASK_FILE = ROOT / "examples" / "toy_task.json"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
_METRIC = {"kind": "stdout_json", "key": "metric"}


class _Researcher:
    def propose(self, state, parent):
        return Idea(operator="draft", params={}, space={"x": [1.0, 2.0]}, rationale="sweep")


class _PrintsDeveloper:
    """A solution that prints exactly `lines` (Python expressions evaluated in the sandbox)."""

    def __init__(self, *lines: str, prelude: str = ""):
        self.lines, self.prelude = lines, prelude

    def implement(self, idea: Idea) -> str:
        return "import json\n" + self.prelude + "".join(f"print({line})\n" for line in self.lines)


def _solution_run(tmp_path, *lines: str, task=None, prelude: str = ""):
    if task is None:
        task = ToyTask.load(TASK_FILE)
        task.direction = "min"
    engine = make_engine(tmp_path / "run", task=task, researcher=_Researcher(),
                         developer=_PrintsDeveloper(*lines, prelude=prelude),
                         sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                         holdout_fraction=0.0)
    anyio.run(engine.run)
    events = EventStore(tmp_path / "run" / "events.jsonl").read_all()
    return fold(events), events


def _repo_run(tmp_path, code: str, *, metric=None, **eval_extra):
    task = RepoTask(id="lines", goal="g", direction="max", editable_path=str(FIXTURE),
                    edit_surface=["*.json"], protect=["ttrain.py"],
                    eval=EvalSpec(command=[sys.executable, "-c", code], metric=metric or _METRIC,
                                  timeout=60, **eval_extra))
    engine = make_engine(tmp_path / "run", task=task, researcher=RepoParamResearcher({}),
                         developer=NoOpRepoDeveloper(), n_seeds=1, max_nodes=1)
    anyio.run(engine.run)
    events = EventStore(tmp_path / "run" / "events.jsonl").read_all()
    return fold(events), events


def _settled(state):
    """The node got an ordinary terminal and the run was not paused by it."""
    node = state.nodes[0]
    assert node.status is not NodeStatus.pending, "the node never got a terminal"
    assert "engine_error" not in (node.error_reason or ""), (node.error_reason, node.error)
    assert not state.paused, "one printed line paused the whole run"
    return node


# ------------------------------------------------------------------ stdout: trials, metric, extras
def test_a_deep_trial_field_is_dropped_and_its_real_score_kept(tmp_path):
    """Reduced to `Trial`'s own fields: the deep audit field goes, the trial's metric STAYS (it was
    measured) — replacing the whole entry, the first fix's shape, discarded a real score."""
    deep = "json.loads('[' * 300 + ']' * 300)"
    state, events = _solution_run(
        tmp_path,
        "json.dumps({'trials': [{'params': {'x': 1.0}, 'metric': 1.0},"
        f" {{'params': {{'x': 2.0}}, 'metric': 0.25, 'junk': {deep}}}]}})")
    node = _settled(state)
    assert node.status is NodeStatus.evaluated and node.metric == 0.25
    row = next(e.data for e in events if e.type == "node_evaluated")
    assert "junk" not in json.dumps(row["trials"])


def test_a_huge_integer_and_lone_surrogates_in_a_trial_land_as_values(tmp_path):
    """The surrogate in the error, in a PARAM NAME and in an EXTRA's name each failed the append;
    each now lands made surrogate-safe, and the huge int as the float both readers make of it."""
    state, _events = _solution_run(
        tmp_path,
        "'{\"trials\": [{\"metric\": 18446744073709551616, \"error\": \"\\\\ud800 bad\"},"
        " {\"params\": {\"\\\\ud800x\": 1}, \"metric\": 0.5,"
        " \"extra_metrics\": {\"\\\\ud801y\": 2.0}}]}'")
    node = _settled(state)
    assert node.status is NodeStatus.evaluated and node.metric == 0.5
    assert node.trials[0].error == "? bad" and node.trials[0].metric == float(2 ** 64)
    assert node.trials[1].params == {"?x": 1.0} and node.trials[1].extra_metrics == {"?y": 2.0}


def test_a_metric_too_large_for_a_float_is_no_metric_not_a_crash(tmp_path):
    state, _events = _solution_run(tmp_path, "'{\"metric\": ' + '1' * 401 + '}'")
    node = _settled(state)
    assert node.metric is None


def test_a_lone_surrogate_in_an_extra_metric_name_lands(tmp_path):
    state, _events = _solution_run(tmp_path, "'{\"metric\": 0.5, \"\\\\ud800x\": 2.0}'")
    node = _settled(state)
    assert node.metric == 0.5 and node.extra_metrics.get("?x") == 2.0


@pytest.mark.parametrize("line", [
    "'{\"a\": ' + '[' * 5000 + ']' * 5000 + '}'",      # RecursionError out of json.loads
    "'{\"n\": ' + '7' * 5000 + '}'",                   # ValueError: the 4,300-digit limit
])
def test_an_unparseable_last_line_on_the_repo_tier_is_skipped(tmp_path, line):
    """The repo COMMAND tier reads the same stdout through more readers — among them
    `declared_failure_reason`, on every result and outside any guard."""
    state, _events = _repo_run(tmp_path, "import json; print(json.dumps({'metric': 0.5}));"
                                         f" print({line})")
    node = _settled(state)
    assert node.status is NodeStatus.evaluated and node.metric == 0.5


# ------------------------------------------------------------------------- files the candidate writes
@pytest.mark.parametrize("state_json", [
    "{'train_batch_size': 2 ** 64, 'global_step': 3}",
    "{'train_batch_size': 8, 'global_step': 2 ** 64}",
])
def test_a_huge_count_in_trainer_state_is_refused_as_a_reading(tmp_path, state_json):
    """It rode onto the settle row and failed its append BEFORE the terminal (the first account of
    it, "after the terminal, the trust scan lost", named the wrong site)."""
    code = (f"import json; json.dump({state_json}, open('trainer_state.json', 'w'));"
            " print(json.dumps({'metric': 0.5}))")
    state, events = _repo_run(tmp_path, code)
    node = _settled(state)
    assert node.status is NodeStatus.evaluated and node.metric == 0.5
    assert "trust_scan" in [e.type for e in events]
    assert not any(2 ** 64 in (e.data.get("train_batch_size"), e.data.get("global_step"))
                   for e in events if e.type == "effective_train_batch")


@pytest.mark.parametrize("body", ["'[' * 5000 + '0.5' + ']' * 5000", "'9' * 5000"])
def test_a_metric_file_the_candidate_writes_cannot_pause_the_run(tmp_path, body):
    code = f"open('metrics.json', 'w').write({body}); print('done')"
    state, _events = _repo_run(tmp_path, code,
                               metric={"kind": "file_json", "path": "metrics.json", "key": "metric"})
    node = _settled(state)
    assert node.metric is None


# A 401-digit prediction is unscorable under an error metric (no metric) and simply matches no
# label under accuracy (0.0); a file too deep or with too long an integer to parse is no metric.
_HUGE = "'[' + ('1' * 401 + ',') * 2 + '1' * 401 + ']'"
_HOSTILE_PREDICTIONS = [(_HUGE, "rmse", None), (_HUGE, "accuracy", 0.0)] + [
    (preds, scorer, None) for preds in ("'[' * 5000 + ']' * 5000", "'[' + '7' * 5000 + ']'")
    for scorer in ("rmse", "accuracy")]


@pytest.mark.parametrize("preds,scorer,expected", _HOSTILE_PREDICTIONS)
def test_predictions_the_host_scores_cannot_pause_the_run(tmp_path, preds, scorer, expected):
    """`host_score` is the TRUST-tier reader — the one a candidate cannot self-report through — so a
    hostile candidate that could pause the run through it could do so whenever it liked."""
    labels = tmp_path / "labels.json"
    labels.write_text("[1, 2, 3]", encoding="utf-8")
    code = f"open('predictions.json', 'w').write({preds}); print('done')"
    state, _events = _repo_run(tmp_path, code, metric={
        "kind": "host_score", "predictions": "predictions.json", "labels": str(labels),
        "scorer": scorer})
    node = _settled(state)
    assert node.metric == expected


class _HostGraded(BaseModel):
    """The solution tier's generic host grade (`engine/holdout.py`), as `test_host_grading.py`."""
    kind: str = "predtest"
    id: str = "predtest"
    goal: str = "predict a held-out target"
    direction: str = "min"
    scorer: str = "rmse"

    def host_grader(self) -> dict:
        return {"predictions": "predictions.json", "scorer": self.scorer,
                "labels": [1.0, 2.0, 3.0]}


@pytest.mark.parametrize("preds,scorer,expected", _HOSTILE_PREDICTIONS[:4])
def test_predictions_the_solution_tier_grades_cannot_pause_the_run(tmp_path, preds, scorer,
                                                                   expected):
    state, _events = _solution_run(tmp_path, "'done'", task=_HostGraded(scorer=scorer),
                                   prelude=f"open('predictions.json', 'w').write({preds})\n")
    node = _settled(state)
    assert node.metric == expected


@BYTES_FILENAMES
def test_a_directory_name_that_is_not_utf8_is_refused_not_recorded(tmp_path):
    """`Path.rglob`/`glob` hand such a name back holding lone surrogates. A `trainer_state.json`
    under one, and a `subject_glob` match under one, each failed the node's settle append."""
    code = ("import json, os; os.makedirs(b'out/\\xff', exist_ok=True);"
            " open(b'out/\\xff/model.bin', 'wb').write(b'weights');"
            " os.makedirs(b'\\xfe', exist_ok=True);"
            " json.dump({'train_batch_size': 8, 'global_step': 3},"
            " open(b'\\xfe/trainer_state.json', 'w')); print(json.dumps({'metric': 0.5}))")
    state, _events = _repo_run(tmp_path, code, metric={**_METRIC,
                                                       "subject_glob": ["out/*/model.bin"]})
    node = _settled(state)
    assert node.status is NodeStatus.evaluated and node.metric == 0.5
    prov = node.metric_provenance or {}
    assert prov.get("subject_bound") is False and prov.get("unbound_reason") == "unreadable"
    assert prov["subjects"][0]["path"] == "out/?/model.bin", "refused, and named surrogate-safe"


def test_an_mtime_past_64_bits_is_stored_clamped():
    """tmpfs accepts `os.utime` to 2**40 s, whose `st_mtime_ns` overflowed the encoder (driven on
    /dev/shm; ext4 clamps it itself). Clamped on BOTH sides of every identity comparison."""
    from looplab.runtime.metric_subject import _storable_int

    assert _storable_int(2 ** 40 * 10 ** 9) == 2 ** 64 - 1
    assert _storable_int(-(2 ** 70)) == -(2 ** 63)
    assert _storable_int(1_700_000_000_000_000_000) == 1_700_000_000_000_000_000
    shm = Path("/dev/shm")
    if not (shm.is_dir() and shm.stat().st_mode & 0o002):
        return
    import os
    import tempfile

    from looplab.runtime.metric_subject import bind_one
    with tempfile.TemporaryDirectory(dir=shm) as d:
        (Path(d) / "m.bin").write_bytes(b"w")
        try:
            os.utime(Path(d) / "m.bin", ns=(2 ** 40 * 10 ** 9, 2 ** 40 * 10 ** 9))
        except (OSError, OverflowError):
            return
        row = bind_one(d, "m.bin", confine=lambda wd, rel: Path(wd) / rel)
        from looplab.core.jsonutil import canonical_json
        import orjson
        orjson.dumps(row)                       # what the event store does with it
        assert row["mtime_ns"] <= 2 ** 64 - 1 and canonical_json(row)


# ------------------------------------------------------------------ the reduction, as units
def test_the_shared_scan_skips_what_it_cannot_parse():
    abyss = '{"a": ' + "[" * 5000 + "]" * 5000 + "}"
    assert _last_json_dict('{"metric": 2.0}\n' + abyss, lambda o: "metric" in o) == {"metric": 2.0}
    assert _last_json_dict('{"metric": 2.0}\n{"metric": ' + "9" * 5000 + "}",
                           lambda o: "metric" in o) == {"metric": 2.0}


# A battery of entries the event store COULD encode before the change: the reduction must leave
# both readers' view of each exactly as it was.
_ENCODABLE = [
    {"params": {"x": 1.0}, "metric": 0.5, "seconds": 1.5, "extra_metrics": {"n": 3}, "error": ""},
    {"params": {"opt": "adam", "lr": 0.1}, "metric": 0.9},            # text param: fold refuses it
    {"params": {"lr": "0.1"}, "metric": "0.25"},                      # numeric strings read as numbers
    {"params": {"lr": True}, "metric": True},                         # bools read as 1.0
    {"metric": "inf"}, {"metric": "nan"}, {"metric": " 0.5 "}, {"metric": "1_000"},
    {"metric": [1]}, {"metric": {"a": 1}}, {"metric": "abc"}, {"seconds": [1]},
    {"params": {"a": None}}, {"params": {"a": []}}, {"params": [1]}, {"params": None},
    {"error": None}, {"error": 5}, {"error": "boom", "metric": None},
    {"extra_metrics": [1, 2]}, {"extra_metrics": {"k": "text", "j": 2}}, {"junk": {"deep": [[1]]}},
    {}, 1, None, "text", [1, 2], {"metric": 2 ** 63 - 1}, {"metric": -(2 ** 63)},
]


def _folded_trials(tmp_path, name, trials):
    store = EventStore(tmp_path / name / "events.jsonl")
    store.append("run_started", {"run_id": name, "task_id": "t", "goal": "g", "direction": "max"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}, "rationale": "s"},
                                  "code": "pass\n"})
    store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 0.5,
                                    "violations": [], "trials": trials})
    return [t.model_dump() for t in fold(store.read_all()).nodes[0].trials]


def test_the_fold_sees_exactly_what_it_saw_before(tmp_path):
    """"Reduced" must not mean "shown differently": a trial with a text param stays REFUSED by the
    fold (the digest's "Tuning of" block and `read_experiment` render trials into prompts, and a
    prompt is a contract), and every accepted value folds to the same number."""
    raw = _folded_trials(tmp_path, "raw", _ENCODABLE)
    reduced = _folded_trials(tmp_path, "reduced", [trial_record(e) for e in _ENCODABLE])
    assert json.dumps(reduced, default=str) == json.dumps(raw, default=str)


@pytest.mark.parametrize("direction", ["min", "max"])
def test_the_engine_picks_the_same_metric_off_the_reduced_list(direction):
    def _pick(trials):
        res = SimpleNamespace(trials=trials, metric=None, extra_metrics=None,
                              extra_metrics_provenance=None)
        EvalDispatchMixin._apply_sweep_best(
            SimpleNamespace(task=SimpleNamespace(direction=direction)), res)
        return res.metric, res.extra_metrics, res.extra_metrics_provenance

    for trials in (_ENCODABLE, _ENCODABLE[::-1], [1], [{"metric": [1]}], [{}, None],
                   [{"metric": 2 ** 70, "extra_metrics": {"a": 1}}, {"metric": 10 ** 400}]):
        assert _pick([trial_record(e) for e in trials]) == _pick(list(trials))


def test_what_the_reduction_writes():
    printed = [1, [1, 2], {"params": {"x": 1.0, "bad": "text", "deep": [[1]]}, "metric": 0.5,
                           "extra": {"n": 3}, "extra_metrics": {"n": 3}}, {"metric": 10 ** 400}]
    assert json_line_trials(json.dumps({"trials": printed})) == [
        1, None, {"params": {"x": 1.0, "bad": "text", "deep": None}, "metric": 0.5,
                  "extra_metrics": {"n": 3.0}},
        {"metric": TRIAL_NOT_A_NUMBER}]
    # A line of empty objects stays a line of empty objects (a record grows by under 2x at worst:
    # a container read as `null`, `1` as `1.0`, a 64-bit-plus int as its float repr).
    assert json_line_trials('{"trials": [{}, {}]}') == [{}, {}]
    # Two param names that differ only in a lone surrogate collide once made safe; the merge must
    # not keep the number and drop the text param the fold refuses the trial for.
    assert trial_record({"params": {"\ud800opt": "adam", "?opt": 0.1}}) == {"params": None}


def test_the_other_readers_of_candidate_output_skip_what_they_cannot_parse():
    from looplab.engine.asha_monitor import _json_objects_newest_first
    from looplab.events.digest import _last_json_object
    from looplab.runtime.command_eval import declared_failure_reason, host_score
    from looplab.runtime.numeric_contract import last_readings

    abyss = '{"a": ' + "[" * 5000 + "]" * 5000 + "}"
    huge = '{"loss": ' + "9" * 5000 + "}"
    assert _last_json_object(abyss) is None
    assert declared_failure_reason(abyss + "}") is None
    assert declared_failure_reason('{"looplab_failure_reason": "rules_violation"}\n' + abyss) \
        == "rules_violation"
    assert list(_json_objects_newest_first('{"loss": 1}\n' + huge)) == [{"loss": 1}]
    # An unparseable line is not a JSON reading; `last_readings` then reads it as TEXT, as it reads
    # any non-JSON line, and a 5,000-digit loss is an infinite one (non-finite readings INCLUDED).
    assert last_readings('{"loss": 0.5}\n' + abyss, ["loss"]) == {"loss": 0.5}
    assert last_readings('{"loss": 0.5}\n' + huge, ["loss"]) == {"loss": float("inf")}
    assert last_readings('{"loss": ' + "9" * 400 + "}", ["loss"]) == {"loss": float("inf")}
    assert host_score("rmse", [10 ** 400, 1, 1], [1, 2, 3]) is None
    assert host_score("accuracy", [10 ** 400, 2, 3], [1, 2, 3]) == pytest.approx(2 / 3)


# ------------------------------------------------------------------ second critic pass (2026-09-26)
def _no_score_state(tmp_path):
    """A node whose final line carries a `no_score` account whose reason ESCAPES a lone surrogate —
    ASCII in the stored tail, a surrogate once parsed."""
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}, "rationale": "r"},
                                  "code": "pass\n"})
    store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 0.5, "violations": [],
                                    "stdout_tail": '{"metric": 0.5, "no_score": {"reason": '
                                                   '"\\ud800 why"}}'})
    return fold(store.read_all())


def test_a_surrogate_in_a_printed_account_never_reaches_a_prompt(tmp_path):
    """The digest renders `no_score.reason` into every later Researcher prompt; parsed, it held a
    lone surrogate the LLM client could not encode — raised out of `Engine.run()`, on every
    resume (driven by the critic against a real client). The digest now reads a surrogate-safe tree."""
    from looplab.agents.roles import _state_brief

    state = _no_score_state(tmp_path)
    brief = _state_brief(state, state.nodes[0])
    brief.encode("utf-8")                        # the request body an SDK builds from it
    assert "? why" in brief


def test_no_request_leaves_with_a_lone_surrogate_in_it():
    """The last line before bytes leave (`core/llm.py::_bounded_create`): whatever reaches a
    request — a tool result, a log line, a parsed candidate string — is sent surrogate-safe, where
    the SDK raised `UnicodeEncodeError` building it."""
    from looplab.core.llm import OpenAICompatibleClient

    sent = {}

    class _Completions:
        def create(self, **kwargs):
            sent.update(kwargs)
            json.dumps(kwargs, ensure_ascii=False).encode("utf-8")   # what the SDK does with it
            return {"ok": True}

    client = OpenAICompatibleClient(base_url="http://127.0.0.1:9/v1", api_key="k", model="m")
    client._sdk = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    out = client._bounded_create({"model": "m", "messages": [
        {"role": "user", "content": "log line: \ud800 end"},
        {"role": "tool", "content": [{"type": "text", "text": "x\udfff"}]}]}, 5.0)
    assert out == {"ok": True}
    assert sent["messages"][0]["content"] == "log line: ? end"


def test_an_activation_manifest_the_candidate_nests_deep_is_no_declaration(tmp_path):
    code = ("import json; open('looplab_activation.json', 'w').write("
            "'{\"markers\": ' + '[' * 5000 + ']' * 5000 + '}'); print(json.dumps({'metric': 0.5}))")
    state, _events = _repo_run(tmp_path, code)
    node = _settled(state)
    assert node.status is NodeStatus.evaluated and node.metric == 0.5


def _returns_promptly(fn, *args, seconds=10.0):
    import threading

    box = {}
    worker = threading.Thread(target=lambda: box.update(value=fn(*args)), daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), f"{fn.__name__} blocked on a FIFO"
    return box.get("value")


@POSIX_ONLY_OS_CALLS
def test_a_fifo_in_the_workdir_blocks_no_reader(tmp_path):
    """A FIFO under the activation manifest's name, or as `x.log`, blocked a plain `open` forever —
    on the event loop (critic 2026-09-26, driven: no terminal after 100 s against a 2 s baseline)."""
    import os

    from looplab.engine.activation import _fresh_logs, read_markers
    from looplab.engine.eval_log_plan import snapshot_training_logs
    from looplab.runtime.effective_batch import _read_state

    os.mkfifo(tmp_path / "looplab_activation.json")
    os.mkfifo(tmp_path / "evil.log")
    os.mkfifo(tmp_path / "trainer_state.json")
    (tmp_path / "train.log").write_text("loss 0.5\n", encoding="utf-8")
    assert _returns_promptly(read_markers, tmp_path) == []
    assert _returns_promptly(_fresh_logs, tmp_path, None) == ["loss 0.5\n"]
    snapshot = _returns_promptly(snapshot_training_logs, tmp_path)
    cursors = {Path(k).name if "/" in str(k) else str(k): v for k, v in snapshot.cursors.items()}
    assert any(c.offset is None for c in cursors.values()), "the FIFO is an unreadable cursor"
    assert _returns_promptly(_read_state, tmp_path / "trainer_state.json") is None


def test_the_other_candidate_file_parsers_skip_what_they_cannot_parse(tmp_path):
    from looplab.adapters.repo_write_tools import declared_output_paths
    from looplab.runtime.effective_batch import _read_state

    deep = "[" * 5000 + "]" * 5000
    assert declared_output_paths(deep) == []
    (tmp_path / "trainer_state.json").write_text('{"a": ' + deep + "}", encoding="utf-8")
    assert _read_state(tmp_path / "trainer_state.json") is None


def test_a_submission_field_past_the_csv_limit_is_no_score(tmp_path):
    """`csv.Error` is not a `ValueError`: one 200,000-character field in the candidate's submission
    raised it out of the MLE-bench search grade — the default protocol — pausing the run."""
    from looplab.adapters.mlebench_grade import grade_search_split_in_subprocess

    sub = tmp_path / "submission.csv"
    sub.write_text("id,label\n1," + "x" * 200_000 + "\n", encoding="utf-8")
    assert grade_search_split_in_subprocess("comp", sub, "id,label\n1,a\n", ["1"]) is None


def test_a_directory_byte_total_past_64_bits_is_stored_clamped(tmp_path):
    """Five sparse files of 2**62 bytes sum past 64 bits (tmpfs, XFS, btrfs allow them) and the
    settle append refused the directory subject's row. Driven where the filesystem allows it."""
    import os
    import tempfile

    import orjson

    from looplab.runtime.metric_subject import bind_one

    shm = Path("/dev/shm")
    base = shm if shm.is_dir() and os.access(shm, os.W_OK) else tmp_path
    with tempfile.TemporaryDirectory(dir=base) as d:
        ckpt = Path(d) / "ckpt"
        ckpt.mkdir()
        try:
            for i in range(5):
                with open(ckpt / f"shard{i}.bin", "wb") as fh:
                    fh.truncate(2 ** 62)
        except (OSError, OverflowError):
            pytest.skip("this filesystem refuses a 2**62-byte sparse file")
        row = bind_one(d, "ckpt", confine=lambda wd, rel: Path(wd) / rel)
        orjson.dumps(row)                       # what the event store does with it
        assert row["bytes"] == 2 ** 64 - 1


@BYTES_FILENAMES
def test_an_ambiguous_glob_over_names_that_are_not_utf8_is_named_safely(tmp_path):
    code = ("import json, os\n"
            "for d in (b'out/\\xff', b'out/\\xfe'):\n"
            "    os.makedirs(d, exist_ok=True); open(d + b'/model.bin', 'wb').write(b'w')\n"
            "print(json.dumps({'metric': 0.5}))\n")
    state, _events = _repo_run(tmp_path, code, metric={**_METRIC,
                                                       "subject_glob": ["out/*/model.bin"]})
    node = _settled(state)
    prov = node.metric_provenance or {}
    assert prov.get("unbound_reason") == "ambiguous"
    assert sorted(prov["subjects"][0]["matched"]) == ["out/?/model.bin", "out/?/model.bin"]


def test_a_deep_prediction_file_at_the_holdout_grade_is_no_holdout_metric(tmp_path, monkeypatch):
    """The FINISH-phase holdout grade reads each top node's predictions again: a file too deep to
    parse there raised out of the finish (the search-phase grade had its guard; this one had not)."""
    from looplab.runtime import command_eval
    from tests.test_holdout import _HostGradedTask, _PredsDeveloper, _PredsResearcher

    real, calls = command_eval.read_candidate_file, []

    def _deep_after_the_search(path, *a, **k):
        calls.append(path)
        return real(path, *a, **k) if len(calls) == 1 else "[" * 5000 + "]" * 5000

    monkeypatch.setattr(command_eval, "read_candidate_file", _deep_after_the_search)
    engine = make_engine(tmp_path / "run", task=_HostGradedTask(), researcher=_PredsResearcher(),
                         developer=_PredsDeveloper(), sandbox=SubprocessSandbox(),
                         policy=GreedyTree(n_seeds=1, max_nodes=1), holdout_fraction=0.25,
                         holdout_select=True, holdout_top_k=1)
    final = anyio.run(engine.run)
    assert final.finished and not final.paused, "the finish-phase grade raised"
    rows = [e.data for e in engine.store.read_all() if e.type == "holdout_evaluated"]
    assert rows and all(row["metric"] is None for row in rows) and len(calls) >= 2
