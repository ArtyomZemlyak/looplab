"""A stage-manifest edit confined STRICTLY AFTER the reuse point must not forfeit the stages before
it — and every other shape of manifest edit still must.

TIER 1 THROUGHOUT: every assertion drives the real `Engine._safe_reuse_start` over a real workdir
holding the real entry scripts its reachability closure walks, and the manifests are the ones
`runs/e5small-dr-unified-v2` node 0 actually committed (`tests/fixtures/
stage_manifest_prefix_corpus.json`, copied out of that run's `node_created`/`node_repaired` rows —
this suite never reads `runs/`). Nothing here asserts against source text.

THE MEASUREMENT THE FIXTURE IS. Node 0 failed `train` with a CUDA OOM four times. Its `mine` stage
ran 61 minutes and its entry — argv, `expect.files`, `expect.assert`, `timeout`, `check` — is
byte-identical across all five manifests; the only entry any repair touched is `train`'s. Reuse
fired exactly once, on the one repair whose change set was EMPTY. The three re-mines were bought by
a file-level manifest test that could not tell those two facts apart.
"""
import json
from pathlib import Path

import pytest

from looplab.engine.eval_stages import EvalStagesMixin, manifest_prefix_unchanged

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "stage_manifest_prefix_corpus.json").read_text("utf-8"))
MANIFESTS = [m["looplab_stages.json"] for m in FIXTURE["manifests"]]
BUILD, R1, R2, R3, R4 = MANIFESTS          # node_created, then repair attempts 1-4


