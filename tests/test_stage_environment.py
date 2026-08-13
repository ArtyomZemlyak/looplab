"""The DECLARED ENVIRONMENT (backlog F1d), driven end to end rather than pinned.

Every test here observes an EFFECT — what a real child process saw in `os.environ`, what a real
`docker run` argv carries, what a real event log records and what a second Engine over that log then
holds — because the whole point of this field is that a value reaches a process, and "the call is in
the source" cannot tell that apart from "the value was dropped on one tier".

The four properties, in the order they can bite:
  1. an operator's declaration REACHES the child, at all three levels, with the merge order stated;
  2. the two sandbox tiers AGREE — the Docker tier forwards it as `-e` or refuses the stage;
  3. the Developer CANNOT declare it, on either of its two surfaces;
  4. it is DURABLE — recorded, folded, and restored on re-entry — and a secret is refused everywhere.
"""
from __future__ import annotations

import json
import sys

import pytest

from looplab.core.config import Settings
from looplab.core.envsafe import validate_env_map
from looplab.runtime.command_eval import (STAGE_ENV_UNSUPPORTED, make_docker_wrap, merge_env,
                                          run_command_eval, validate_stages)

_M = {"kind": "stdout_json", "key": "metric"}

# A stage command that PRINTS what it actually saw, as the metric line the eval reads. The value is
# the observation: a test that asserted on the manifest would pass while the variable never arrived.
_ECHO = ("import json,os;"
         "print(json.dumps({'metric': 1.0, 'seen': os.environ.get('VS_LOCAL_DATA_ROOT'),"
         " 'other': os.environ.get('OMP_NUM_THREADS')}))")


def _seen(res, key="seen"):
    """What the child reported for `key`, read out of its own stdout."""
    for line in (res.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line).get(key)
            except json.JSONDecodeError:
                continue
    return None


# ------------------------------------------------------------------ 1. it reaches the child

def test_a_stage_env_reaches_the_child_process(tmp_path):
    stages = [{"name": "train", "command": [sys.executable, "-c", _ECHO],
               "env": {"VS_LOCAL_DATA_ROOT": "/data/local"}}]
    clean, err = validate_stages(stages, allow_env=True)
    assert err is None
    res = run_command_eval([], str(tmp_path), 30, _M, stages=clean)
    assert res.exit_code == 0
    assert _seen(res) == "/data/local", "the declared variable never reached the process"


def test_the_run_level_env_reaches_every_stage_and_the_stage_layer_wins(tmp_path):
    # The engine composes run+task level into `env`; the stage layer is the overlay. Two stages, one
    # of which re-declares the variable, so the merge ORDER is observed and not merely asserted.
    stages = [{"name": "prep", "command": [sys.executable, "-c", _ECHO]},
              {"name": "train", "command": [sys.executable, "-c", _ECHO],
               "env": {"VS_LOCAL_DATA_ROOT": "/data/stage"}}]
    clean, err = validate_stages(stages, allow_env=True)
    assert err is None
    res = run_command_eval([], str(tmp_path), 30, _M, stages=clean,
                           env={"VS_LOCAL_DATA_ROOT": "/data/run", "OMP_NUM_THREADS": "8"})
    assert res.exit_code == 0
    # The LAST stage's stdout is what `run_command_eval` returns, so this is `train`'s view: the
    # stage layer overrode the run layer, and the run layer it did NOT re-declare still arrived.
    assert _seen(res) == "/data/stage"
    assert _seen(res, "other") == "8"
    # …and `prep`, which declared nothing, ran under the run layer unchanged.
    assert [s["status"] for s in res.stages] == ["ok", "ok"]


def test_a_run_that_declares_nothing_is_byte_identical(tmp_path):
    # The empty-declaration path must not materialize anything: `merge_env()` of nothing is `{}` and
    # `_declared_eval_env` hands back the SAME object it was given, `None` included — three call
    # sites and tests/test_gpu_resources.py read that None as "unpinned, whole box, legacy".
    assert merge_env(None, {}, None) == {}
    stages = [{"name": "train", "command": [sys.executable, "-c", _ECHO]}]
    clean, _ = validate_stages(stages, allow_env=True)
    res = run_command_eval([], str(tmp_path), 30, _M, stages=clean)
    assert res.exit_code == 0 and _seen(res) is None


