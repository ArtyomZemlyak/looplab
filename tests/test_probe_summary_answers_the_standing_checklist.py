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


def _cell(out: str, probe: str, column: str) -> str:
    """One cell of the main table, addressed BY COLUMN NAME.

    The tests used to index from the end (`row.split()[-2]`), and adding a `build` column silently
    moved every one of those assertions onto its neighbour. A table read positionally is a table
    that breaks the next time it grows.
    """
    lines = out.splitlines()
    head = next(l for l in lines if l.startswith("probe "))
    row = next(l for l in lines if l.split() and l.split()[0] == probe)
    return row.split()[head.split().index(column)]


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
    # AGED deliberately. A recovery is only offered for a run that has STOPPED -- extracting a
    # champion from a live one hands back an intermediate dressed as final -- and a fixture written
    # milliseconds ago is the liveliest run on the box.
    import os
    import time
    ev = p / "runs" / "t" / "run" / "events.jsonl"
    old_t = time.time() - 7200
    os.utime(ev, (old_t, old_t))
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
    assert _cell(out, "slow", "->build") == "30m", (
        "time to the first build step is not reported:\n" + out)


def test_a_run_that_has_not_built_yet_shows_a_dash_not_a_zero(tmp_path):
    """Zero minutes would read as "built instantly", which is the opposite of the truth."""
    _mk_probe(tmp_path, "nobuild", "t", nodes=[], costs_before=[0.3], costs_after=[])
    out = _run(tmp_path)
    assert _cell(out, "nobuild", "->build") == "-", (
        "a run with no build step yet was reported as having built at minute zero:\n" + out)


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
    assert _cell(out, "prop", "->build") == "-", (
        "propose/deep_research/plan were counted as reaching a build step:\n" + out)


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


def test_how_long_the_build_itself_took_is_reported(tmp_path):
    """`->build` and `build` together answer "stuck run, or slow task?"; neither does alone.

    Measured 2026-09-01 over 19 runs, first plan_step to first node: edge_expansion 5-14 min,
    pde_heat1d 25-44, discrete_log 23-54, with no point between 14 and 23. A discrete_log probe 42
    minutes into a build with no node is inside its band; the same reading on edge_expansion would
    be three times the worst ever seen.
    """
    p = _mk_probe(tmp_path, "bd", "t", nodes=[], costs_before=[0.5], costs_after=[], test=None)
    run = p / "runs" / "t" / "run"
    spans = [json.loads(l) for l in open(run / "spans.jsonl")]
    events = [json.loads(l) for l in open(run / "events.jsonl")]
    t0 = min(float(x["start"]) for x in spans)
    spans.append({"name": "generation", "start": t0 + 60, "duration_s": 1.0,
                  "attributes": {"cost": 0.01, "phase": "plan_step"}})
    events.append({"type": "node_evaluated", "ts": t0 + 60 + 1500, "data": {"metric": 5.0}})
    (run / "spans.jsonl").write_text("".join(json.dumps(x) + "\n" for x in spans))
    (run / "events.jsonl").write_text("".join(json.dumps(x) + "\n" for x in events))
    out = _run(tmp_path)
    assert _cell(out, "bd", "build") == "25m", "build duration is not reported:\n" + out


def test_a_build_still_running_shows_a_dash_not_a_number(tmp_path):
    """A run mid-build has no duration yet; printing one would invent a finished build."""
    p = _mk_probe(tmp_path, "bd2", "t", nodes=[], costs_before=[0.5], costs_after=[])
    run = p / "runs" / "t" / "run"
    spans = [json.loads(l) for l in open(run / "spans.jsonl")]
    t0 = min(float(x["start"]) for x in spans)
    spans.append({"name": "generation", "start": t0 + 60, "duration_s": 1.0,
                  "attributes": {"cost": 0.01, "phase": "plan_step"}})
    (run / "spans.jsonl").write_text("".join(json.dumps(x) + "\n" for x in spans))
    out = _run(tmp_path)
    assert _cell(out, "bd2", "build") == "-", (
        "a build with no node yet was given a duration:\n" + out)


