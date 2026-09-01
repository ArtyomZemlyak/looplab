"""Sweep item 9's seven questions, computed once instead of hand-rolled every sweep.

For three sweeps running I answered "champion, test against train, money by phase, spend after the
last evaluated node, eval_train count, does the model use the reference module" by writing the same
throwaway script again. Each rewrite is a chance to compute a slightly different thing and call it
the same number.

It earned its keep on the first run: it flagged `remPde`'s champion as carrying a kernel, which
contradicted §72 and §73.2 -- both of which called that run plain Python. The champion has four
`@njit` kernels behind an indented `try:` import that an earlier grep never saw. Two sections were
corrected.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "benchmarks" / "probe_summary.py"


def _mk_probe(root, name, task, *, nodes, costs_before, costs_after, test=None,
              champion=None, eval_trains=0):
    """A probe tree shaped like the real one: run/{events,spans}.jsonl + final.json + champion."""
    run = root / "model-probes" / name / "runs" / task / "run"
    run.mkdir(parents=True, exist_ok=True)
    t = 1000.0
    # A real run always has events before its first node -- `run_started`, `llm_usage`, and so on --
    # and `summarise` skips a tree with an empty events.jsonl entirely. A fixture without one is a
    # run that cannot exist, and it made the no-nodes case look like a disappearing probe.
    events, spans = [{"type": "run_started", "ts": t, "data": {}}], []

    # NOT `plan_step` by default. It was, and that silently made every fixture reach its first
    # build step at minute zero -- so the time-to-build test asserted 30m against a run that had
    # "built" before it started, and the tool was right while the harness was lying.
    def gen(ts, cost, phase="propose"):
        spans.append({"name": "generation", "start": ts, "duration_s": 1.0,
                      "attributes": {"cost": cost, "phase": phase}})

    for c in costs_before:
        t += 1
        gen(t, c)
    for m in nodes:
        t += 1
        events.append({"type": "node_evaluated", "ts": t, "data": {"metric": m}})
    for c in costs_after:
        t += 1
        gen(t, c)
    for i in range(eval_trains):
        spans.append({"name": "tool", "start": t + i,
                      "attributes": {"tool": "run_dev_command", "args": {"name": "eval_train"}}})

    (run / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    (run / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans))
    probe = root / "model-probes" / name
    if test is not None:
        (probe / "final.json").write_text(json.dumps({"speedup": test, "subset": "test"}))
    if champion is not None:
        (probe / "champion_solver.py").write_text(champion)
    return probe


def _run(*roots):
    r = subprocess.run([sys.executable, str(TOOL), *[str(x) for x in roots]],
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def test_it_reports_spend_before_the_first_node_not_only_after_the_last(tmp_path):
    """§72's finding: the waste metric pointed at the wrong end of the run."""
    _mk_probe(tmp_path, "p1", "t", nodes=[10.0],
              costs_before=[0.8], costs_after=[0.2], test=10.0)
    out = _run(tmp_path)
    row = next(l for l in out.splitlines() if l.startswith("p1"))
    assert "80%" in row, f"spend BEFORE the first node is not reported: {row}"
    assert "20%" in row, f"spend AFTER the last node is not reported: {row}"


def test_a_kernel_behind_an_indented_import_is_still_a_kernel(tmp_path):
    """The exact miss that put a false row into two sections of docs/56."""
    body = ("try:\n    from numba import njit\n    _HAVE_NUMBA = True\n"
            "except Exception:\n    _HAVE_NUMBA = False\n\nif _HAVE_NUMBA:\n    @njit\n"
            "    def k(x):\n        return x\n")
    _mk_probe(tmp_path, "p2", "t", nodes=[5.0], costs_before=[0.5], costs_after=[0.5],
              test=5.0, champion=body)
    out = _run(tmp_path)
    row = next(l for l in out.splitlines() if l.startswith("p2"))
    assert "kernel" in row and "plain python" not in row, (
        "a numba kernel behind an indented try: import was read as plain Python -- the miss that "
        f"produced the corrected rows in §72 and §73.2: {row}"
    )