def test_the_engine_composes_run_and_task_level_before_either_tier_sees_it():
    """`_declared_eval_env` is the ONE composition point, so the two tiers cannot be given different
    environments — and it returns its argument unchanged when nothing is declared."""
    from looplab.engine.eval_dispatch import EvalDispatchMixin

    class _E(EvalDispatchMixin):
        _eval_env = {"VS_LOCAL_DATA_ROOT": "/data/run", "A": "run"}

    e = _E()
    assert e._declared_eval_env({"CUDA_VISIBLE_DEVICES": "0"}, {}) == {
        "CUDA_VISIBLE_DEVICES": "0", "VS_LOCAL_DATA_ROOT": "/data/run", "A": "run"}
    # task level wins over run level; the engine's own env is preserved underneath both.
    assert e._declared_eval_env({"CUDA_VISIBLE_DEVICES": "0"}, {"env": {"A": "task"}}) == {
        "CUDA_VISIBLE_DEVICES": "0", "VS_LOCAL_DATA_ROOT": "/data/run", "A": "task"}
    # An engine with NO declaration is the byte-identical path: the SAME object back, None included.
    class _N(EvalDispatchMixin):
        _eval_env: dict = {}

    n, base = _N(), {"CUDA_VISIBLE_DEVICES": "0"}
    assert n._declared_eval_env(None, {}) is None
    assert n._declared_eval_env(base, {"env": {}}) is base
    # …and it tolerates the NON-repo eval spec, which is `None`: the composition sits ABOVE the
    # branch in `_run_eval` so `Settings.eval_env` reaches the solution.py sandbox path too, which
    # is what makes the field's promise ("every eval of every node") true for a non-repo task.
    assert e._declared_eval_env(None, None) == {"VS_LOCAL_DATA_ROOT": "/data/run", "A": "run"}


# ------------------------------------------------------------------ 2. the two tiers agree

def test_the_docker_tier_forwards_a_stage_env_as_an_e_pair(tmp_path, monkeypatch):
    # The Docker tier inherits NOTHING from the host client, so the ONLY way in is another `-e`.
    # Observed on the real argv the wrap builds, without running a container — and NOT skipped when
    # the docker CLI is absent, because this is the tier-agreement property and skipping it on the
    # box where the other tier's tests all pass is how the two would drift apart unnoticed. Only the
    # CLI PRESENCE check is stubbed; every line that assembles the argv is the shipped one.
    monkeypatch.setattr("looplab.runtime.command_eval.require_docker_cli", lambda what: None)
    wrap = make_docker_wrap(str(tmp_path), "python:3.12-slim", env={"LOOPLAB_EVAL_SEED": "7"})
    base = wrap(["python", "x.py"], str(tmp_path))
    assert "-e" in base and "LOOPLAB_EVAL_SEED=7" in base
    assert not any(a.startswith("VS_LOCAL_DATA_ROOT=") for a in base)
    rebound = wrap.rebind_env({"VS_LOCAL_DATA_ROOT": "/data/local"})(["python", "x.py"], str(tmp_path))
    assert "VS_LOCAL_DATA_ROOT=/data/local" in rebound
    assert "LOOPLAB_EVAL_SEED=7" in rebound, "rebinding dropped the engine's own env"
    # The rebound wrap is still a real container wrap (the in-container `timeout` prefix depends on it)
    assert getattr(rebound and wrap.rebind_env({}), "_docker", False) is True


def test_a_container_wrap_that_cannot_carry_the_env_refuses_the_stage_instead_of_dropping_it(tmp_path):
    # THE TIER-AGREEMENT RULE. A wrap that claims to be docker but predates `rebind_env` cannot get
    # the declaration into the container. Running the stage anyway is how the two tiers would stop
    # agreeing — and the resulting failure (a loader reaching S3 because the variable is unset) is
    # precisely what the declaration exists to prevent, now with a manifest saying it cannot happen.
    def old_wrap(argv, host_cwd):
        return list(argv)
    old_wrap._docker = True

    stages = [{"name": "train", "command": [sys.executable, "-c", _ECHO],
               "env": {"VS_LOCAL_DATA_ROOT": "/data/local"}}]
    clean, err = validate_stages(stages, allow_env=True)
    assert err is None
    res = run_command_eval([], str(tmp_path), 30, _M, stages=clean, wrap=old_wrap)
    assert res.exit_code != 0 and res.failed_stage == "train"
    assert res.stages[0]["status"] == "env_unsupported"
    assert STAGE_ENV_UNSUPPORTED.format(stage="train") in res.stderr
    # and it did NOT run: no metric, no stdout from the child.
    assert res.metric is None