def test_a_node_that_predates_the_first_build_step_is_not_a_negative_build(tmp_path):
    """Ordering is not guaranteed; a negative duration would be printed as fact."""
    p = _mk_probe(tmp_path, "bd3", "t", nodes=[], costs_before=[0.5], costs_after=[])
    run = p / "runs" / "t" / "run"
    spans = [json.loads(l) for l in open(run / "spans.jsonl")]
    events = [json.loads(l) for l in open(run / "events.jsonl")]
    t0 = min(float(x["start"]) for x in spans)
    events.append({"type": "node_evaluated", "ts": t0 + 10, "data": {"metric": 5.0}})
    spans.append({"name": "generation", "start": t0 + 600, "duration_s": 1.0,
                  "attributes": {"cost": 0.01, "phase": "plan_step"}})
    (run / "spans.jsonl").write_text("".join(json.dumps(x) + "\n" for x in spans))
    (run / "events.jsonl").write_text("".join(json.dumps(x) + "\n" for x in events))
    out = _run(tmp_path)
    assert _cell(out, "bd3", "build") == "-", (
        "a node before the first build step produced a duration:\n" + out)


def test_the_build_duration_comment_claims_no_gap_it_does_not_have():
    """It claimed one for thirty minutes, and the next run landed in it.

    On 19 runs the comment read "nothing between 14 and 23"; remPde8 then built in 14.0 minutes on
    pde_heat1d. A guide that names a gap invites the next reader to treat a point inside it as a
    fault, which is precisely the investigation this column exists to prevent.
    """
    src = (REPO / "benchmarks" / "probe_summary.py").read_text()
    i = src.index("first `plan_step` to first `node_evaluated`")
    block = src[i:i + 2000]
    assert "No point falls between" not in block, "the gap claim is back"
    assert "ROUGH, NOT A BAND" in block, (
        "the comment no longer warns that these are observed ranges rather than bounds"
    )


def test_every_unscored_probe_is_listed_even_with_no_quotable_reason(tmp_path):
    """The section was gated on six hardcoded phrases and matched none of five real probes.

    `remDL` -- dead, $0.1292, zero nodes -- got no line at all, though its run.log says
    `answered HTTP 504 (overloaded) -- waiting 30s before attempt 7 of 9`. A section written
    because "the diagnosis is in a file nobody reads" was silent because the diagnosis was phrased
    differently from the phrases it knew.
    """
    p = _mk_probe(tmp_path, "quiet", "t", nodes=[5.0], costs_before=[0.5], costs_after=[0.5])
    (p / "probe.log").write_text("[12:00:00] started\n[13:00:00] done\n")
    out = _run(tmp_path)
    block = out.split("probes with NO test score", 1)[-1].split("per-probe detail")[0]
    assert "quiet" in block, (
        "an unscored probe with no matching phrase was dropped from the section entirely:\n" + out
    )
    assert "no stated reason" in block, "it is listed but nothing says why it has no reason"


def test_a_live_run_is_marked_and_not_told_its_budget_is_spent(tmp_path):
    """Extracting a champion from a running probe hands back an intermediate dressed as final."""
    p = _mk_probe(tmp_path, "live1", "t", nodes=[5.0], costs_before=[0.5], costs_after=[0.5])
    import os
    import time
    ev = p / "runs" / "t" / "run" / "events.jsonl"
    os.utime(ev, (time.time(), time.time()))
    out = _run(tmp_path)
    block = out.split("probes with NO test score", 1)[-1].split("per-probe detail")[0]
    line = next(l for l in block.splitlines() if l.strip().startswith("live1"))
    assert "STILL RUNNING" in line, f"a probe writing right now was not marked as live: {line}"
    assert "recoverable for $0" not in block, (
        "a live run was told its budget is already spent and offered a recovery:\n" + block
    )