def _engine(tmp_path, manifest):
    """A bare mixin over a REAL workdir carrying node 0's two entry points.

    `_safe_reuse_start` reads nothing off the engine but the filesystem, so the truth table is
    reachable without driving an evaluation — the same shape `test_stage_contract.py` uses for the
    rollback rung. The scripts are stubs on purpose: what the closure resolves is proved by
    `test_inline_repair.py`, and what is under test here is the manifest compare.
    """
    class _Eng(EvalStagesMixin):
        pass

    for rel in ("vectorsearch/__init__.py", "vectorsearch/data/__init__.py"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
    (tmp_path / "vectorsearch" / "data" / "mine_negatives.py").write_text(
        "import vectorsearch.data.preprocess\n", encoding="utf-8")
    (tmp_path / "vectorsearch" / "data" / "preprocess.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "vectorsearch" / "train.py").write_text("import vectorsearch.loss\n",
                                                        encoding="utf-8")
    (tmp_path / "vectorsearch" / "loss.py").write_text("Y = 1\n", encoding="utf-8")
    (tmp_path / "looplab_stages.json").write_text(manifest, encoding="utf-8")
    return _Eng()


def _stages(manifest):
    """The resolved chain the engine runs, the way `_resolve_stages` builds it from a manifest."""
    from looplab.runtime.command_eval import materialized_stages
    return materialized_stages(json.loads(manifest))


def _mutate(manifest, fn):
    doc = json.loads(manifest)
    fn(doc["stages"])
    return json.dumps(doc, indent=1)


# --------------------------------------------------------------------------------------------
# The property, on the real rows.
# --------------------------------------------------------------------------------------------

def test_the_fixture_really_is_a_later_stage_only_edit(tmp_path):
    """NON-VACUITY OF THE FIXTURE ITSELF, asserted before anything is read off it: node 0's first
    repair changed the manifest, and it changed ONLY `train`'s entry. If a future re-extraction ever
    picks up rows where `mine` moved, every test below would be asserting about a different fact."""
    assert FIXTURE["manifests"][1]["changed"] == ["looplab_stages.json"]
    before, after = _stages(BUILD), _stages(R1)
    assert [s["name"] for s in before] == [s["name"] for s in after] == ["mine", "train"]
    assert before[0] == after[0]                       # `mine`: argv, expect, needs, timeout, check
    assert before[1] != after[1]                       # `train`: the argv the OOM repair retuned


def test_a_later_stage_only_manifest_edit_reuses_the_completed_earlier_stage(tmp_path):
    """THE ROW THIS EXISTS FOR. `train` failed, the repair rewrote `train`'s argv and touched
    nothing else — 61 minutes of `mine` were re-paid for a manifest edit that provably cannot reach
    it."""
    eng = _engine(tmp_path, R1)
    assert eng._safe_reuse_start(_stages(R1), "train", {"looplab_stages.json"}, tmp_path,
                                 prev_manifest=BUILD) == "train"


def test_without_the_previous_manifest_the_clause_forfeits_exactly_as_before(tmp_path):
    """The keyword's ABSENT value is the historical behaviour, byte for byte — a caller that has not
    been taught to pass the fold's copy can never accidentally gain a reuse."""
    eng = _engine(tmp_path, R1)
    assert eng._safe_reuse_start(_stages(R1), "train", {"looplab_stages.json"}, tmp_path) is None


def test_a_change_to_the_reused_stages_own_entry_still_forfeits(tmp_path):
    """The entry that produced the artifact moved, so the artifact is not what a re-run would make.
    Driven on node 0's own `mine` entry with its argv retuned the way the repairs retuned `train`'s."""
    edited = _mutate(R1, lambda st: st[0]["command"].append("--n-negatives=8"))
    eng = _engine(tmp_path, edited)
    assert eng._safe_reuse_start(_stages(edited), "train", {"looplab_stages.json"}, tmp_path,
                                 prev_manifest=R1) is None


def test_a_change_to_the_reused_stages_expect_still_forfeits(tmp_path):
    """`expect` is what the stage DECLARES IT PRODUCES — the artifact contract the reuse would be
    keeping. A repair that moves the declared path, or the `assert` sentence the checker holds the
    stage to, has changed what "that stage completed" means."""
    moved = _mutate(R1, lambda st: st[0]["expect"].__setitem__(
        "files", ["vectorsearch/experiments/other/negatives.parquet"]))
    reworded = _mutate(R1, lambda st: st[0]["expect"].__setitem__(
        "assert", "hard negatives mined for at least 50% of the training queries"))
    for edited in (moved, reworded):
        eng = _engine(tmp_path, edited)
        assert eng._safe_reuse_start(_stages(edited), "train", {"looplab_stages.json"}, tmp_path,
                                     prev_manifest=R1) is None


def test_a_reorder_still_forfeits(tmp_path):
    """Swapping the two stages leaves the manifest's BYTES a permutation of themselves and every
    entry individually equal — which is exactly why the compare is positional and the failed stage's
    INDEX must match on both sides."""
    edited = _mutate(R1, lambda st: st.reverse())
    eng = _engine(tmp_path, edited)
    assert eng._safe_reuse_start(_stages(edited), "train", {"looplab_stages.json"}, tmp_path,
                                 prev_manifest=R1) is None


def test_a_rename_of_the_reused_stage_still_forfeits(tmp_path):
    """A renamed stage is a stage whose `<name>.log`, trace label and reuse identity all moved, and
    `reused_stage_count` resolves `start_stage` BY NAME."""
    edited = _mutate(R1, lambda st: st[0].__setitem__("name", "mine_v2"))
    eng = _engine(tmp_path, edited)
    assert eng._safe_reuse_start(_stages(edited), "train", {"looplab_stages.json"}, tmp_path,
                                 prev_manifest=R1) is None


def test_a_stage_inserted_before_the_reuse_point_still_forfeits(tmp_path):
    """An INSERTION moves the reuse point: `train` is now at index 2 and a stage that never ran sits
    in front of it."""
    edited = _mutate(R1, lambda st: st.insert(1, {"name": "prep", "command": ["python", "prep.py"]}))
    eng = _engine(tmp_path, edited)
    assert eng._safe_reuse_start(_stages(edited), "train", {"looplab_stages.json"}, tmp_path,
                                 prev_manifest=R1) is None


def test_a_stage_removed_from_before_the_reuse_point_still_forfeits(tmp_path):
    """THE DANGEROUS SHRINK, and the reason the index compare is not redundant with prefix equality.
    A manifest that DROPS a completed `prep` leaves `prep`'s outputs on disk; reusing across it
    would feed `train` an artifact a full re-run provably would not have produced. The remaining
    prefix (`mine`) is untouched, so entry equality alone would have admitted it."""
    three = _mutate(R1, lambda st: st.insert(1, {"name": "prep", "command": ["python", "prep.py"]}))
    two = _mutate(three, lambda st: st.pop(1))
    eng = _engine(tmp_path, two)
    assert eng._safe_reuse_start(_stages(two), "train", {"looplab_stages.json"}, tmp_path,
                                 prev_manifest=three) is None


def test_a_failure_in_the_protected_score_stage_forfeits_the_whole_manifest(tmp_path):
    """`score` is appended by the engine from the operator's EvalSpec and is in no manifest, so when
    it fails the ENTIRE manifest is the prefix and no manifest edit can be "after" it. That is the
    shape `rubertlite-dr-unified-v8` node 0 attempt 4 really had."""
    chain = _stages(R1) + [{"name": "score", "command": ["python", "-m", "vectorsearch.test"]}]
    eng = _engine(tmp_path, R1)
    assert eng._safe_reuse_start(chain, "score", {"looplab_stages.json"}, tmp_path,
                                 prev_manifest=BUILD) is None
    # AND STILL WHEN EVERY MANIFEST ENTRY IS UNCHANGED. This is the case the presence check alone
    # holds: the appended `score` entry is in NO manifest, so there is no previous copy of it to
    # compare, and a rule that let the failed stage's index be "one past the end of the previous
    # manifest" would reuse the whole pipeline on the strength of a compare it never made. Refusing
    # costs the corpus nothing — the one real score-failure row (v8 node 0 attempt 4) had a changed
    # `mine` and is refused either way — and it keeps the protected stage outside the compare.
    only_mine = json.dumps({"stages": [json.loads(R1)["stages"][0]]}, indent=1)
    reformatted = json.dumps({"note": "same stages, re-serialized",
                              "stages": [json.loads(R1)["stages"][0]]})
    short_chain = _stages(reformatted) + [{"name": "score", "command": ["python", "score.py"]}]
    assert _stages(only_mine) == _stages(reformatted)          # the STAGES really are identical
    assert eng._safe_reuse_start(short_chain, "score", {"looplab_stages.json"}, tmp_path,
                                 prev_manifest=only_mine) is None


# --------------------------------------------------------------------------------------------
# The manifest acquittal narrows ONE clause and hands the rest of the change set on untouched.
# --------------------------------------------------------------------------------------------

def test_an_acquitted_manifest_does_not_smuggle_a_config_change_past_the_non_py_clause(tmp_path):
    """THE ROW THE CORPUS IS MOSTLY MADE OF, and the one this change deliberately does NOT buy.
    Three of node 0's four repairs changed `config.yaml` beside the manifest; that clause is
    unwidened (`_safe_reuse_start`'s own `needs` refusal, and `engine/champion_caveats.py`), so
    acquitting the manifest must leave it firing."""
    eng = _engine(tmp_path, R3)
    changed = {"looplab_stages.json", "vectorsearch/configs/config.yaml"}
    assert eng._safe_reuse_start(_stages(R3), "train", changed, tmp_path, prev_manifest=R2) is None


def test_an_acquitted_manifest_does_not_smuggle_a_reachable_py_change_past_the_closure(tmp_path):
    """A `.py` inside the reused stage's own import closure still forfeits — the acquittal removes
    the manifest from the change set and changes nothing about what is left."""
    eng = _engine(tmp_path, R1)
    reached = {"looplab_stages.json", "vectorsearch/data/preprocess.py"}
    assert eng._safe_reuse_start(_stages(R1), "train", reached, tmp_path, prev_manifest=BUILD) is None
    unreached = {"looplab_stages.json", "vectorsearch/loss.py"}
    assert eng._safe_reuse_start(_stages(R1), "train", unreached, tmp_path,
                                 prev_manifest=BUILD) == "train"


def test_only_the_pipelines_own_manifest_is_ever_acquitted(tmp_path):
    """A `looplab_stages.json` somewhere ELSE in the tree is not the pipeline's manifest —
    `_resolve_stages` reads exactly `<workdir>/looplab_stages.json` — so the compare says nothing
    about it and it must keep the historical refusal. Acquitting on the BASENAME would clear a
    non-`.py` the predicate never examined out of the change set and walk it past that clause."""
    eng = _engine(tmp_path, R1)
    assert eng._safe_reuse_start(_stages(R1), "train", {"sub/looplab_stages.json"}, tmp_path,
                                 prev_manifest=BUILD) is None
    assert eng._safe_reuse_start(_stages(R1), "train",
                                 {"looplab_stages.json", "sub/looplab_stages.json"}, tmp_path,
                                 prev_manifest=BUILD) is None


def test_a_deletion_still_forfeits_even_with_a_confined_manifest_edit(tmp_path):
    """The closure cannot see a vanished module, so a deletion is un-boundable whatever the manifest
    says."""
    eng = _engine(tmp_path, R1)
    assert eng._safe_reuse_start(_stages(R1), "train", {"looplab_stages.json"}, tmp_path,
                                 deleted=["vectorsearch/data/preprocess.py"],
                                 prev_manifest=BUILD) is None


def test_a_non_default_cwd_still_forfeits_even_with_a_confined_manifest_edit(tmp_path):
    eng = _engine(tmp_path, R1)
    assert eng._safe_reuse_start(_stages(R1), "train", {"looplab_stages.json"}, tmp_path,
                                 cwd="sub", prev_manifest=BUILD) is None


# --------------------------------------------------------------------------------------------
# The rule's own truth table, where the caller cannot reach it.
# --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("prev", [None, "", "   ", "{not json", '{"stages": []}', "[]"])
def test_an_unresolvable_previous_manifest_is_never_confined(prev):
    """Fail closed on anything that does not resolve through the SAME validator the pipeline is
    built by — an unparseable, empty or absent previous manifest proves nothing about what ran."""
    assert manifest_prefix_unchanged(prev, _stages(R1), "train") is False


def test_a_params_token_in_the_prefix_compares_expanded_on_both_sides():
    """`%params%` is expanded by `_resolve_stages` before the chain runs, so the previous manifest is
    expanded through the same rule — otherwise a pipeline that uses the token would silently lose
    this narrowing by comparing a token against its expansion."""
    from looplab.runtime.command_eval import expand_params
    templated = _mutate(R1, lambda st: st[0]["command"].append("%params%"))
    params = {"train.training.batch_size": 8192.0}
    resolved = [dict(s, command=expand_params(list(s["command"]), params))
                for s in _stages(templated)]
    assert resolved[0]["command"] != _stages(templated)[0]["command"]      # the token really expanded
    assert manifest_prefix_unchanged(templated, resolved, "train", params) is True
    assert manifest_prefix_unchanged(templated, resolved, "train", {"other": 1.0}) is False


def test_the_whole_fixture_run_replays_to_one_new_reuse_and_no_others(tmp_path):
    """REPLAY of node 0's four repairs, in order, over its own manifests — the arithmetic quoted in
    `_safe_reuse_start`'s comment, driven rather than asserted. Attempt 1 (manifest only) is the ONE
    the narrowing buys. Attempt 2 already reused before this change — its change set is empty, which
    is the only way node 0 ever reused anything. Attempts 3 and 4 stay refused, because they also
    rewrote `config.yaml` and that clause is not touched."""
    rows = FIXTURE["manifests"]
    verdicts = []
    for i in range(1, len(rows)):
        prev, cur = rows[i - 1]["looplab_stages.json"], rows[i]["looplab_stages.json"]
        eng = _engine(tmp_path, cur)
        verdicts.append(eng._safe_reuse_start(_stages(cur), "train", set(rows[i]["changed"]),
                                              tmp_path, prev_manifest=prev))
    assert verdicts == ["train", "train", None, None]
    assert [r["changed"] for r in rows[1:]] == [
        ["looplab_stages.json"],
        [],
        ["looplab_stages.json", "vectorsearch/configs/config.yaml"],
        ["looplab_stages.json", "vectorsearch/configs/config.yaml"]]


# --------------------------------------------------------------------------------------------
# END TO END: the effect landing, not the predicate answering.
# --------------------------------------------------------------------------------------------

class _ManifestOnlyDev:
    """A Developer that stages the manifest at build and, on repair, rewrites ONLY the argv of the
    stage that FAILED — node 0's four repairs, in miniature.

    It must stage the manifest in `implement` too, and that is a property of the rule rather than a
    detail of the double: `prev_manifest` is read off `node.files`, so a node whose manifest was
    never committed (it came from the editable source and no `declare_stages` staged it) has no
    previous copy to compare and forfeits. The repo Developer's STAGES phase persists it before the
    write tools are composed, which is why all 85 corpus rows carry it.
    """

    def __init__(self, mine_cmd, train_cmd):
        self.mine_cmd, self.train_cmd = mine_cmd, train_cmd
        self.repair_calls = 0
        self.last_files: dict = {}
        self.last_deleted: list = []

    def _manifest(self, train_cmd):
        return json.dumps({"stages": [
            {"name": "mine", "command": self.mine_cmd, "timeout": 900.0},
            {"name": "train", "command": train_cmd, "timeout": 900.0}]}, indent=1)

    def implement(self, idea):
        self.last_files = {"looplab_stages.json": self._manifest(self.train_cmd)}
        self.last_deleted = []
        return ""

    def repair(self, idea, code, error):
        self.repair_calls += 1
        # ONLY the failed stage's argv moves — the exact shape of an OOM repair retuning a batch size.
        self.last_files = {"looplab_stages.json": self._manifest(
            self.train_cmd + [f"--batch-size={8192 >> self.repair_calls}"])}
        self.last_deleted = []
        return ""


def _drive_repair(tmp_path, monkeypatch, dev, results):
    """The REAL repair loop over a staged repo, with `run_command_eval` stubbed so the test observes
    the `start_stage` the engine CHOSE. Same shape as `test_first_stage_repair_cost.py::_drive`."""
    import sys

    import anyio
    from looplab.adapters.repo_task import EvalSpec, RepoTask
    from looplab.engine.orchestrator import Engine
    from looplab.events.eventstore import EventStore
    from looplab.runtime import command_eval
    from looplab.runtime.sandbox import RunResult, SubprocessSandbox
    from looplab.search.policy import GreedyTree

    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "mine.py").write_text("print('mine')\n", encoding="utf-8")
    (src / "train.py").write_text("print('train')\n", encoding="utf-8")
    (src / "looplab_eval.py").write_text("print('score')\n", encoding="utf-8")

    starts: list = []

    def fake(cmd, _cwd, timeout, metric, env=None, **kwargs):
        starts.append(kwargs.get("start_stage"))
        return results.pop(0) if results else RunResult(
            exit_code=0, stdout='{"metric": 1.0}', stderr="", metric=1.0, timed_out=False,
            stages=[{"name": "mine", "status": "reused", "exit_code": 0, "seconds": 0.0},
                    {"name": "train", "status": "ok", "exit_code": 0, "seconds": 1.0}])

    monkeypatch.setattr(command_eval, "run_command_eval", fake)
    task = RepoTask(id="r", direction="max", editable_path=str(src),
                    edit_surface=["*.py", "*.json"],
                    eval=EvalSpec(command=[sys.executable, "looplab_eval.py"], metric={
                        "kind": "stdout_json", "key": "metric"}, cwd="."))
    researcher, _ = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=researcher, developer=dev,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 auto_install_deps=False, inline_repair=True, inline_repair_attempts=1,
                 inline_repair_retrain_cap=2, repair_critic_after=99)
    anyio.run(eng.run)
    return starts, list(EventStore(tmp_path / "run" / "events.jsonl").read_all())