def test_a_non_docker_passthrough_wrap_still_gets_the_env_through_the_subprocess_dict(tmp_path):
    # A passthrough wrap is not a container: the env travels as `run_argv`'s dict exactly as it does
    # with no wrap at all, so this must NOT take the refusal branch.
    stages = [{"name": "train", "command": [sys.executable, "-c", _ECHO],
               "env": {"VS_LOCAL_DATA_ROOT": "/data/local"}}]
    clean, _ = validate_stages(stages, allow_env=True)
    res = run_command_eval([], str(tmp_path), 30, _M, stages=clean,
                           wrap=lambda argv, hc: list(argv))
    assert res.exit_code == 0 and _seen(res) == "/data/local"


# ------------------------------------------------------------------ 3. the Developer cannot declare it

def test_the_developer_may_not_declare_a_stage_env_on_either_surface():
    stage = [{"name": "train", "command": ["python", "t.py"], "env": {"A": "1"}}]
    # (a) the fail-closed DEFAULT — a new call site that forgets gets the refusal, not the smuggle.
    clean, err = validate_stages(stage)
    assert clean is None and "may not declare `env`" in err
    assert "operator" in err.lower(), "the refusal must name who CAN set it"
    # (b) the authoring tool passes `reserved=("score",)` and nothing else, so it inherits the refusal.
    clean, err = validate_stages(stage, reserved=("score",))
    assert clean is None and "may not declare `env`" in err
    # (c) a hand-written looplab_stages.json goes through `materialized_stages`, which keeps the
    #     default — an invalid manifest drops to the single command rather than smuggling the key.
    from looplab.runtime.command_eval import materialized_stages
    assert materialized_stages({"stages": stage}) is None
    assert materialized_stages(stage) is None


def test_the_operator_surfaces_do_opt_in():
    """Every site that validates the OPERATOR's own stage list passes allow_env=True — otherwise the
    field would be declarable in the task file and silently dropped by the engine that consumes it."""
    import ast
    import inspect

    from looplab.adapters import repo_developer, repo_task
    from looplab.engine import eval_stages
    from looplab.serve.routers import runs

    def _allow_env_calls(mod):
        tree = ast.parse(inspect.getsource(mod))
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name != "validate_stages":
                continue
            out.append(any(k.arg == "allow_env" and getattr(k.value, "value", None) is True
                           for k in node.keywords))
        return out

    # These three modules validate ONLY operator stage lists, so every call must opt in.
    for mod in (repo_task, eval_stages, runs):
        calls = _allow_env_calls(mod)
        assert calls and all(calls), f"{mod.__name__} validates operator stages without allow_env"
    # `repo_developer` is the module that holds BOTH sides of the boundary: it reads the operator's
    # stages to describe them (opted in) and validates the agent's own `declare_stages` emit (must
    # NOT be). A module where every call agreed would mean one of the two had drifted.
    dev_calls = _allow_env_calls(repo_developer)
    assert dev_calls.count(True) == 1 and dev_calls.count(False) == 2, dev_calls


# ------------------------------------------------------------------ 4. durable, and secrets refused

@pytest.mark.parametrize("name,value", [
    ("HF_TOKEN", "hf_abc"),                       # NAME screen
    ("MY_AUTH", "x"),                             # …the looser half of it
    ("DATABASE_URL", "postgres://u:pw@h/db"),     # VALUE screen: inline credentials
])
def test_a_secret_is_refused_at_every_level_with_the_same_rule(name, value):
    where = f"stage 'train' `env`"
    clean, err = validate_env_map(where, {name: value})
    assert clean is None and "credential" in err
    assert "no secret store" in err, "the refusal must say what LoopLab is NOT doing"
    assert "LOOPLAB_LLM_API_KEY" in err, "…and where LoopLab's own credentials go instead"
    # The same rule, reached from all three declaring levels.
    _, stage_err = validate_stages(
        [{"name": "train", "command": ["x"], "env": {name: value}}], allow_env=True)
    assert stage_err is not None and "credential" in stage_err
    with pytest.raises(Exception):
        Settings(eval_env={name: value})
    from looplab.adapters.repo_task import EvalSpec
    with pytest.raises(Exception):
        EvalSpec(command=["x"], env={name: value})


def test_the_engine_owns_its_own_variable_names():
    from looplab.runtime.read_fence import FENCE_DIR_ENV

    for name in ("CUDA_VISIBLE_DEVICES", FENCE_DIR_ENV, "LOOPLAB_EVAL_SEED", "LOOPLAB_MEMORY_DIR"):
        clean, err = validate_env_map("eval_env", {name: "x"})
        assert clean is None, f"{name} is engine-owned and must not be declarable"
        assert "LoopLab computes it per eval" in err
    # The read-fence marker is covered by the LOOPLAB_ prefix rule rather than its own entry — the
    # module that owns the rule may not import `runtime` to assert that, so this is the joint.
    assert FENCE_DIR_ENV.startswith("LOOPLAB_")