def test_a_killed_call_still_counts_as_a_call(tmp_path):
    """All 21 status-504 rows carry `prompt_tokens: null`; the >1000 filter dropped them.

    A probe whose real calls ALL died read "0 unstreamed / 0 streamed ... streamed, but hit the
    gateway ceiling" -- the verdict backwards.
    """
    _mk_probe(tmp_path, "dead", "t", nodes=[1.0], costs_before=[0.5], costs_after=[0.5], test=1.0)
    meter = _mk_meter(tmp_path, [{"arm": "dead", "prompt_tokens": None, "stream": False,
                                  "status": 504}] * 6
                      + [{"arm": "dead", "prompt_tokens": 10, "stream": False, "status": 200}] * 2)
    out = _run_with_meter(tmp_path, meter)
    line = next(l for l in _instrument_block(out).splitlines() if l.strip().startswith("dead "))
    assert "6 unstreamed" in line, f"the killed calls were not counted at all: {line}"
    assert "UNSTREAMED" in line, (
        f"a probe whose every real call died was called streamed: {line}"
    )


def test_a_row_with_no_stream_field_is_unknown_not_unstreamed(tmp_path):
    _mk_probe(tmp_path, "nokey", "t", nodes=[1.0], costs_before=[0.5], costs_after=[0.5], test=1.0)
    meter = _mk_meter(tmp_path, [{"arm": "nokey", "prompt_tokens": 9000, "status": 200}] * 40)
    out = _run_with_meter(tmp_path, meter)
    assert "NOT on the current instrument" not in out, (
        "rows with no `stream` field were read as unstreamed, inventing a verdict from a gap in "
        "the data:\n" + out
    )


def test_the_champion_ledger_counts_ties_out_of_the_sign_test(tmp_path):
    """docs/56 §84 was hand-computed and `remPde10` finished thirty minutes later and moved it.

    The ledger exists so the figure is never hand-carried again, and the one thing it must not get
    wrong is the DENOMINATOR: a tie -- last node WAS the best -- is not evidence for the rule, and
    counting ties among the non-ties halves the p for free. Four runs, two of which moved and two of
    which tied, must report 2 of 4 and p = 1/4, not 1/16.
    """
    _mk_probe(tmp_path, "moved1", "t1", nodes=[10.0, 2.0], costs_before=[0.1], costs_after=[0.01])
    _mk_probe(tmp_path, "moved2", "t1", nodes=[8.0, 4.0], costs_before=[0.1], costs_after=[0.01])
    _mk_probe(tmp_path, "tied1", "t1", nodes=[5.0, 5.0], costs_before=[0.1], costs_after=[0.01])
    _mk_probe(tmp_path, "tied2", "t1", nodes=[3.0, 3.0], costs_before=[0.1], costs_after=[0.01])
    out = _run(tmp_path)
    assert "2 of 4 runs ended on a node that was NOT their best" in out, out
    assert "(2 ties, 0 ended on a ZERO)" in out, out
    assert "one-sided p = 0.25 = 1/4" in out, out


def test_a_single_node_run_is_not_in_the_ledger_at_all(tmp_path):
    """A run with one node cannot demonstrate the rule either way, and counting it as a tie would
    quietly pad the denominator with runs that had no second node to lose."""
    _mk_probe(tmp_path, "moved1", "t1", nodes=[10.0, 2.0], costs_before=[0.1], costs_after=[0.01])
    _mk_probe(tmp_path, "single", "t1", nodes=[7.0], costs_before=[0.1], costs_after=[0.01])
    out = _run(tmp_path)
    assert "1 of 1 runs ended on a node that was NOT their best" in out, out
    assert "single" not in out.split("the champion rule, over")[1].split("per-probe detail")[0]


def test_a_last_node_of_zero_does_not_divide_by_zero(tmp_path):
    """Two of eighteen real runs finished by scoring 0.0, which is the largest possible gap and the
    one arithmetic that most naturally crashes a ratio column."""
    _mk_probe(tmp_path, "zeroed", "t1", nodes=[9.0, 0.0], costs_before=[0.1], costs_after=[0.01])
    out = _run(tmp_path)
    assert "infx" in out, out
    assert "last node scored ZERO" in out, out
    assert "(0 ties, 1 ended on a ZERO)" in out, out


