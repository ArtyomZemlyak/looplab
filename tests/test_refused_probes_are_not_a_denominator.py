"""A probe the cap refused ran nothing, so it cannot be a denominator for "did this run read the
reference".

`developer_probe_max_calls` (§190) makes `run_probe` return "run_probe refused: ..." once a run hits
its cap, and that refusal is still a `run_probe` tool span. `probe_summary` counted them, so `capA2`
— a treated probe with 12 executed calls and 7 refusals — reported its reference use over 19. The
§69.1 band it is compared against (4.9–8.3 %) was measured on runs where `refused` was zero, so the
dilution lands entirely on the treatment side of the live arm: two rates over two different
denominators, printed as if they were one.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import probe_summary  # noqa: E402

REFUSAL = ("(run_probe refused: this run has already made 12 probes, the cap set for this run.)")


def _probe(tmp_path: Path, executed: int, refused: int, importing: int) -> Path:
    """A run with `executed` real probes, `importing` of which import the reference."""
    spans = []
    for i in range(executed):
        body = "from reference_edge_expansion import solve" if i < importing else "print(1)"
        spans.append({"kind": "tool", "name": "tool", "start": float(i), "duration_s": 1.0,
                      "attributes": {"tool": "run_probe", "phase": "plan_step",
                                     "input": json.dumps({"code": body}), "output": "ok"}})
    for j in range(refused):
        spans.append({"kind": "tool", "name": "tool", "start": 100.0 + j, "duration_s": 0.1,
                      "attributes": {"tool": "run_probe", "phase": "plan_step",
                                     "input": json.dumps({"code": "print(2)"}), "output": REFUSAL}})
    spans.append({"kind": "generation", "name": "generation", "start": 200.0, "duration_s": 1.0,
                  "attributes": {"phase": "plan_step", "cost": "1.0"}})
    d = tmp_path / "capX" / "runs" / "edge_expansion" / "run"
    d.mkdir(parents=True)
    (d / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")
    (d / "events.jsonl").write_text(
        json.dumps({"type": "node_evaluated", "ts": 150.0,
                    "data": {"node_id": 0, "metric": 200.0}}) + "\n", encoding="utf-8")
    return d


def test_the_rate_is_over_executed_probes_not_over_spans(tmp_path):
    got = probe_summary.summarise(_probe(tmp_path, executed=12, refused=7, importing=3))
    assert got["run_probe"] == 12, "refusals crept back into the denominator"
    assert got["run_probe_refused"] == 7
    assert abs(got["ref_pct"] - 25.0) < 1e-9, (
        f"3 of 12 executed probes is 25.0 %, not {got['ref_pct']:.1f} % -- with the 7 refusals in "
        "the denominator it reads 15.8 %, which is what made a capped run look like it consulted "
        "the reference less than an uncapped one")


def test_an_uncapped_run_is_unchanged(tmp_path):
    """The fix must not move the corpus the §69.1 band was measured on."""
    got = probe_summary.summarise(_probe(tmp_path, executed=20, refused=0, importing=1))
    assert got["run_probe"] == 20 and got["run_probe_refused"] == 0
    assert abs(got["ref_pct"] - 5.0) < 1e-9, got["ref_pct"]


def test_a_run_that_was_refused_every_time_has_no_rate_at_all(tmp_path):
    """Not 0 %. Zero executed probes is no evidence about reference use, and 0 % is evidence."""
    got = probe_summary.summarise(_probe(tmp_path, executed=0, refused=5, importing=0))
    assert got["run_probe"] == 0 and got["run_probe_refused"] == 5
    assert got["ref_pct"] is None, (
        "an all-refused run reported a rate; 0 % would put it below the §69.1 floor on no data")


UNKNOWN = "(unknown tool: run_probe; available here: arxiv_search, concept_card (+36 more))"


def test_a_phase_that_does_not_offer_the_tool_is_not_a_denominator_either(tmp_path):
    """The second way a `run_probe` span ran nothing. `run_probe` is not available in every phase,
    and a call in one that does not offer it returns `(unknown tool: run_probe; …)` in 2 ms. Four
    such spans exist in the corpus, and one of them made `capA6` read as 13 executed probes under a
    cap of 12 — so this counter can produce a number that its own cap forbids."""
    d = _probe(tmp_path, executed=12, refused=4, importing=3)
    with open(d / "spans.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "tool", "name": "tool", "attributes": {
            "tool": "run_probe", "phase": "propose",
            "input": json.dumps({"argument": "import time"}), "output": UNKNOWN}}) + "\n")
    got = probe_summary.summarise(d)
    assert got["run_probe"] == 12, (
        f'{got["run_probe"]} executed probes under a cap of 12; an unknown-tool span was counted')
    assert abs(got["ref_pct"] - 25.0) < 1e-9, got["ref_pct"]