def test_the_launch_line_an_operator_actually_types():
    # `--set` splits on the FIRST `=`, so what reaches the field is a bare NAME=VALUE string. This is
    # the exact spelling `looplab run task.yaml -s eval_env=VS_LOCAL_DATA_ROOT=/data/dr-local` yields.
    from looplab.core.appconfig import parse_sets

    sets = parse_sets(["eval_env=VS_LOCAL_DATA_ROOT=/home/jovyan/data/dr-local"])
    assert sets["eval_env"] == "VS_LOCAL_DATA_ROOT=/home/jovyan/data/dr-local"
    assert Settings(**sets).eval_env == {"VS_LOCAL_DATA_ROOT": "/home/jovyan/data/dr-local"}
    # …and the several-variable form, plus the mapping form a config file / LOOPLAB_EVAL_ENV carries.
    assert Settings(eval_env="A=1,B=2").eval_env == {"A": "1", "B": "2"}
    assert Settings(eval_env={"A": "1"}).eval_env == {"A": "1"}
    assert Settings().eval_env == {}


def test_a_declared_env_is_recorded_folded_and_restored_on_re_entry(tmp_path, caplog):
    """The durability chain, end to end over a REAL event log: run_started carries it, the fold pins
    it, and a SECOND Engine over the same run dir adopts the record over its own live config."""
    from looplab.events.replay import fold
    from tests.factories import make_engine

    declared = {"VS_LOCAL_DATA_ROOT": "/home/jovyan/data/dr-local"}
    eng = make_engine(tmp_path, eval_env=declared)
    assert eng._eval_env == declared
    eng._setup_phase(fold(eng.store.read_all()))
    started = [e for e in eng.store.read_all() if e.type == "run_started"]
    assert started and started[0].data["eval_env"] == declared, "the log does not record it"
    assert fold(eng.store.read_all()).eval_env == declared

    # A re-entry whose LIVE config disagrees adopts the record (engine invariant #6) — and SAYS
    # which variable disagreed and how. The ordinary case is one variable whose VALUE moved, so a
    # warning that printed the two key SETS would report a conflict while hiding it: both sides read
    # `['VS_LOCAL_DATA_ROOT']`. That is what this run's own warning did before it was measured.
    resumed = make_engine(tmp_path, eval_env={"VS_LOCAL_DATA_ROOT": "/somewhere/else"})
    with caplog.at_level("WARNING"):
        resumed._repin_declared_env(fold(resumed.store.read_all()))
    assert resumed._eval_env == declared
    warned = "\n".join(r.getMessage() for r in caplog.records)
    assert "/home/jovyan/data/dr-local" in warned and "/somewhere/else" in warned


def test_a_run_with_no_declaration_writes_a_byte_identical_run_started(tmp_path):
    """The key is ABSENT, not empty. `search/speculation_quality.py` compares the run_started payload's
    key SET for EQUALITY, so an unconditional key would revoke every issued calibration receipt."""
    from tests.factories import make_engine
    from looplab.events.replay import fold

    eng = make_engine(tmp_path)
    eng._setup_phase(fold(eng.store.read_all()))
    started = [e for e in eng.store.read_all() if e.type == "run_started"]
    assert started and "eval_env" not in started[0].data
    assert fold(eng.store.read_all()).eval_env == {}


def test_an_old_log_keeps_this_processs_own_declaration(tmp_path):
    """A run started before this field existed records nothing. Reading that as "the log said empty,
    so drop yours" would make the first resume silently lose an environment the operator has since
    declared — a regression, not a pin."""
    from looplab.core.models import RunState
    from tests.factories import make_engine

    eng = make_engine(tmp_path, eval_env={"A": "1"})
    eng._repin_declared_env(RunState())          # a fold with no recorded eval_env
    assert eng._eval_env == {"A": "1"}


def test_the_fold_degrades_a_hand_edited_row_instead_of_handing_it_to_subprocess():
    """`fold` has no per-event exception handler and its output reaches a child PROCESS, so a row
    carrying a list or a nested object must read as "nothing declared"."""
    from looplab.events.eventstore import Event
    from looplab.events.replay import fold

    def _folded(value):
        return fold([Event(seq=0, ts=0.0, type="run_started",
                           data={"run_id": "r", "eval_env": value})]).eval_env

    assert _folded({"A": "1", "B": 2, "C": ["x"], 3: "y", "D": True}) == {"A": "1", "B": "2"}
    assert _folded(["A=1"]) == {}
    assert _folded(None) == {}