def test_the_ledger_says_what_it_does_not_measure(tmp_path):
    """The protective value of the rule and the value of STATING it are different claims, and the
    p is small enough that a reader who merges them would take the card clause as proven."""
    _mk_probe(tmp_path, "moved1", "t1", nodes=[10.0, 2.0], costs_before=[0.1], costs_after=[0.01])
    out = _run(tmp_path)
    assert "PROTECTIVE value" in out
    assert "--no-unteachable-rules" in out


def _instrument(root, name, *, args="", sha=""):
    """The record a probe writes about the card it was given."""
    p = root / "model-probes" / name
    p.mkdir(parents=True, exist_ok=True)
    lines = ["probe:          " + name]
    if args or sha:
        lines.append("card_args:      " + (args or "(none -- the shipped card)"))
    if sha:
        lines.append("card_sha256:    " + sha)
    (p / "INSTRUMENT.txt").write_text("\n".join(lines) + "\n")


def test_one_arm_is_one_row_even_when_the_record_gained_a_field_mid_arm(tmp_path):
    """Keyed on the hash, four probes of ONE arm split into two rows on 2026-09-01.

    remEEctl1 and remEEctl2 launched before `card_sha256` was added to INSTRUMENT.txt; remEEctl3 and
    remEEctl4 launched after. Same flags, same card, four dollars -- and the summary reported them as
    two separate arms because the INSTRUMENT gained a field between the second probe and the third.
    An instrument that improves mid-arm must not partition the arm it is measuring. The flags are the
    key; the hash is evidence about the key.
    """
    for i, sha in ((1, ""), (2, ""), (3, "d20e9c0e0b3eb26f"), (4, "d20e9c0e0b3eb26f")):
        name = f"ctl{i}"
        _mk_probe(tmp_path, name, "edge_expansion", nodes=[10.0 + i],
                  costs_before=[0.1 * i], costs_after=[0.01])
        _instrument(tmp_path, name, args="--no-unteachable-rules", sha=sha)
    _mk_probe(tmp_path, "shipped1", "edge_expansion", nodes=[9.0],
              costs_before=[0.05], costs_after=[0.01])
    out = _run(tmp_path)
    block = out.split("spend before the FIRST evaluated node")[1].split("the champion rule")[0]
    rows = [ln for ln in block.splitlines() if "--no-unteachable-rules" in ln]
    assert len(rows) == 1, "the arm is split across %d rows:\n%s" % (len(rows), block)
    assert "n= 4" in rows[0], rows[0]


def test_the_same_flags_over_two_different_card_TEXTS_is_reported(tmp_path):
    """The flag column cannot see a reworded clause; the hash can, and must say so.

    Pooling two card texts under one set of flags is the confound `card_sha256` exists to catch --
    the same failure as the split above, wearing the other hat.
    """
    for i, sha in ((1, "aaaaaaaaaaaaaaaa"), (2, "bbbbbbbbbbbbbbbb")):
        name = f"ctl{i}"
        _mk_probe(tmp_path, name, "edge_expansion", nodes=[10.0],
                  costs_before=[0.1], costs_after=[0.01])
        _instrument(tmp_path, name, args="--no-unteachable-rules", sha=sha)
    _mk_probe(tmp_path, "shipped1", "edge_expansion", nodes=[9.0],
              costs_before=[0.05], costs_after=[0.01])
    out = _run(tmp_path)
    assert "pools 2 DIFFERENT card texts" in out, out
    assert "aaaaaaaaaaaa" in out and "bbbbbbbbbbbb" in out, out


def test_a_run_with_no_node_is_NAMED_and_not_silently_dropped(tmp_path):
    """Spend-before-first-node can only be computed for a run that reached one, so the arm that
    fails to evaluate leaves the table instead of landing at the far end of it. That censors the
    comparison toward whichever arm evaluates least, which is the outcome being measured."""
    _mk_probe(tmp_path, "ctl1", "edge_expansion", nodes=[10.0], costs_before=[0.5], costs_after=[0.01])
    _instrument(tmp_path, "ctl1", args="--no-unteachable-rules")
    _mk_probe(tmp_path, "ctl2", "edge_expansion", nodes=[], costs_before=[0.9], costs_after=[])
    _instrument(tmp_path, "ctl2", args="--no-unteachable-rules")
    _mk_probe(tmp_path, "shipped1", "edge_expansion", nodes=[9.0], costs_before=[0.05], costs_after=[0.01])
    out = _run(tmp_path)
    assert "ctl2" in out.split("spend before the FIRST")[1].split("the champion rule")[0], out
    assert "censoring this row" in out, out