def test_plain_python_is_still_called_plain_python(tmp_path):
    _mk_probe(tmp_path, "p3", "t", nodes=[5.0], costs_before=[0.5], costs_after=[0.5],
              test=5.0, champion="def solve(x):\n    return x\n")
    out = _run(tmp_path)
    row = next(l for l in out.splitlines() if l.startswith("p3"))
    assert "plain python" in row, f"a kernel was invented where there is none: {row}"


def test_a_probe_in_two_roots_is_reported_once_and_the_fuller_copy_wins(tmp_path):
    """Live tree and archive hold the same run; the archive copy can be stale."""
    live, arch = tmp_path / "live", tmp_path / "arch"
    _mk_probe(live, "dup", "t", nodes=[9.0], costs_before=[0.6, 0.3], costs_after=[0.1], test=9.0)
    _mk_probe(arch, "dup", "t", nodes=[9.0], costs_before=[0.2], costs_after=[], test=9.0)
    # ARCHIVE FIRST, deliberately. With the live root first, "keep the first one seen" and "keep
    # the fuller one" agree, and mutation showed the test passed with the freshness rule deleted.
    out = _run(arch, live)
    rows = [l for l in out.splitlines() if l.startswith("dup")]
    assert len(rows) == 1, f"one probe reported {len(rows)} times:\n{out}"
    assert "1.0000" in rows[0], (
        f"the STALE archive copy won over the fuller live one: {rows[0]}"
    )


def test_a_probe_with_no_final_json_says_so_instead_of_inventing_a_score(tmp_path):
    _mk_probe(tmp_path, "p4", "t", nodes=[3.0], costs_before=[0.5], costs_after=[0.5])
    out = _run(tmp_path)
    row = next(l for l in out.splitlines() if l.startswith("p4"))
    assert " - " in row or row.rstrip().endswith("-") or "  -" in row, (
        f"a probe with no test result did not report it as absent: {row}"
    )


def test_a_final_json_without_a_number_is_absent_not_zero(tmp_path):
    """A run whose final.json carries `speedup: null` has NO score; 0.0 is a real and terrible one.

    The first version of the absent-score test used a probe with no final.json at all, so the line
    that coerces the value never ran -- mutation turned it into `float(sp or 0.0)` and the file
    stayed green while every unscored probe would have been reported as a zero.
    """
    p = _mk_probe(tmp_path, "p6", "t", nodes=[3.0], costs_before=[0.5], costs_after=[0.5])
    (p / "final.json").write_text(json.dumps({"speedup": None, "subset": "test"}))
    out = _run(tmp_path)
    row = next(l for l in out.splitlines() if l.startswith("p6"))
    assert "0.0000" not in row, (
        f"a null speedup was reported as a score of zero, which is a different fact: {row}"
    )


def test_eval_train_is_counted_and_other_dev_commands_are_not(tmp_path):
    p = _mk_probe(tmp_path, "p5", "t", nodes=[1.0], costs_before=[0.5], costs_after=[0.5],
                  test=1.0, eval_trains=3)
    run = p / "runs" / "t" / "run"
    extra = {"name": "tool", "start": 5000,
             "attributes": {"tool": "run_dev_command", "args": {"name": "check"}}}
    with open(run / "spans.jsonl", "a") as fh:
        fh.write(json.dumps(extra) + "\n")
    out = _run(tmp_path)
    row = next(l for l in out.splitlines() if l.startswith("p5"))
    assert row.split()[-3] == "3" or " 3 " in row, (
        f"eval_train count is wrong -- `check` should not be counted: {row}"
    )


def test_an_empty_box_says_so(tmp_path):
    out = _run(tmp_path)
    assert "no probes" in out


# ------------------------------------------------------- a missing score is not one fact either


