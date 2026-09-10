"""Two thirds of the standing list's item (c) is stale, and only a measurement could say so.

`cue_reach` resolves chained prompts; reading `attributes.input` directly reports a phase as blind
whenever `input_from` is set, and that mistake has been made by hand twice (31.7 % where the truth
was 99.3 %). Driven 2026-09-08 over the three newest probe trees:

    plan 100 % of spans (7.2 % of spend)   propose 90 %   repropose 92 %
    foresight_rank 0 % (2.7 %)             hyp_prioritize 0 % (1.4 %)

`plan` was closed on 2026-08-31 through the Developer's own note. What stays blind is the foresight
panel, and that is a recorded decision with its own revisit line -- "a few per cent" -- which is why
the check reports the SHARE and not only the reach.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import sweep_claims  # noqa: E402

STUB = '''#!/usr/bin/env python3
import json, sys
payload = {PAYLOAD}
# HOW MANY ROOTS THE CHECK ACTUALLY HANDED OVER, so the sample is testable and not a promise.
payload["logs"] = len([a for a in sys.argv[1:] if not a.startswith("--")])
if "--json" not in sys.argv:
    # The COLUMNS, as the real tool prints them when nobody asks for json. A check that reads these
    # by eye is the §289 mistake, so the stub makes that path available and wrong-shaped on purpose.
    print(f'{len(payload["phases"])} span log(s), pattern /x/')
    print(f'{"phase":22s} {"spans":>6s} {"sees":>6s} {"%":>6s} {"cost":>9s} {"share":>6s}')
    for r in payload["phases"]:
        print(f'{r["phase"]:22s} {r["spans"]:6d} {r["sees"]:6d} {r["reach_pct"]:5.1f}% '
              f'${r["usd"]:8.4f} {r["share_pct"]:5.1f}%')
    raise SystemExit(0)
print(json.dumps(payload))
'''


def _bench(tmp_path, phases) -> str:
    """A tree shaped like the bench, with a `cue_reach.py` that answers a fixed table."""
    root = tmp_path
    tools = root / "looplab" / "benchmarks"
    tools.mkdir(parents=True)
    payload = {"logs": 3, "pattern": "x", "grand_usd": 10.0, "blind_usd": 1.0,
               "phases": [{"phase": p, "spans": 10, "sees": s, "reach_pct": r,
                           "usd": 1.0, "share_pct": sh} for p, s, r, sh in phases]}
    (tools / "cue_reach.py").write_text(STUB.replace("{PAYLOAD}", json.dumps(payload)),
                                        encoding="utf-8")
    for name in ("p1", "p2", "p3", "p4", "p5"):
        (root / "model-probes" / name / "runs").mkdir(parents=True)
    return str(root)


def test_plan_alone_refutes_the_claim(tmp_path):
    bench = _bench(tmp_path, [("plan", 10, 100.0, 7.2), ("foresight_rank", 0, 0.0, 2.7),
                              ("hyp_prioritize", 0, 0.0, 1.4)])
    ok, detail = sweep_claims.check_money_cue_reaches_the_choosers(bench)
    assert not ok, detail
    assert "plan 100 % of spans" in detail, detail
    assert "STILL BLIND: foresight_rank, hyp_prioritize" in detail, detail


def test_the_claim_holds_only_when_all_three_are_blind(tmp_path):
    bench = _bench(tmp_path, [("plan", 0, 0.0, 8.0), ("foresight_rank", 0, 0.0, 2.7),
                              ("hyp_prioritize", 0, 0.0, 1.4)])
    ok, detail = sweep_claims.check_money_cue_reaches_the_choosers(bench)
    assert ok, detail


def test_a_blind_phase_past_its_own_revisit_line_is_named(tmp_path):
    """The decision to leave the foresight panel blind carries a threshold: revisit if either grows
    past a few per cent. A check that reported only "still blind" would keep that promise silently
    and forever."""
    bench = _bench(tmp_path, [("plan", 10, 100.0, 7.0), ("foresight_rank", 0, 0.0, 6.5),
                              ("hyp_prioritize", 0, 0.0, 1.4)])
    _, detail = sweep_claims.check_money_cue_reaches_the_choosers(bench)
    assert "past its own 'a few per cent' revisit line: foresight_rank at 6.5 %" in detail, detail


def test_cue_reach_json_says_the_same_as_its_table():
    """The check reads `--json` because §289 measured what parsing the prose costs. The two outputs
    must not drift: same probes, same numbers."""
    roots = sorted((Path("/var/tmp/looplab-bench/model-probes")).glob("*/runs"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    roots = [str(r.parent) for r in roots if r.parent.name != "_ruler"][:2]
    if not roots:
        # SKIP, not `return`: this anchor reads the LIVE bench corpus, and a box without one
        # must report "not checked" rather than print a green dot for a check that never ran.
        pytest.skip("no probe runs on this box")
    tool = str(BENCH / "cue_reach.py")
    as_json = json.loads(subprocess.run([sys.executable, tool, "--json", *roots],
                                        capture_output=True, text=True, timeout=600).stdout)
    text = subprocess.run([sys.executable, tool, *roots], capture_output=True, text=True,
                          timeout=600).stdout
    from_text = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 6 and parts[3].endswith("%") and parts[4].startswith("$"):
            from_text[parts[0]] = float(parts[3].rstrip("%"))
    assert from_text, text[:300]
    for row in as_json["phases"]:
        assert row["phase"] in from_text, (row, sorted(from_text))
        assert abs(from_text[row["phase"]] - row["reach_pct"]) < 0.1, (row, from_text[row["phase"]])


def test_the_revisit_line_is_judged_on_the_corpus_not_on_a_three_probe_window(tmp_path):
    """§341. Порог, который несёт решение оставить панель предвидения слепой — «пересмотреть, если
    вырастет за несколько процентов», — это утверждение о КОРПУСЕ. Окно из трёх проб не может ни
    провалить его, ни очистить честно: 2026-09-08 три новейшие пробы все оказались `discrete_log`,
    задачей с самой высокой долей этой фазы, окно показало 3.0 %, и проверка объявила порог
    перейдённым. По всем 142 деревьям с этим спаном доля 2.06 % (по пробам: медиана 2.01, p75 2.63,
    максимум 5.56 — 21 из 142 на 3 % и выше). Решение стоит; тревогу сделала выборка."""
    bench = _bench(tmp_path, [("plan", 10, 100.0, 7.0), ("foresight_rank", 0, 0.0, 2.1),
                              ("hyp_prioritize", 0, 0.0, 1.2)])
    _, detail = sweep_claims.check_money_cue_reaches_the_choosers(bench)
    assert "over 5 probe tree(s)" in detail, f"выборка не названа: {detail}"


def test_every_probe_tree_is_handed_to_the_tool(tmp_path):
    """Заглушка сообщает, СКОЛЬКО корней ей передали. Срез `[:3]` виден отсюда, а не из обещания."""
    bench = _bench(tmp_path, [("foresight_rank", 0, 0.0, 2.1)])
    _, detail = sweep_claims.check_money_cue_reaches_the_choosers(bench)
    assert "over 5 probe tree(s)" in detail, f"проверка отдала не все деревья: {detail}"