def test_before_usd_is_absolute_and_does_not_move_as_the_run_continues(tmp_path):
    """`before_pct` is 100 % at the first node and shrinks all run; the dollars do not. An arm
    readable in flight needs the figure that is fixed once the first node lands."""
    _mk_probe(tmp_path, "early", "edge_expansion", nodes=[5.0],
              costs_before=[0.20], costs_after=[0.80])
    _mk_probe(tmp_path, "young", "edge_expansion", nodes=[5.0],
              costs_before=[0.20], costs_after=[])
    _instrument(tmp_path, "early", args="--no-unteachable-rules")
    _instrument(tmp_path, "young")
    out = _run(tmp_path)
    block = out.split("spend before the FIRST")[1].split("the champion rule")[0]
    assert block.count("$0.2000") >= 2, (
        "the same $0.20 before the first node must read the same for a finished run and a young "
        "one; it does not:\n" + block
    )


def test_time_to_first_build_is_reported_beside_the_dollars(tmp_path):
    """The same "measure early" quantity in minutes, and LESS censored than the dollars.

    A run that has started building has a build time even if it never evaluates anything, so the
    arm that fails to evaluate still appears here instead of vanishing. Printed under the same row,
    not as a second heading: one construct in two units is not two confirmations, and separating
    them invites reading two p-values as independent.
    """
    def _with_build(name, args, minutes):
        probe = _mk_probe(tmp_path, name, "edge_expansion", nodes=[10.0],
                          costs_before=[0.5], costs_after=[0.01])
        _instrument(tmp_path, name, args=args)
        run = probe / "runs" / "edge_expansion" / "run"
        spans = [json.loads(l) for l in open(run / "spans.jsonl")]
        t0 = min(float(x["start"]) for x in spans)
        spans.append({"name": "generation", "start": t0 + minutes * 60, "duration_s": 1.0,
                      "attributes": {"cost": 0.01, "phase": "plan_step"}})
        (run / "spans.jsonl").write_text("".join(json.dumps(x) + "\n" for x in spans))

    _with_build("ctl1", "--no-unteachable-rules", 50)
    _with_build("shipped1", "", 20)
    out = _run(tmp_path)
    block = out.split("spend before the FIRST")[1].split("the champion rule")[0]
    assert block.count("to first build:") == 2, block
    assert "median  50.0m" in block and "median  20.0m" in block, block
    # minutes, rounded — an unrounded float here is how "range 47.02592163880666m" shipped once
    # Only the BUILD rows: the dollar rows above carry four decimals on purpose, and a regex loose
    # enough to catch them reported an unrounded-minutes failure against money.
    import re
    for line in block.splitlines():
        if "to first build:" not in line:
            continue
        m = re.search(r"range (\S+?)-(\S+?)m", line)
        assert m, line
        for v in m.groups():
            assert "." not in v, f"unrounded minutes in the range: {v!r} — {line}"


def test_a_zero_byte_final_json_is_a_destroyed_score_not_an_unfinished_run(tmp_path):
    """`remEEctl1` scored 35.0981, printed it, and had its final.json truncated to zero bytes.

    The run_probe.sh offset hazard (dcdf1f29) resumed the shell at a stale offset after the file had
    grown under it, re-parsed the scoring block, and applied its `> "$OUT/final.json"` redirect to
    nothing. The summary reported "STILL RUNNING (no stated reason)" -- a finished, fully paid probe
    filed under "not done yet", which is exactly how a spent dollar goes unnoticed. The two states
    need opposite actions: one is waited for, the other is recovered.
    """
    probe = _mk_probe(tmp_path, "wiped", "edge_expansion", nodes=[10.0],
                      costs_before=[0.5], costs_after=[0.01])
    (probe / "final.json").write_text("")
    (probe / "champion_solver.py").write_text("# preserved\n")
    out = _run(tmp_path)
    assert "ZERO BYTES" in out, out
    assert "re-score it" in out, out
    assert "STILL RUNNING" not in out.split("wiped")[1][:200], out