def test_a_missing_score_comes_with_the_probe_s_own_explanation(tmp_path):
    """`accEE` had no TEST because champion extraction died on an import, on top of an auto-pause.

    Both sentences were in the probe's own two log files, and finding them took six commands. The
    same shape as the zeros section: the diagnosis exists, in a file nothing read.
    """
    p = _mk_probe(tmp_path, "acc", "t", nodes=[221.539], costs_before=[0.3], costs_after=[0.7])
    (p / "probe.log").write_text(
        "[02:17:33] ===== start =====\n"
        "[04:02:54] прогон rc=0 за 6321с\n"
        "could not fold /x/runs/t/run: ModuleNotFoundError: No module named 'looplab'\n"
        "[04:02:54] чемпион: НЕТ\n")
    out = _run(tmp_path)
    assert "ModuleNotFoundError" in out, (
        "a probe with no test score gave no reason, and the reason was in its own log:\n" + out
    )
    # The HEADER too, not just the line: mutation showed that deleting it left the detail floating
    # unlabelled among the per-probe output, where a reader has no idea what it is asserting.
    assert "probes with NO test score" in out, (
        "the reason line is printed with nothing saying what it is:\n" + out
    )


def test_a_pause_is_reported_when_that_is_the_reason(tmp_path):
    p = _mk_probe(tmp_path, "pz", "t", nodes=[5.0], costs_before=[0.5], costs_after=[0.5])
    (p / "run.log").write_text(
        "run=run task=t finished=False\n"
        "stop: PAUSED (node 2) — resumable, NOT finished\n"
        "  pause reason: auto-paused: a Developer session crashed\n")
    out = _run(tmp_path)
    assert "PAUSED" in out, "a paused run's own stop line is not surfaced:\n" + out


def test_a_scored_probe_gets_no_excuse_line(tmp_path):
    """The section is for probes with NO score; a scored one must not appear in it."""
    p = _mk_probe(tmp_path, "ok2", "t", nodes=[5.0], costs_before=[0.5], costs_after=[0.5], test=5.0)
    (p / "probe.log").write_text("could not fold something irrelevant\n")
    out = _run(tmp_path)
    tail = out.split("probes with NO test score", 1)
    assert len(tail) == 1 or "ok2" not in tail[1].split("per-probe detail")[0], (
        "a probe that HAS a test score was listed among those that do not:\n" + out
    )


def test_run_finished_is_not_used_as_the_discriminator(tmp_path):
    """It is absent from four probes on this box that all scored a test perfectly well.

    Reading its absence as "unfinished" would mark accPde, remDL3, remEE and remEE2 as failures.
    """
    import inspect
    src = (REPO / "benchmarks" / "probe_summary.py").read_text()
    i = src.index("def _why_no_test")
    body = src[i:src.index("def summarise")]
    assert "run_finished" not in body.split('"""')[2:] or True  # docstring may mention it
    code = body.split('"""')[-1]
    assert "run_finished" not in code, (
        "run_finished crept into the discriminator; its absence means nothing on its own"
    )


def test_an_unscored_run_with_nodes_is_told_it_is_recoverable_for_free(tmp_path):
    """A run that spent its budget and reached a node has already paid for everything expensive.

    `accEE` sat unscored for twenty hours after an import bug that was fixed the same morning.
    Recovering it cost two commands and $0, and the score came back 224.8846 -- within 0.2 % of the
    figure the brief had been carrying with no evidence behind it on this box. Nothing said the
    recovery was possible, which is the only reason it waited.
    """
    p = _mk_probe(tmp_path, "acc2", "t", nodes=[221.539], costs_before=[0.3], costs_after=[0.7])
    (p / "probe.log").write_text("could not fold /x: ModuleNotFoundError: No module named 'looplab'\n")
    out = _run(tmp_path)
    assert "recoverable for $0" in out, (
        "an unscored run holding an evaluated node was not told it can be re-scored for nothing:\n"
        + out
    )
    assert "extract_champion.py" in out and "--subset test" in out, (
        "the recovery is named but not spelled out, so it still costs a reconstruction:\n" + out
    )
    assert "looplab resume" not in out, (
        "resume continues the RUN and spends more money; accEE had already spent $1.0042 of $1.00, "
        "so resuming would break the budget contract that makes it comparable"
    )


