"""The graded activation check DRIVEN through the real `_evaluate` loop, sandbox and inline repair.

minionerec-lora-v1 node 2, 2026-10-01: the node changed ONLY `MiniOneRec/looplab/experiment.env`
(`SFT_EVAL_SAMPLE=-2`, `SFT_RESUME_EVERY_MIN=0`) and declared those two assignments as its activation
markers. No code prints an assignment, so attempt 3 -- 6.8 h of training, metric 0.1126388 -- was
withheld as `inert_path`, the repair directive had the Developer add `echo` lines for the check, and a
full 7 h re-run followed. Each test below runs a real (tiny) repo task end to end; the pure halves are
in `tests/test_activation_graded.py`.
"""
from __future__ import annotations

import json
import sys

import anyio

from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.core.models import Idea
from looplab.engine import activation as act
from looplab.engine.orchestrator import Engine
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree

PY = sys.executable
ENV = "MiniOneRec/looplab/experiment.env"
BASE_ENV = "SFT_EVAL_SAMPLE=0\nSFT_RESUME_EVERY_MIN=60\nSFT_LORA_R=16\n"
INCIDENT_ENV = ("# SFT_EVAL_SAMPLE=-2 streams the whole valid file\n"
                "SFT_EVAL_SAMPLE=-2\nSFT_RESUME_EVERY_MIN=0\nSFT_LORA_R=16\n")
INCIDENT_MARKERS = ["SFT_EVAL_SAMPLE=-2", "SFT_RESUME_EVERY_MIN=0"]
# The eval reads the env file the way a sourced shell script would, prints its own progress and the
# metric JSON -- and never echoes an assignment, exactly like the incident's runner.
RUN_PY = ("import json\n"
          f"env = dict(l.split('=', 1) for l in open({ENV!r}).read().split('\\n')\n"
          "           if '=' in l and not l.startswith('#'))\n"
          "print('training with sample', env.get('SFT_EVAL_SAMPLE'))\n"
          "print(json.dumps({'metric': 0.1126388}))\n")


class _Researcher:
    """Asks for a repair on the first failure, then abandons — one repair, then the terminal."""

    def __init__(self):
        self.asked = 0

    def propose(self, state, parent):
        return Idea(operator="x", params={})

    def triage_crash(self, node, error, attempt, **kw):
        self.asked += 1
        if attempt <= 1:
            return {"action": "repair", "rationale": "make the declared path run"}
        return {"action": "abandon", "rationale": "stop after one look"}


class _Dev:
    """Each repair merges the next dict of `plan` onto the node's files (cumulative, as the repo
    Developer hands them back)."""

    def __init__(self, files, plan):
        self.files = dict(files)
        self.plan = list(plan)
        self.errors: list = []
        self.last_files: dict = {}
        self.last_deleted: list = []

    def implement(self, idea):
        return ""

    def repair(self, idea, code, error):
        self.errors.append(error)
        if self.plan:
            self.files.update(self.plan.pop(0))
        self.last_files = dict(self.files)
        return ""