def test_a_zero_byte_final_json_with_no_champion_says_so(tmp_path):
    """Recoverable and unrecoverable must not read the same: without the champion there is nothing
    to re-score, and the honest report is that the number is gone."""
    probe = _mk_probe(tmp_path, "gone", "edge_expansion", nodes=[10.0],
                      costs_before=[0.5], costs_after=[0.01])
    (probe / "final.json").write_text("")
    out = _run(tmp_path)
    assert "champion is gone too" in out, out


def test_a_probe_with_a_champion_is_not_called_STILL_RUNNING(tmp_path):
    """`remEEctl2` finished at 15:56 and the summary said "STILL RUNNING final.json is ZERO BYTES".

    The two halves contradict each other in one sentence: a destroyed score is a thing that already
    happened. The label was keyed on the age of events.jsonl, which says only that something
    happened recently -- and a probe that has extracted a champion has by definition stopped
    producing nodes. Reading it as running is what sends an operator to wait instead of to recover.
    """
    probe = _mk_probe(tmp_path, "done", "edge_expansion", nodes=[10.0],
                      costs_before=[0.5], costs_after=[0.01])
    (probe / "final.json").write_text("")
    (probe / "champion_solver.py").write_text("# preserved\n")
    out = _run(tmp_path)
    # From the UNSCORED section, not the main table: the table's first column is the probe name too,
    # and matching there picked up a row that carries neither label. Selecting the wrong block is
    # how a test asserts something true about a line nobody was arguing over.
    block = out.split("probes with NO test score")[1].split("\n\n")[0]
    line = [l for l in block.splitlines() if l.strip().startswith("done")]
    assert line, block
    assert "STILL RUNNING" not in line[0], line[0]
    assert "ZERO BYTES" in line[0], line[0]


def test_a_probe_with_no_champion_and_a_fresh_log_still_reads_as_running(tmp_path):
    """The label must not become useless in the other direction: a live probe has no champion yet
    and its freshness is the only signal there is."""
    _mk_probe(tmp_path, "live", "edge_expansion", nodes=[], costs_before=[0.2], costs_after=[])
    out = _run(tmp_path)
    block = out.split("probes with NO test score")[1].split("\n\n")[0]
    line = [l for l in block.splitlines() if l.strip().startswith("live")]
    assert line and "STILL RUNNING" in line[0], block


def test_after_pct_is_marked_while_it_is_still_accumulating(tmp_path):
    """`after%` means two different things and the table printed them identically.

    For a FINISHED probe it is waste: money spent after the last node it will ever evaluate -- the
    §75 pattern, and accEE's 41 % is a real instance of it. For a RUNNING one it is only "time since
    the last node", which grows until the next node lands and then collapses. On 2026-09-01 the live
    remEEctl5 showed 47 % directly beneath accEE's 41 %, inviting a reader to take them for the same
    thing.
    """
    _mk_probe(tmp_path, "done", "t", nodes=[5.0], costs_before=[0.2], costs_after=[0.8], test=5.0)
    (tmp_path / "model-probes" / "done" / "champion_solver.py").write_text("# ok\n")
    _mk_probe(tmp_path, "live", "t", nodes=[5.0], costs_before=[0.2], costs_after=[0.8])
    out = _run(tmp_path)
    done = [l for l in out.splitlines() if l.startswith("done")][0]
    live = [l for l in out.splitlines() if l.startswith("live")][0]
    assert "80%+" in live, live
    assert "80%+" not in done and "80%" in done, done
    assert "STILL ACCUMULATING" in out, out


def test_the_legend_is_absent_when_every_probe_has_finished(tmp_path):
    """A legend for a marker nothing carries teaches a reader to skip legends."""
    _mk_probe(tmp_path, "done", "t", nodes=[5.0], costs_before=[0.2], costs_after=[0.8], test=5.0)
    (tmp_path / "model-probes" / "done" / "champion_solver.py").write_text("# ok\n")
    out = _run(tmp_path)
    assert "STILL ACCUMULATING" not in out, out