def test_an_unscored_run_with_no_nodes_is_not_promised_a_recovery(tmp_path):
    """Nothing to extract: a run that never evaluated a node cannot be re-scored at any price."""
    p = _mk_probe(tmp_path, "empty2", "t", nodes=[], costs_before=[0.4], costs_after=[])
    (p / "probe.log").write_text("could not fold /x: ModuleNotFoundError\n")
    out = _run(tmp_path)
    block = out.split("probes with NO test score", 1)[-1].split("per-probe detail")[0]
    assert "empty2" in block, "the unscored run vanished entirely"
    assert "recoverable for $0" not in block, (
        "a run with zero evaluated nodes was promised a free recovery there is nothing to do:\n"
        + block
    )


def test_time_to_the_first_build_step_is_reported(tmp_path):
    """A sweep that cannot tell a slow TASK from a stuck RUN investigates every one of them.

    Two discrete_log probes at 55 minutes with no build looked stuck; their task's completed runs
    reach a first build step at 64 and 74 minutes, so they were on schedule. Four commands to find
    that out, hence the column.
    """
    p = _mk_probe(tmp_path, "slow", "t", nodes=[5.0], costs_before=[0.5], costs_after=[0.5], test=5.0)
    run = p / "runs" / "t" / "run"
    spans = [json.loads(l) for l in open(run / "spans.jsonl")]
    t0 = min(float(x["start"]) for x in spans)
    spans.append({"name": "generation", "start": t0 + 1800, "duration_s": 1.0,
                  "attributes": {"cost": 0.01, "phase": "plan_step"}})
    (run / "spans.jsonl").write_text("".join(json.dumps(x) + "\n" for x in spans))
    out = _run(tmp_path)
    row = next(l for l in out.splitlines() if l.startswith("slow"))
    assert row.split()[-2] == "30m", f"time to the first build step is not reported: {row}"


def test_a_run_that_has_not_built_yet_shows_a_dash_not_a_zero(tmp_path):
    """Zero minutes would read as "built instantly", which is the opposite of the truth."""
    _mk_probe(tmp_path, "nobuild", "t", nodes=[], costs_before=[0.3], costs_after=[])
    out = _run(tmp_path)
    row = next(l for l in out.splitlines() if l.startswith("nobuild"))
    assert row.split()[-2] == "-", (
        f"a run with no build step yet was reported as having built at minute zero: {row}"
    )


def test_only_plan_step_counts_as_a_build(tmp_path):
    """`propose` and `deep_research` are what a slow run is BUSY with; they are not building."""
    p = _mk_probe(tmp_path, "prop", "t", nodes=[], costs_before=[], costs_after=[])
    run = p / "runs" / "t" / "run"
    spans = [json.loads(l) for l in open(run / "spans.jsonl")] if (run / "spans.jsonl").read_text().strip() else []
    base = 1000.0
    for ph in ("propose", "deep_research", "plan"):
        spans.append({"name": "generation", "start": base + 60, "duration_s": 1.0,
                      "attributes": {"cost": 0.01, "phase": ph}})
    (run / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans))
    out = _run(tmp_path)
    row = next(l for l in out.splitlines() if l.startswith("prop"))
    assert row.split()[-2] == "-", (
        f"propose/deep_research/plan were counted as reaching a build step: {row}"
    )


def _mk_run_probe(root, name, task, *, total, with_import=0, with_call=0):
    """Append `run_probe` tool spans, some carrying reference usage."""
    run = root / "model-probes" / name / "runs" / task / "run"
    spans = [json.loads(l) for l in open(run / "spans.jsonl")] if (run / "spans.jsonl").is_file() else []
    for i in range(total):
        code = "x = 1\n"
        if i < with_import:
            code = "from reference_t import Task\n" + code
        if i < with_call:
            code += "Task().is_solution(p, s)\n"
        spans.append({"name": "tool", "start": 2000 + i, "duration_s": 0.1,
                      "attributes": {"tool": "run_probe", "args": {"code": code}}})
    (run / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans))


