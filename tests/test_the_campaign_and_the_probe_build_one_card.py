"""Two drivers of one experiment must hand the model the same goal card, or their numbers are not one scale.

THE DEFECT, measured on af13b4dd. `--enforce-rules` appears once in `run_probe.sh` and zero times
in `campaign.sh`, whose only card input was `${MAKE_TASK_ARGS:-}` — empty by default. Building both
cards over one synthetic checkout on 2026-08-30: the campaign's goal was 5,010 characters and the
probe's 10,111. The campaign card carried no YOUR OUTPUT IS THE FILE, no ONE HYPOTHESIS, no rules
clause and no solution-space clause.

The flag is not only prose. It rides into the `eval_train` developer command and the `score` stage,
where it runs AlgoTune's OWN validator over the candidate. Without it arm B may submit a solver arm
A cannot even WRITE — `editor_functions.py` refuses that edit at authoring time — so arm B could
score and win on a primitive the other arm is physically unable to use, and nothing in the result
would say so.

The second half is the same fact from the other side. `campaign.sh` exports `PYTHONPATH=$REPO`;
`run_probe.sh` did not, and `cd "$ROOT/looplab"` does not help because `python3 <path>/make_task.py`
puts the SCRIPT's directory on `sys.path`, not the working one. So `session_budget_s()` returned
None under the probe and the card lost the fraction: the campaign said "bounded at <N> s ... about
3 % of your session", the probe said "bounded by a wall clock nobody shows you". One card, two
sentences, and the numbers compared as though they were one.

HOW THIS IS DRIVEN. The two `make_task.py` invocations are lifted out of the two scripts and RUN,
with an argv recorder standing in for the interpreter, so what is compared is what each driver
really passes. Those argv lists are then handed to the real `make_task.py` over one synthetic
AlgoTune root, and the two goal cards must be identical.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN = ROOT / "benchmarks" / "algotune" / "campaign.sh"
PROBE = ROOT / "benchmarks" / "algotune" / "run_probe.sh"
MAKE_TASK = ROOT / "benchmarks" / "algotune" / "make_task.py"
# The invocation, not the mere mention: both files discuss `make_task.py` in prose above it.
_MAKE_TASK_CALL = 'make_task.py" --algotune-root'

# The synthetic AlgoTune checkout `make_task.py` accepts, borrowed from the file that already
# builds one rather than described a second time.
_COST = importlib.util.spec_from_file_location(
    "_eval_cost_clause_fixtures", ROOT / "tests" / "test_algotune_eval_cost_clause.py")
_FIX = importlib.util.module_from_spec(_COST)
_COST.loader.exec_module(_FIX)


def _command(script: Path, needle: str) -> str:
    """The whole logical shell command containing `needle`, continuations included."""
    lines = script.read_text(encoding="utf-8").splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if needle in ln and not ln.lstrip().startswith("#")), None)
    assert start is not None, f"{script.name} no longer invokes {needle}"
    out = []
    i = start
    while True:
        out.append(lines[i])
        if not lines[i].rstrip().endswith("\\"):
            break
        i += 1
    return "\n".join(out)


def _recorded_argv(tmp: Path, preamble: str, command: str, names: tuple[str, ...]) -> list[str]:
    """Run the driver's real command line with the interpreter replaced by an argv recorder."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    argv = tmp / "argv.json"
    for name in names:
        (bin_dir / name).write_text(
            "#!/bin/sh\n"
            f'{sys.executable} -c "import json,sys; json.dump(sys.argv[1:], '
            f"open('{argv}','w'))\" \"$@\"\n",
            encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    env.pop("CARD_ARGS", None)
    env.pop("MAKE_TASK_ARGS", None)
    got = subprocess.run(["bash", "-c", "set -u\n" + preamble + "\n" + command],
                         capture_output=True, text=True, timeout=60, env=env, cwd=str(tmp))
    assert got.returncode == 0, got.stdout + got.stderr
    assert argv.exists(), (f"the command never reached the interpreter:\n{command}\n"
                           f"{got.stdout}{got.stderr}")
    return json.loads(argv.read_text(encoding="utf-8"))


def _campaign_argv(tmp: Path) -> list[str]:
    src = CAMPAIGN.read_text(encoding="utf-8")
    card = next((ln for ln in src.splitlines() if ln.startswith("CARD_ARGS=")), None)
    assert card, "campaign.sh no longer declares the card its arm is defined by"
    preamble = (f'REPO="{ROOT}"\nAT="{tmp}/AlgoTune"\nWS="{tmp}/ws"\nT=demo\n{card}')
    return _recorded_argv(tmp, preamble, _command(CAMPAIGN, _MAKE_TASK_CALL), ("python", "python3"))


def _probe_argv(tmp: Path) -> list[str]:
    preamble = (f'ROOT="{tmp}/stand"\nOUT="{tmp}/out"\nTASK=demo\nLOG="{tmp}/probe.log"\n'
                'say() { echo "$*"; }')
    return _recorded_argv(tmp, preamble, _command(PROBE, _MAKE_TASK_CALL), ("python", "python3"))


def _flags(argv: list[str]) -> set[str]:
    """The card-shaping options, with the paths that legitimately differ dropped."""
    skip = {"--algotune-root", "--task", "--out-dir", "--python", "--timeout"}
    out, i = set(), 0
    while i < len(argv):
        token = argv[i]
        if token in skip:
            i += 2
            continue
        if token.startswith("--"):
            out.add(token)
        i += 1
    return out


def test_the_two_drivers_ask_for_the_same_clauses(tmp_path):
    """The falsifier for a campaign whose card is an environment variable nobody exported."""
    campaign = _flags(_campaign_argv(tmp_path / "camp"))
    probe = _flags(_probe_argv(tmp_path / "probe"))
    assert campaign == probe, (
        f"the campaign builds a different card from the probe: only in the campaign "
        f"{campaign - probe}, only in the probe {probe - campaign}")
    assert "--enforce-rules" in campaign, (
        "nothing runs the arena's own validator, so arm B may submit a solver arm A cannot write")


def _card(tmp: Path, argv: list[str], *, pythonpath: str | None) -> dict:
    root = _FIX._make_root(tmp, "demo")
    out = tmp / "ws"
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    call = [sys.executable, str(MAKE_TASK), "--algotune-root", str(root), "--task", "demo",
            "--out-dir", str(out), *sorted(_flags(argv))]
    got = subprocess.run(call, capture_output=True, text=True, timeout=300, env=env)
    assert got.returncode == 0, got.stdout + got.stderr
    return json.loads((out / "algotune_demo.json").read_text(encoding="utf-8"))


def test_the_two_drivers_produce_the_same_goal(tmp_path):
    """Not the flags — the CARD. Both argv lists through the real generator, one checkout."""
    campaign = _card(tmp_path / "a", _campaign_argv(tmp_path / "camp"), pythonpath=str(ROOT))
    probe = _card(tmp_path / "b", _probe_argv(tmp_path / "probe"), pythonpath=str(ROOT))
    assert campaign["goal"] == probe["goal"], (
        f"campaign {len(campaign['goal'])} chars, probe {len(probe['goal'])}")


def test_the_probe_puts_the_engine_where_the_card_can_read_it(tmp_path):
    """`session_budget_s()` reads the ceiling out of `Settings`; unreachable, the card drops the
    fraction and says "a wall clock nobody shows you". `cd $ROOT/looplab` does not fix that — a
    script run BY PATH puts its own directory on `sys.path`, not the working one."""
    src = PROBE.read_text(encoding="utf-8")
    line = next((ln for ln in src.splitlines() if ln.startswith("export PYTHONPATH=")), None)
    assert line, "run_probe.sh exports no PYTHONPATH, so make_task.py cannot import the engine"
    stand = tmp_path / "stand"
    stand.mkdir()
    (stand / "looplab").symlink_to(ROOT)
    got = subprocess.run(["bash", "-c", f'set -u\nROOT="{stand}"\n{line}\necho "$PYTHONPATH"'],
                         capture_output=True, text=True, timeout=60,
                         env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
    assert got.returncode == 0, got.stderr
    resolved = got.stdout.strip()
    card = _card(tmp_path / "c", ["--deliver", "--one-card", "--enforce-rules"],
                 pythonpath=resolved)
    assert "bounded by a wall clock nobody shows you" not in card["goal"], (
        "the probe's own PYTHONPATH does not make the engine importable, so its card still states "
        "no session budget while the campaign's states one")
    assert "bounded at" in card["goal"], card["goal"][:400]