def _train_failed():
    from looplab.runtime.sandbox import RunResult
    return RunResult(exit_code=1, stdout="", stderr="torch.cuda.OutOfMemoryError\n", metric=None,
                     timed_out=False, failed_stage="train",
                     stages=[{"name": "mine", "status": "ok", "exit_code": 0, "seconds": 3593.0},
                             {"name": "train", "status": "fail", "exit_code": 1, "seconds": 6.6}])


def test_the_engine_really_restarts_at_train_after_a_later_stage_only_manifest_repair(
        tmp_path, monkeypatch):
    """THE EFFECT, not the answer: drive the real repair loop and read the `start_stage` the engine
    handed the runner. Before this change it was `None` on every attempt — a full pipeline re-run,
    the 61-minute `mine` paid again."""
    dev = _ManifestOnlyDev(["python", "mine.py"], ["python", "train.py"])
    starts, evs = _drive_repair(tmp_path, monkeypatch, dev, [_train_failed()])
    assert dev.repair_calls == 1
    assert starts == [None, "train"], starts
    repaired = [e for e in evs if e.type == "node_repaired"]
    assert [r.data.get("changed") for r in repaired] == [["looplab_stages.json"]]
    assert [e for e in evs if e.type == "full_retrain_charged"] == [], (
        "nothing was discarded, so the re-train cap is charged nothing")


def test_the_engine_still_re_runs_everything_when_the_repair_moves_the_earlier_stage(
        tmp_path, monkeypatch):
    """THE NEGATIVE CONTROL over the same loop: the same Developer pointed at `mine`'s own argv."""
    class _EarlierDev(_ManifestOnlyDev):
        def repair(self, idea, code, error):
            self.repair_calls += 1
            self.last_files = {"looplab_stages.json": json.dumps({"stages": [
                {"name": "mine", "command": self.mine_cmd + [f"--n={self.repair_calls}"],
                 "timeout": 900.0},
                {"name": "train", "command": self.train_cmd, "timeout": 900.0}]}, indent=1)}
            self.last_deleted = []
            return ""

    dev = _EarlierDev(["python", "mine.py"], ["python", "train.py"])
    starts, evs = _drive_repair(tmp_path, monkeypatch, dev, [_train_failed()])
    assert dev.repair_calls == 1
    assert starts == [None, None], starts
    assert len([e for e in evs if e.type == "full_retrain_charged"]) == 1, (
        "a discarded completed stage is what the re-train cap counts")