def test_reference_use_is_reported_as_a_share_of_run_probe_calls(tmp_path):
    """§69.1 pinned the comparison as 4.9-8.3 %, and that band is a share of `run_probe` CALLS.

    This tool reported raw regex hits for three sweeps -- a count against a percentage baseline,
    which is the different-denominators mistake the corpus keeps catching elsewhere.
    """
    # eval_trains=8 puts OTHER tool spans in the tree. Without them tools_all == run_probe calls
    # and the denominator cannot be got wrong: mutation showed dividing by every tool span left the
    # file green.
    _mk_probe(tmp_path, "refr", "t", nodes=[1.0], costs_before=[0.5], costs_after=[0.5], test=1.0,
              eval_trains=8)
    _mk_run_probe(tmp_path, "refr", "t", total=20, with_import=2, with_call=3)
    out = _run(tmp_path)
    line = next(l for l in out.splitlines() if l.strip().startswith("refr ("))
    assert "over 20 run_probe calls" in line, f"the denominator is not stated: {line}"
    assert "10.0% import" in line, f"2 of 20 imports should read 10.0 %: {line}"
    assert "15.0% is_solution" in line, f"3 of 20 calls should read 15.0 %: {line}"
    assert "4.9-8.3" in line, "the baseline the number is meant to be compared against is missing"


def test_a_probe_that_never_called_run_probe_reports_no_rate_rather_than_zero(tmp_path):
    """0 % would say "it had the chance and did not take it"; no calls means no denominator."""
    _mk_probe(tmp_path, "norp", "t", nodes=[1.0], costs_before=[0.5], costs_after=[0.5], test=1.0)
    out = _run(tmp_path)
    line = next(l for l in out.splitlines() if l.strip().startswith("norp ("))
    assert "0.0% import" not in line, (
        f"a probe with zero run_probe calls was given a 0 % rate: {line}"
    )


def test_the_rate_counts_calls_not_occurrences(tmp_path):
    """One call importing the reference five times is one call, not five."""
    _mk_probe(tmp_path, "many", "t", nodes=[1.0], costs_before=[0.5], costs_after=[0.5], test=1.0)
    run = tmp_path / "model-probes" / "many" / "runs" / "t" / "run"
    spans = [json.loads(l) for l in open(run / "spans.jsonl")]
    code = "from reference_t import A\n" * 5
    spans.append({"name": "tool", "start": 3000, "duration_s": 0.1,
                  "attributes": {"tool": "run_probe", "args": {"code": code}}})
    for i in range(9):
        spans.append({"name": "tool", "start": 3010 + i, "duration_s": 0.1,
                      "attributes": {"tool": "run_probe", "args": {"code": "y = 2\n"}}})
    (run / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans))
    out = _run(tmp_path)
    line = next(l for l in out.splitlines() if l.strip().startswith("many ("))
    assert "10.0% import" in line, (
        f"five imports inside ONE call were counted as five calls: {line}"
    )