def _run(tmp_path, *, node_files, base=None, mode="graded", gate="audit", plan=(), run_py=RUN_PY):
    """One node over a real repo task: `base` is the operator's tree, `node_files` the node's overlay.
    Returns `(node-0 events, dev, run_dir)`."""
    src, run_dir = tmp_path / "src", tmp_path / "run"
    files = {ENV: BASE_ENV, "run.py": run_py, **(base or {})}
    for rel, body in files.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(body)
    task = RepoTask(id="r", direction="max", editable_path=str(src), edit_surface=["*"],
                    eval=EvalSpec(command=[PY, "run.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}, cwd="."))
    dev = _Dev(node_files, plan)
    eng = Engine(run_dir, task=task, researcher=_Researcher(), developer=dev,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 auto_install_deps=False, inline_repair=True, inline_repair_attempts=3,
                 activation_check=mode, activation_unverified_gate=gate)
    eng.store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": "seed"},
        "code": "", "files": dict(node_files)})

    async def _bounded() -> bool:
        with anyio.move_on_after(300) as scope:
            await eng._evaluate(0, anyio.CapacityLimiter(1), None)
        return scope.cancelled_caught

    assert not anyio.run(_bounded), "the eval did not terminate"
    events = [e for e in EventStore(run_dir / "events.jsonl").read_all()
              if e.data.get("node_id") == 0]
    return events, dev, run_dir


# The eval command's script is the OPERATOR's (protected: a node cannot rewrite `run.py`), so the
# tests whose node changes the code being run hand it `main.py` through this fixed entrypoint.
_ENTRY = {"run.py": "import runpy\nrunpy.run_path('main.py', run_name='__main__')\n",
          "main.py": "print('{}')\n"}


def _types(events, *names):
    return [e for e in events if e.type in names]


def _manifest(markers) -> dict:
    return {act.ACTIVATION_MANIFEST_NAME: act.manifest_text(markers)}


# ------------------------------------------------------------ REGRESSION: the incident

# The base tree of the incident's repo holds the markers' TEXT in code that never prints it:
# `MiniOneRec/sft_resume.py`'s reason string, stored in a dict (a guarded literal the emitter scan
# reads as an EXISTING printer), and the runner's own guarded assignment of the same value.
INCIDENT_BASE = {
    "MiniOneRec/sft_resume.py": (
        "def resolve_resume_settings(env):\n"
        "    every_min = float(env.get('SFT_RESUME_EVERY_MIN') or '60')\n"
        "    why_off = ''\n"
        "    if every_min <= 0:\n"
        "        why_off = \"SFT_RESUME_EVERY_MIN=0\"\n"
        "    return {'enabled': not why_off, 'why_off': why_off}\n"),
    "MiniOneRec/looplab/run_experiment.sh": (
        "if [[ \"${SFT_EVAL_SAMPLE:-0}\" -gt \"$_valid_rows\" ]]; then\n"
        "    SFT_EVAL_SAMPLE=-2; export SFT_EVAL_SAMPLE\n"
        "fi\n"),
}


def test_the_incident_settles_with_its_metric_through_the_env_check(tmp_path):
    """minionerec-lora-v1 node 2 under `graded`, with the incident's own base tree: both markers are
    `NAME=value` assignments the node's changed `experiment.env` makes, so both are the `env` entries
    the static check verifies -- even though `sft_resume.py` holds `why_off =
    "SFT_RESUME_EVERY_MIN=0"`, which the emitter scan reads as an existing printer (TP2). One
    invocation, settled `ok`, `node_evaluated` with the metric, grade weak, no repair.
    MUTATION: disable `normalize_config_assignments` -> marker 2 blocks as TP2, a second invocation."""
    events, dev, run_dir = _run(tmp_path, base=INCIDENT_BASE,
                                node_files={ENV: INCIDENT_ENV, **_manifest(INCIDENT_MARKERS)})
    claims = _types(events, "eval_invocation_claimed")
    settles = _types(events, "eval_invocation_settled")
    assert len(claims) == 1 and [s.data["outcome"] for s in settles] == ["ok"]
    assert not _types(events, "node_repaired") and dev.errors == []
    [terminal] = _types(events, "node_evaluated", "node_failed")
    assert terminal.type == "node_evaluated" and terminal.data["metric"] == 0.1126388
    record = terminal.data["activation"]
    assert (record["verdict"], record["grade"], record["change_class"], record["kinds"]) == (
        "ok", "weak", "config_only", ["env"])
    assert record["missing"] == []
    assert terminal.data["violations"] == []                 # NOT a violations row: still feasible
    node = fold(EventStore(run_dir / "events.jsonl").read_all()).nodes[0]
    assert node.feasible and node.activation["grade"] == "weak"


def test_an_assignment_marker_the_touched_config_does_not_set_keeps_the_log_rules(tmp_path):
    """`USE_X=1` declared while the changed config sets `USE_X=0`: not the env entry -- a log marker
    nothing prints, on a config-only change, so the graded WARN (the metric stands, flagged)."""
    events, _dev, _ = _run(tmp_path, node_files={
        "conf/run.env": "USE_X=0\n", **_manifest(["USE_X=1"])})
    [terminal] = _types(events, "node_evaluated")
    record = terminal.data["activation"]
    assert (record["verdict"], record["kinds"], record["missing"]) == ("warn", ["log"], ["USE_X=1"])


def test_a_config_only_marker_nothing_could_print_settles_flagged_unverified(tmp_path):
    """The graded WARN itself: a config-only change whose marker is no assignment and that no code
    anywhere prints keeps its metric, flagged -- not a violations row."""
    events, dev, _ = _run(tmp_path, node_files={ENV: INCIDENT_ENV, **_manifest(["LORA OFF"])})
    assert len(_types(events, "eval_invocation_claimed")) == 1 and dev.errors == []
    [terminal] = _types(events, "node_evaluated")
    record = terminal.data["activation"]
    assert (record["verdict"], record["grade"], record["change_class"]) == (
        "warn", "weak", "config_only")
    assert record["missing"] == ["LORA OFF"] and record["gate"] == "audit"
    assert terminal.data["violations"] == []


def test_the_incident_under_strict_is_inert_path_as_it_always_was(tmp_path):
    events, dev, _run_dir = _run(tmp_path, mode="strict",
                                 node_files={ENV: INCIDENT_ENV, **_manifest(INCIDENT_MARKERS)})
    assert _types(events, "node_repaired"), "strict sends it to repair, exactly as before"
    [terminal] = _types(events, "node_evaluated", "node_failed")
    assert terminal.type == "node_failed" and terminal.data["reason"] == "inert_path"
    assert "activation" not in json.dumps([e.data for e in _types(events, "node_evaluated")])
    # the historical failure text, byte for byte in its head
    assert dev.errors[0].startswith("[failure kind: inert_path]\n[inert_path] the evaluation "
                                    "finished and printed metric 0.1126388, but the activation "
                                    "marker(s) this node declared its new path prints never appeared")
    # …and the withheld number is a FIELD now, on the repair row and the terminal alike
    assert _types(events, "node_repaired")[0].data["withheld_metric"] == {
        "metric": 0.1126388, "reason": "inert_path"}
    assert terminal.data["withheld_metric"]["metric"] == 0.1126388


def test_the_gate_bars_an_unverified_node_from_best_and_keeps_it_feasible(tmp_path):
    events, _dev, run_dir = _run(tmp_path, gate="gate",
                                 node_files={ENV: INCIDENT_ENV, **_manifest(["LORA OFF"])})
    [terminal] = _types(events, "node_evaluated")
    assert terminal.data["activation"]["gate"] == "gate"
    state = fold(EventStore(run_dir / "events.jsonl").read_all())
    assert state.nodes[0].feasible and 0 in state.breed_excluded
    assert state.best_node_id is None


# ------------------------------------------------------------ TP guards: what strict caught, graded catches

_FALLBACK_SVC = ("def fast():\n    raise AttributeError('removed in transformers 5')\n"
                 "try:\n    fast()\n    print('PREFIX CACHE ON')\nexcept Exception:\n"
                 "    print('falling back')\n")


def test_tp1_a_swallowed_fallback_in_the_new_code_is_still_inert_path(tmp_path):
    """The prefix-cache incident (activation.py's docstring): the new path's printer is in the
    node's CHANGED code, a fallback swallowed it, the metric printed. MUTATION: grade TP1 as warn."""
    run_py = "import svc, json\nprint(json.dumps({'metric': 1.004}))\n"
    events, dev, _ = _run(tmp_path, run_py=run_py, base={"svc.py": "x = 1\n"},
                          node_files={"svc.py": _FALLBACK_SVC, **_manifest(["PREFIX CACHE ON"])})
    [terminal] = _types(events, "node_evaluated", "node_failed")
    assert terminal.type == "node_failed" and terminal.data["reason"] == "inert_path"
    assert "YOUR CHANGED code prints it" in dev.errors[0]


def test_tp2_a_config_change_whose_existing_printer_never_ran_is_still_inert_path(tmp_path):
    """The flag the change sets did not take effect (misspelled), and EXISTING code prints the marker
    when it does. Config-only, and still withheld: an emitter exists. MUTATION: drop the existing-
    emitter branch -> warn."""
    base = {"conf.json": json.dumps({"fast": False}),
            "run.py": ("import json\ncfg = json.load(open('conf.json'))\n"
                       "if cfg.get('fast'):\n    print('FAST PATH ON')\n"
                       "print(json.dumps({'metric': 0.5}))\n")}
    events, dev, _ = _run(tmp_path, base=base, node_files={
        "conf.json": json.dumps({"fats": True}), **_manifest(["FAST PATH ON"])})
    [terminal] = _types(events, "node_evaluated", "node_failed")
    assert terminal.type == "node_failed" and terminal.data["reason"] == "inert_path"
    assert "EXISTING code prints it" in dev.errors[0]


def test_tp3_a_marker_declared_on_a_code_change_with_no_printer_is_still_inert_path(tmp_path):
    """inf12 node 4's shape: declared, never written. Class `code`, nothing prints it -> blocked."""
    events, dev, _ = _run(tmp_path, base={"svc.py": "x = 1\n"}, node_files={
        "svc.py": "x = 2\n", **_manifest(["DEDUP FIRST DECODE ON"])})
    [terminal] = _types(events, "node_evaluated", "node_failed")
    assert terminal.type == "node_failed" and terminal.data["reason"] == "inert_path"
    assert "no code anywhere prints it" in dev.errors[0]


def test_tp3_on_a_code_change_says_a_config_assignment_is_not_a_printed_marker(tmp_path):
    # The node does NOT set it (the base env says 0), so it stays a log marker nothing prints.
    events, dev, _ = _run(tmp_path, base={"svc.py": "x = 1\n"}, node_files={
        "svc.py": "x = 2\n", **_manifest(["SFT_EVAL_SAMPLE=-2"])})
    assert _types(events, "node_failed")[0].data["reason"] == "inert_path"
    assert "config assignment, not a printed marker" in dev.errors[0]
    assert "do not add an echo" in dev.errors[0]
    assert "Do not add an echo just for the check" in dev.errors[0]        # the graded directive


def test_an_unconditional_echo_is_scored_and_graded_weak(tmp_path):
    """69.8: an `echo` added for the check prints whatever happens -- it proves the script ran. The
    metric stands (it was printed), the record says how little that proved."""
    run_py = "import json\nprint('NEW PATH ON')\nprint(json.dumps({'metric': 0.5}))\n"
    events, _dev, _ = _run(tmp_path, base=_ENTRY,
                           node_files={"main.py": run_py, **_manifest(["NEW PATH ON"])})
    [terminal] = _types(events, "node_evaluated")
    assert terminal.data["activation"]["verdict"] == "ok"
    assert terminal.data["activation"]["grade"] == "weak"


def test_a_guarded_printer_in_the_changed_code_is_graded_strong(tmp_path):
    run_py = ("import json, os\nif not os.environ.get('NO_FAST'):\n    print('NEW PATH ON')\n"
              "print(json.dumps({'metric': 0.5}))\n")
    events, _dev, _ = _run(tmp_path, base=_ENTRY,
                           node_files={"main.py": run_py, **_manifest(["NEW PATH ON"])})
    assert _types(events, "node_evaluated")[0].data["activation"]["grade"] == "strong"


def test_a_typed_env_entry_is_checked_statically(tmp_path):
    env = {"kind": "env", "name": "SFT_EVAL_SAMPLE", "equals": "-2", "file": ENV}
    events, _dev, _ = _run(tmp_path, node_files={ENV: INCIDENT_ENV, **_manifest([env])})
    record = _types(events, "node_evaluated")[0].data["activation"]
    assert (record["verdict"], record["grade"], record["kinds"]) == ("ok", "weak", ["env"])
    wrong = dict(env, equals="-1")
    events, dev, _ = _run(tmp_path / "w", node_files={ENV: INCIDENT_ENV, **_manifest([wrong])})
    assert _types(events, "node_failed")[0].data["reason"] == "inert_path"
    assert "does not assign that value" in dev.errors[0]


# ------------------------------------------------------------ freshness: only this attempt's bytes

def test_a_marker_an_earlier_attempt_printed_before_crashing_does_not_vouch(tmp_path):
    """The cross-attempt false credit: attempt 0 prints the marker and crashes; the repair's attempt
    1 takes the fallback and APPENDS to the same `eval.log`. Only attempt 1's bytes count, so its
    metric is withheld. Holds on master since crit_v52 F2 (`attempt_byte_floor`); pinned here for the
    single-command `eval.log` the incident ran on. MUTATION: read `eval.log` from byte 0 -> scored."""
    crash = ("print('NEW PATH ON', flush=True)\nraise RuntimeError('OOM in the new path')\n")
    fallback = "import json\nprint('fallback path')\nprint(json.dumps({'metric': 0.5}))\n"
    events, _dev, run_dir = _run(tmp_path, base=_ENTRY,
                                 node_files={"main.py": crash, **_manifest(["NEW PATH ON"])},
                                 plan=[{"main.py": fallback}])
    log = (run_dir / "nodes" / "node_0" / "eval.log").read_text()
    assert "NEW PATH ON" in log and "fallback path" in log          # one appended log, both attempts
    [terminal] = _types(events, "node_evaluated", "node_failed")
    assert terminal.type == "node_failed" and terminal.data["reason"] == "inert_path"


# ------------------------------------------------------------ re-check without re-run

_PRINTS_ACTIVE = ("import json, os\nif not os.environ.get('NO_NEW'):\n    print('NEW PATH ACTIVE')\n"
                  "print(json.dumps({'metric': 0.75}))\n")


def test_a_manifest_only_repair_proven_by_the_failed_attempt_s_output_settles_without_a_run(tmp_path):
    """The new path RAN and printed `NEW PATH ACTIVE`; the declaration named a text nothing prints
    (TP3, blocked). The repair rewrites ONLY the manifest to the printed text, whose printer is in the
    node's changed code: the engine re-reads that attempt's own bytes and settles -- one invocation.
    MUTATION: drop the re-check call -> a second `eval_invocation_claimed`."""
    events, _dev, _ = _run(tmp_path, base=_ENTRY,
                           node_files={"main.py": _PRINTS_ACTIVE, **_manifest(["NEW PATH ON"])},
                           plan=[_manifest(["NEW PATH ACTIVE"])])
    assert len(_types(events, "eval_invocation_claimed")) == 1
    assert len(_types(events, "node_repaired")) == 1
    [terminal] = _types(events, "node_evaluated", "node_failed")
    assert terminal.type == "node_evaluated" and terminal.data["metric"] == 0.75
    assert terminal.data["activation"]["rechecked"] is True
    assert terminal.data["activation"]["grade"] == "strong"


def test_a_manifest_only_repair_whose_printer_is_existing_code_is_re_evaluated(tmp_path):
    base = {"lib.py": "def go():\n    if True:\n        print('LIB PATH ACTIVE')\n", **_ENTRY}
    run_py = ("import json, lib\nlib.go()\nprint(json.dumps({'metric': 0.75}))\n")
    events, _dev, _ = _run(tmp_path, base=base,
                           node_files={"main.py": run_py, **_manifest(["NEW PATH ON"])},
                           plan=[_manifest(["LIB PATH ACTIVE"])])
    assert len(_types(events, "eval_invocation_claimed")) == 2           # a normal re-evaluation
    assert _types(events, "node_evaluated")[0].data["metric"] == 0.75


def test_a_downgrade_to_none_on_a_code_change_is_re_evaluated(tmp_path):
    events, _dev, _ = _run(tmp_path, base=_ENTRY,
                           node_files={"main.py": _PRINTS_ACTIVE, **_manifest(["NEW PATH ON"])},
                           plan=[_manifest([{"kind": "none", "why": "nothing to print"}])])
    assert len(_types(events, "eval_invocation_claimed")) == 2
    [terminal] = _types(events, "node_evaluated")
    assert terminal.data["activation"]["kinds"] == ["none"]


def test_a_manifest_only_repair_is_not_re_checked_under_strict(tmp_path):
    events, _dev, _ = _run(tmp_path, mode="strict", base=_ENTRY,
                           node_files={"main.py": _PRINTS_ACTIVE, **_manifest(["NEW PATH ON"])},
                           plan=[_manifest(["NEW PATH ACTIVE"])])
    assert len(_types(events, "eval_invocation_claimed")) == 2


# ------------------------------------------------------------ replay

def test_an_old_inert_path_journal_folds_as_before_and_old_rows_carry_no_activation(tmp_path):
    """A strict run's journal -- `node_repaired`, `node_failed inert_path`, no `activation` key
    anywhere -- folds with `Node.activation` None and the node out of every gate; an evaluated row
    with no record folds the same."""
    events, _dev, run_dir = _run(tmp_path, mode="strict",
                                 node_files={ENV: INCIDENT_ENV, **_manifest(INCIDENT_MARKERS)})
    state = fold(EventStore(run_dir / "events.jsonl").read_all())
    node = state.nodes[0]
    assert node.error_reason == "inert_path" and node.activation is None
    assert state.breed_excluded == set()
    events, _dev, run_dir = _run(tmp_path / "plain", node_files={ENV: INCIDENT_ENV})
    node = fold(EventStore(run_dir / "events.jsonl").read_all()).nodes[0]
    assert node.activation is None and "activation" not in _types(events, "node_evaluated")[0].data


# ------------------------------------------------------------ the critic's pass (post-review fixes)

def _lint_then_run(tmp_path, monkeypatch, *, base, writes, markers, **run_kw):
    """The whole chain: a REAL `LLMRepoDeveloper(activation_graded=True)` over the task repo `base`
    writes `writes` and declares `markers` (its graded lint decides what the manifest says), then the
    REAL evaluation loop runs that node. Returns `(events, dev, run_dir, refusals, files)`."""
    import looplab.agents.agent as agent_mod
    from looplab.adapters.repo_task import LLMRepoDeveloper

    refusals: list = []

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        name = emit_spec["function"]["name"]
        if name == "declare_stages":
            return finalize({"stages": []})
        if name == "propose_plan":
            return finalize({"steps": []})
        for path, content in writes.items():
            tools.execute("write_file", {"path": path, "content": content})
        args = {"summary": "s", "activation_markers": list(markers)}
        validate = opts.get("validate")
        if validate is not None and (refusal := validate(dict(args))):
            refusals.append(refusal)
        return finalize(args)

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    repo = tmp_path / "devrepo"
    files = {ENV: BASE_ENV, "run.py": RUN_PY, **base}
    for rel, body in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(body)
    task = RepoTask(id="r", goal="g", direction="max", editable_path=str(repo), edit_surface=["*"],
                    protect=[], eval=EvalSpec(command=[PY, "run.py"],
                                              metric={"kind": "stdout_json", "key": "metric"}))
    dev = LLMRepoDeveloper(object(), task, plan_decompose=False, activation_graded=True)
    dev.implement(Idea(operator="draft", params={}, rationale="x"))
    node_files = {p: b for p, b in dev.last_files.items()
                  if p in writes or p == act.ACTIVATION_MANIFEST_NAME}
    events, rdev, run_dir = _run(tmp_path / "eval", base=base, node_files=node_files, **run_kw)
    return events, rdev, run_dir, refusals, node_files


# BLOCKER 1: the new path is guarded by a flag the node sets in its config and swallows its own
# exception; `USE_NEW=1` is printed by the NEW code only when the path ran.
_P1_MAIN = ("import json\n"
            "env = dict(l.split('=',1) for l in open('conf/run.env').read().split() if '=' in l)\n"
            "def fast():\n    raise AttributeError('gone')\n"
            "if env.get('USE_NEW') == '1':\n"
            "    try:\n        fast()\n        print('USE_NEW=1')\n"
            "    except Exception:\n        pass\n"
            "print(json.dumps({'metric': 1.004}))\n")


def test_tp1_behind_a_flag_the_code_change_sets_is_not_read_as_an_env_entry(tmp_path):
    """Critic BLOCKER 1 (probe P1): on a CODE change `USE_NEW=1` is what the new path prints; the
    assignment rule read it as "the value is set" (the node's config sets it), the env check held,
    and a swallowed fallback was scored. MUTATION: apply the rewrite on code changes -> evaluated."""
    base = dict(_ENTRY, **{"conf/run.env": "USE_NEW=0\n"})
    events, dev, _ = _run(tmp_path, base=base, node_files={
        "main.py": _P1_MAIN, "conf/run.env": "USE_NEW=1\n", **_manifest(["USE_NEW=1"])})
    [terminal] = _types(events, "node_evaluated", "node_failed")
    assert terminal.type == "node_failed" and terminal.data["reason"] == "inert_path"
    assert "YOUR CHANGED code prints it" in dev.errors[0]


def test_tp1_behind_a_flag_through_the_real_developer_lint(tmp_path, monkeypatch):
    base = dict(_ENTRY, **{"conf/run.env": "USE_NEW=0\n"})
    events, _dev, _, refusals, files = _lint_then_run(
        tmp_path, monkeypatch, base=base, markers=["USE_NEW=1"],
        writes={"main.py": _P1_MAIN, "conf/run.env": "USE_NEW=1\n"})
    assert json.loads(files[act.ACTIVATION_MANIFEST_NAME]) == {"markers": ["USE_NEW=1"]}
    assert _types(events, "node_failed")[0].data["reason"] == "inert_path"


def test_an_untouched_base_config_never_vouches_for_a_code_change(tmp_path, monkeypatch):
    """Critic MAJOR 2, end to end: the base `conf/base.env` already says `USE_FUSED=1`; the code
    change's new path (an f-string print, behind a swallowed fallback) never ran. The lint rewrote
    the marker to an env entry on the BASE file, which held whatever happened, and the eval scored.
    Now the lint bounces with the advice and keeps the log marker, and the eval withholds (TP3: no
    literal printer). MUTATION: the old `configured and m` rewrite -> evaluated, env/weak."""
    main = ("import json\ndef fast():\n    raise AttributeError('gone')\n"
            "try:\n    fast()\n    print(f'USE_FUSED={1}')\nexcept Exception as e:\n"
            "    print('fused path unavailable:', e)\n"
            "print(json.dumps({'metric': 1.004}))\n")
    base = dict(_ENTRY, **{"conf/base.env": "USE_FUSED=1\n"})
    events, _dev, _, refusals, files = _lint_then_run(
        tmp_path, monkeypatch, base=base, markers=["USE_FUSED=1"], writes={"main.py": main})
    assert refusals and "declare it explicitly as" in refusals[0]
    assert json.loads(files[act.ACTIVATION_MANIFEST_NAME]) == {"markers": ["USE_FUSED=1"]}
    [terminal] = _types(events, "node_evaluated", "node_failed")
    assert terminal.type == "node_failed" and terminal.data["reason"] == "inert_path"


def test_the_repo_developer_s_config_only_marker_reaches_the_unverified_warn(tmp_path, monkeypatch):
    """Critic MAJOR 5, end to end: the lint rewrote a config-only marker nothing prints to `none`, so
    the node settled `ok` and the WARN -- and `activation_unverified_gate` -- could never fire for
    the in-house Developer. Kept as the log marker, settle records the WARN and the gate bars it.
    MUTATION: rewrite to none -> verdict ok, not gated."""
    events, _dev, run_dir, refusals, files = _lint_then_run(
        tmp_path, monkeypatch, base={}, markers=["LORA OFF"], writes={ENV: INCIDENT_ENV},
        gate="gate")
    assert refusals == []
    [terminal] = _types(events, "node_evaluated")
    assert (terminal.data["activation"]["verdict"], terminal.data["activation"]["gate"]) == (
        "warn", "gate")
    assert 0 in fold(EventStore(run_dir / "events.jsonl").read_all()).breed_excluded


# MAJOR 3: a TP1 block is not re-checkable. The except branch of the very file that fell back prints
# `cache: falling back`; a manifest-only repair declaring THAT line re-checked as `strong`.
_SWALLOWED = ("import json\ndef fast():\n    raise AttributeError('removed')\n"
              "try:\n    fast()\n    print('PREFIX CACHE ON')\nexcept Exception:\n"
              "    print('cache: falling back')\n"
              "print(json.dumps({'metric': 1.004}))\n")


def test_a_tp1_block_is_never_re_checked_from_the_same_log(tmp_path):
    """Critic MAJOR 3 (probe P2): the attempt is blocked as TP1 (the changed code prints `PREFIX
    CACHE ON`, it never appeared); the repair swaps the declaration for the fallback's own line. That
    is not a declaration error, so no re-check: the evaluation runs again (it then measures what the
    new declaration says -- the engine cannot tell a fallback's words from a path's, and the
    contract is the declaration). MUTATION: drop the cause gate -> one invocation, rechecked."""
    events, _dev, _ = _run(tmp_path, base=_ENTRY,
                           node_files={"main.py": _SWALLOWED, **_manifest(["PREFIX CACHE ON"])},
                           plan=[_manifest(["cache: falling back"])])
    assert len(_types(events, "eval_invocation_claimed")) == 2
    rechecked = [e for e in _types(events, "node_evaluated")
                 if (e.data.get("activation") or {}).get("rechecked")]
    assert rechecked == []


def test_re_check_refuses_when_the_changed_code_is_not_what_the_attempt_left(tmp_path):
    """Critic MINOR 8a: the evaluation rewrote its own changed file (here it appends to itself), so
    the bytes on disk at repair time are not what that attempt left -- no re-check, a re-run.
    MUTATION: drop the digest comparison -> one invocation."""
    selfmod = _PRINTS_ACTIVE + "open(__file__, 'a').write('# ran\\n')\n"
    events, _dev, _ = _run(tmp_path, base=_ENTRY,
                           node_files={"main.py": selfmod, **_manifest(["NEW PATH ON"])},
                           plan=[_manifest(["NEW PATH ACTIVE"])])
    assert len(_types(events, "eval_invocation_claimed")) == 2


def test_re_check_refuses_when_the_attempt_s_log_bytes_cannot_be_re_read(tmp_path):
    """Critic MINOR 8c: a log the failed attempt wrote (and the check read) is REPLACED before the
    re-check -- here re-materialized from the node's files -- so its bytes are no longer the
    attempt's: no re-check, a re-run, even though the marker is in the captured stdout.
    MUTATION: `read_log_spans` None -> [] -> one invocation."""
    writes_log = _PRINTS_ACTIVE + "open('side.log', 'w').write('side channel\\n')\n"
    events, _dev, _ = _run(tmp_path, base=_ENTRY,
                           # the same bytes the eval writes, so the changed-code digest still
                           # matches and only the log's IDENTITY (a fresh inode) moved
                           node_files={"main.py": writes_log, "side.log": "side channel\n",
                                       **_manifest(["NEW PATH ON"])},
                           plan=[_manifest(["NEW PATH ACTIVE"])])
    assert len(_types(events, "eval_invocation_claimed")) == 2


def test_a_printer_in_a_code_file_the_scan_cannot_read_blocks_instead_of_warning(tmp_path, monkeypatch):
    """Critic MAJOR 4b: a code file past the scan's size bound (or a link) was skipped with the scan
    still `complete`, so a config-only marker whose printer lives THERE read as "printed nowhere" and
    WARNed. The scan is incomplete now; the printer is unknown, and unknown blocks.
    MUTATION: skip the oversized file with complete=True -> evaluated, WARN."""
    monkeypatch.setattr(act, "_SCAN_MAX_FILE_BYTES", 4096)
    big = "def go(on):\n    if on:\n        print('BIG PATH ON')\n" + "# pad\n" * 1000
    events, dev, _ = _run(tmp_path, base={"lib/big.py": big},
                          node_files={ENV: INCIDENT_ENV, **_manifest(["BIG PATH ON"])})
    [terminal] = _types(events, "node_evaluated", "node_failed")
    assert terminal.type == "node_failed" and terminal.data["reason"] == "inert_path"
    assert "could not establish which code prints it" in dev.errors[0]


def test_a_printer_in_a_cython_module_is_an_existing_printer(tmp_path):
    """Critic MAJOR 4b, the suffix half: a `.pyx` printer was never read, so a config-only change
    that failed to enable it WARNed instead of blocking as TP2. MUTATION: drop `.pyx` -> WARN."""
    pyx = "def go(bint on):\n    if on:\n        print('CY PATH ON')\n"
    events, dev, _ = _run(tmp_path, base={"ext/fast.pyx": pyx},
                          node_files={ENV: INCIDENT_ENV, **_manifest(["CY PATH ON"])})
    assert _types(events, "node_failed")[0].data["reason"] == "inert_path"
    assert "EXISTING code prints it" in dev.errors[0]


def test_strict_reads_a_typed_manifest_exactly_as_master_did(tmp_path):
    """Critic MINOR 6: master's check reads the manifest's STRINGS only (`read_markers`); a typed
    entry is no declaration there. Under strict an env entry the config does not satisfy must be
    ignored, as before -- not withheld. MUTATION: read_manifest under strict -> inert_path."""
    wrong = {"kind": "env", "name": "SFT_EVAL_SAMPLE", "equals": "-1", "file": ENV}
    events, _dev, _ = _run(tmp_path, mode="strict",
                           node_files={ENV: INCIDENT_ENV, **_manifest([wrong])})
    [terminal] = _types(events, "node_evaluated", "node_failed")
    assert terminal.type == "node_evaluated" and "activation" not in terminal.data
    # …and its strings are still held to the historical rule
    events, _dev, _ = _run(tmp_path / "s", mode="strict",
                           node_files={ENV: INCIDENT_ENV, **_manifest([wrong, "NEVER PRINTED"])})
    assert _types(events, "node_failed")[0].data["reason"] == "inert_path"


def test_a_stage_manifest_only_change_is_a_code_change(tmp_path):
    """Critic MINOR 7: `looplab_stages.json` is the pipeline's COMMANDS; by its `.json` suffix a
    change to it alone read as config-only, so a marker nothing prints WARNed where a code change's
    TP3 blocks. MUTATION: drop it from EXECUTED_MANIFESTS -> config_only, WARN."""
    from looplab.engine.eval_stages import STAGE_MANIFEST_NAME
    assert STAGE_MANIFEST_NAME in act.EXECUTED_MANIFESTS
    assert act.change_class({STAGE_MANIFEST_NAME}) == act.CHANGE_CODE
    stages = json.dumps({"stages": [{"name": "train", "command": [PY, "run.py"], "timeout": 120}]})
    events, dev, _ = _run(tmp_path, node_files={STAGE_MANIFEST_NAME: stages,
                                                 **_manifest(["STAGED PATH ON"])})
    [terminal] = _types(events, "node_evaluated", "node_failed")
    assert terminal.type == "node_failed" and terminal.data["reason"] == "inert_path"
    assert "no code anywhere prints it" in dev.errors[0]
