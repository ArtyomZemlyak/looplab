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