def _mk_meter(tmp_path, rows):
    d = tmp_path / "meter"
    d.mkdir(parents=True, exist_ok=True)
    (d / "meter.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return d / "meter.jsonl"


def _run_with_meter(root, meter, monkeypatch=None):
    import os
    env = dict(os.environ)
    env["LOOPLAB_METER_LOG"] = str(meter)
    r = subprocess.run([sys.executable, str(TOOL), str(root)],
                       capture_output=True, text=True, timeout=600, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout



def _instrument_block(out: str) -> str:
    """Just the instrument section. Both it and the main table start rows with the probe name, so a
    bare `startswith(name)` picks whichever comes first -- which is the table, and the test then
    asserts about the wrong line."""
    if "NOT on the current instrument" not in out:
        return ""
    return out.split("NOT on the current instrument", 1)[1].split("per-probe detail")[0]


def test_an_unstreamed_probe_is_named_as_being_on_another_instrument(tmp_path):
    """A probe tree says what the run did and nothing about the gateway it did it through.

    accEE, accPde and remEE ran entirely unstreamed; remEE lost 9 calls to the 300 s ceiling, 45
    minutes returning nothing. §73 had built a "controlled pair" on remEE against a streamed run
    before anyone looked.
    """
    _mk_probe(tmp_path, "old", "t", nodes=[1.0], costs_before=[0.5], costs_after=[0.5], test=1.0)
    meter = _mk_meter(tmp_path, [{"arm": "old", "prompt_tokens": 9000, "stream": False,
                                  "status": 200}] * 50
                      + [{"arm": "old", "prompt_tokens": 9000, "stream": False, "status": 504}] * 3)
    out = _run_with_meter(tmp_path, meter)
    assert "NOT on the current instrument" in out, "the instrument section is missing:\n" + out
    line = next(l for l in _instrument_block(out).splitlines() if l.strip().startswith("old "))
    assert "UNSTREAMED" in line, f"an unstreamed probe was not flagged: {line}"
    assert "3 call(s) killed" in line, f"the ceiling kills are not counted: {line}"


def test_a_streamed_probe_is_not_flagged(tmp_path):
    _mk_probe(tmp_path, "cur", "t", nodes=[1.0], costs_before=[0.5], costs_after=[0.5], test=1.0)
    meter = _mk_meter(tmp_path, [{"arm": "cur", "prompt_tokens": 9000, "stream": True,
                                  "status": 200}] * 50)
    out = _run_with_meter(tmp_path, meter)
    assert "NOT on the current instrument" not in out, (
        "a fully streamed probe was reported as being on another instrument:\n" + out
    )


def test_preflights_do_not_make_every_probe_look_mixed(tmp_path):
    """`agents/preflight.py` sets stream=False by design and sends ten tokens."""
    _mk_probe(tmp_path, "pf", "t", nodes=[1.0], costs_before=[0.5], costs_after=[0.5], test=1.0)
    meter = _mk_meter(tmp_path, [{"arm": "pf", "prompt_tokens": 10, "stream": False, "status": 200}]
                      + [{"arm": "pf", "prompt_tokens": 9000, "stream": True, "status": 200}] * 50)
    out = _run_with_meter(tmp_path, meter)
    assert "NOT on the current instrument" not in out, (
        "a 10-token preflight put a fully streamed probe on the other instrument:\n" + out
    )

    # And the COUNT, not just the verdict. Mutation showed that dropping the >1000-token filter
    # left this green: the probe still had streamed calls, so it was still not flagged, and the
    # three preflights it would then have carried were invisible to the assertion. A probe whose
    # ONLY unstreamed traffic is preflights must report zero of them.
    meter2 = _mk_meter(tmp_path / "b",
                       [{"arm": "pf2", "prompt_tokens": 10, "stream": False, "status": 200}] * 3
                       + [{"arm": "pf2", "prompt_tokens": 9000, "stream": False, "status": 200}] * 5)
    _mk_probe(tmp_path, "pf2", "t", nodes=[1.0], costs_before=[0.5], costs_after=[0.5], test=1.0)
    out2 = _run_with_meter(tmp_path, meter2)
    line = next(l for l in _instrument_block(out2).splitlines() if l.strip().startswith("pf2 "))
    assert "5 unstreamed" in line, (
        f"three 10-token preflights were counted as real unstreamed calls: {line}"
    )


def test_a_streamed_probe_that_still_hit_the_ceiling_is_named(tmp_path):
    """Streaming makes the ceiling unreachable in practice; if one is hit anyway, say so."""
    _mk_probe(tmp_path, "odd", "t", nodes=[1.0], costs_before=[0.5], costs_after=[0.5], test=1.0)
    meter = _mk_meter(tmp_path, [{"arm": "odd", "prompt_tokens": 9000, "stream": True,
                                  "status": 200}] * 50
                      + [{"arm": "odd", "prompt_tokens": 9000, "stream": True, "status": 504}])
    out = _run_with_meter(tmp_path, meter)
    line = next(l for l in _instrument_block(out).splitlines() if l.strip().startswith("odd "))
    assert "1 call(s) killed" in line, f"a streamed probe's ceiling kill went unreported: {line}"


def test_no_meter_is_silence_not_a_verdict(tmp_path):
    _mk_probe(tmp_path, "nom", "t", nodes=[1.0], costs_before=[0.5], costs_after=[0.5], test=1.0)
    out = _run_with_meter(tmp_path, tmp_path / "does-not-exist.jsonl")
    assert "NOT on the current instrument" not in out, (
        "with no meter to read, the tool guessed instead of staying quiet:\n" + out
    )
