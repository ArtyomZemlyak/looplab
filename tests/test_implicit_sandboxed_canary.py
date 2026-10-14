"""The IMPLICIT sandboxed smoke canary for a task that declares no `eval.canary` (doc 78 leftover,
2026-10-14): operator opt-in per run, writes confined to the scratch tree by Landlock, a refused
write is inconclusive (never a crash on the sandbox's wall), and a clear contract warning when no
preflight runs."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from looplab.engine import eval_canary
from looplab.engine.evaluate import EvaluateMixin, effective_canary_spec
from looplab.harness.progress import canary_preflight
from looplab.runtime import landlock
from looplab.runtime.command_eval import run_command_eval

_NO_LANDLOCK = landlock.unavailable_reason()


def _host(tmp_path, *, external=True, declared=False, trust="trusted_local"):
    spec = {"command": ["python", "x.py"], **({"canary": {"timeout": 60}} if declared else {})}
    return SimpleNamespace(_eval_canary=False, external_harness=external, _eval_spec=spec,
                           run_dir=tmp_path, trust_mode=trust,
                           store=SimpleNamespace(read_all=lambda: []))


def _policy(tmp_path, **body):
    (tmp_path / "harness_policy.json").write_text(json.dumps(body))


@pytest.mark.skipif(_NO_LANDLOCK is not None, reason=str(_NO_LANDLOCK))
def test_the_implicit_smoke_is_operator_opt_in_and_never_replaces_a_declaration(tmp_path):
    host = _host(tmp_path)
    assert effective_canary_spec(host) is None                         # no policy: nothing
    _policy(tmp_path, implicit_canary="sandboxed", implicit_canary_timeout=45)
    spec = effective_canary_spec(host)
    assert spec["sandbox"] is True and spec["mode"] == "smoke" and spec["timeout"] == 45
    a = SimpleNamespace(node_id=0, generation=0,
                        node=SimpleNamespace(files={}, code="", deleted=[], attempt=0))
    assert EvaluateMixin._eval_canary_due(host, a) is True
    assert "sandbox" not in effective_canary_spec(_host(tmp_path, declared=True))
    assert effective_canary_spec(_host(tmp_path, external=False)) is None
    assert effective_canary_spec(_host(tmp_path, trust="untrusted")) is None
    _policy(tmp_path, implicit_canary="yes-please")
    assert effective_canary_spec(host) is None                         # malformed = off


def test_no_sandbox_on_a_kernel_without_landlock_means_no_run(tmp_path, monkeypatch):
    _policy(tmp_path, implicit_canary="sandboxed")
    monkeypatch.setattr(eval_canary, "implicit_canary_unavailable", lambda: "no landlock")
    import looplab.engine.evaluate as ev
    monkeypatch.setattr(ev, "implicit_canary_unavailable", lambda: "no landlock")
    assert effective_canary_spec(_host(tmp_path)) is None
    pre = canary_preflight(tmp_path)
    assert pre["effective"] == "none" and "cannot be built here" in pre["warning"]


def test_the_contract_warns_when_no_preflight_runs(tmp_path):
    (tmp_path / "task.snapshot.json").write_text(json.dumps({"eval": {"command": ["x"]}}))
    pre = canary_preflight(tmp_path)
    assert pre["effective"] == "none" and "eval.canary" in pre["warning"]
    (tmp_path / "task.snapshot.json").write_text(json.dumps(
        {"eval": {"command": ["x"], "canary": {"mode": "smoke", "timeout": 30}}}))
    assert canary_preflight(tmp_path) == {"effective": "declared", "mode": "smoke",
                                          "timeout": 30.0, "warning": None}


@pytest.mark.skipif(_NO_LANDLOCK is not None, reason=str(_NO_LANDLOCK))
def test_the_sandbox_lets_the_scratch_be_written_and_refuses_a_shared_absolute_path(tmp_path):
    scratch, shared = tmp_path / "scratch", tmp_path / "shared"
    scratch.mkdir()
    shared.mkdir()
    env = eval_canary.sandboxed_env({}, scratch)
    prog = ("import pathlib, sys, tempfile\n"
            "pathlib.Path('ok.txt').write_text('x')\n"
            "tempfile.mkstemp()\n"
            f"pathlib.Path({str(shared / 'leak.txt')!r}).write_text('leak')\n")
    res = run_command_eval([sys.executable, "-c", prog], str(scratch), 60,
                           {"kind": "stdout_json", "key": "m"}, env=env)
    assert (scratch / "ok.txt").exists()
    assert not (shared / "leak.txt").exists()
    assert res.exit_code != 0 and eval_canary.sandbox_denied(res)


def test_a_permission_denial_is_inconclusive_and_a_code_defect_is_not():
    denied = SimpleNamespace(stdout="", stderr="PermissionError: [Errno 13] Permission denied: '/data/x'")
    flag = SimpleNamespace(stdout="", stderr="error: unrecognized arguments: --max_steps 5")
    assert eval_canary.sandbox_denied(denied) and not eval_canary.sandbox_denied(flag)


def test_the_operator_policy_route_carries_the_switch(tmp_path):
    from looplab.harness.campaign import read_policy, write_policy
    out = write_policy(tmp_path, {"amendments": "all", "implicit_canary": "sandboxed",
                                  "implicit_canary_timeout": 90})
    assert out["implicit_canary"] == "sandboxed" and read_policy(tmp_path)["implicit_canary_timeout"] == 90
    assert eval_canary.implicit_canary_policy(tmp_path) == {"mode": "sandboxed", "timeout": 90.0}
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        write_policy(tmp_path, {"implicit_canary": "always"})


def _engine_run(tmp_path, code, **kw):
    from test_eval_canary import _Dev, _engine, _evaluate, _seed
    dev = _Dev(code)
    eng = _engine(tmp_path / "run", dev, canary=None, eval_canary=False, external_harness=True,
                  **kw)
    eng.run_dir.mkdir(parents=True, exist_ok=True)
    _policy(eng.run_dir, implicit_canary="sandboxed", implicit_canary_timeout=30)
    _seed(eng, code)
    return eng, _evaluate(eng)


@pytest.mark.skipif(_NO_LANDLOCK is not None, reason=str(_NO_LANDLOCK))
def test_a_refused_write_is_inconclusive_and_the_full_eval_decides(tmp_path):
    from test_eval_canary import _of, _terminals
    from looplab.events.types import EV_EVAL_CANARY_FINISHED
    shared = tmp_path / "shared-ledger.txt"           # an ABSOLUTE path outside the workspace
    code = ("import os\n"
            "c = os.environ.get('LOOPLAB_CANARY') == '1'\n"
            f"open({str(shared)!r}, 'a').write(('canary' if c else 'full') + '\\n')\n"
            "print('METRIC: ' + str(7 / 10))\n")
    eng, evs = _engine_run(tmp_path, code)
    assert shared.read_text().split() == ["full"], "the sandboxed smoke wrote a shared path"
    (finished,) = _of(evs, EV_EVAL_CANARY_FINISHED)
    assert finished.data["passed"] is True and finished.data["implicit"] == "sandboxed"
    assert "refused write" in finished.data["inconclusive"]
    (term,) = _terminals(evs)
    assert term.type == "node_evaluated" and term.data["metric"] == 0.7


@pytest.mark.skipif(_NO_LANDLOCK is not None, reason=str(_NO_LANDLOCK))
def test_a_broken_flag_fails_in_the_sandboxed_smoke_before_the_full_eval(tmp_path):
    from test_eval_canary import _of
    from looplab.events.types import EV_EVAL_CANARY_FINISHED
    marker = tmp_path / "full-ran.txt"
    code = ("import os, sys\n"
            "if os.environ.get('LOOPLAB_CANARY') == '1':\n"
            "    sys.exit('train.py: error: unrecognized arguments: --max_steps 5')\n"
            f"open({str(marker)!r}, 'w').write('x')\n"
            "print('METRIC: ' + str(1 / 10))\n")
    eng, evs = _engine_run(tmp_path, code)
    finished = _of(evs, EV_EVAL_CANARY_FINISHED)
    assert finished and finished[0].data["passed"] is False
    assert finished[0].data["implicit"] == "sandboxed" and "inconclusive" not in finished[0].data
    assert not marker.exists() or len(finished) > 1, "the full eval ran past a failed canary"
