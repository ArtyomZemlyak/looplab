"""THE TRIAGE'S PROSE IS A PROPOSAL; `node_repaired` PRESENTED IT AS A DESCRIPTION (doc 53 §3).

`engine/evaluate.py` asks `_triage_crash` what to do with a crashed node, and some three hundred
lines later opens the repair session. Everything the judge authored — `rationale`, `reason_summary`,
`reason_findings` — is therefore written against the CRASH, by an agent that has read logs and code
and has changed nothing. The engine then stamps that text onto the `node_repaired` row beside the
`files` the SESSION produced. Nothing on the row said which half came first, so it reads as one
account of one act, and it can be flatly contradictory.

THE SPECIMEN, and it is the whole corpus: `runs-B/count_riemann_zeta_zeros` node 0 attempt 1 is the
ONLY `node_repaired` row across all twenty arm-B task-arms. Triage prescribed a capitulation —
"replace the whole port with a direct call to `mp.nzeros(t)` … This yields a valid scored submission
(speedup ~1.0)". The session did the opposite: it KEPT the mpmath port and moved the precision work
onto a private `mp.clone()`. The node scored **6.0212**. Both fixtures below are those two files,
unedited, off the event log.

WHAT IS ASSERTED HERE IS THAT THE RECORD SAYS SO — not that the engine stops it. A repair that
overrides its triage on measured evidence is the loop working (this one turned a prescribed ~1.0
into a 6.02), which is the same verdict the build half reached for plan steps. So: record, don't
prevent.

AND WHY THERE IS NO VERDICT COLUMN, which is a measurement and not a preference. The obvious fix is
to re-point `verify_repair` at the shipped bytes. It does not work, twice over, and
`test_no_token_rung_grades_this_prose` pins both halves: `claimed_tokens` extracts NOTHING from that
rationale (`mp.nzeros` and `mp.prec` carry no underscore and no mid-word case, so `_IDENT_RE` — which
exists to refuse bare words — cannot see them, and the shipped verdict is `unstated`), and the
shipped file contains the literal strings `mp.nzeros` AND `mp.prec` in its own docstrings, so a
substring test over source would score the capitulation as DELIVERED.
"""
from __future__ import annotations

import json
from pathlib import Path

from looplab.engine.repair_verify import (claimed_tokens, named_files, repair_attribution,
                                          verify_repair)

_FIX = Path(__file__).parent / "fixtures"
# `node_created.files["solver.py"]` — the port the triage condemned.
_PRESCRIBED_AGAINST = (_FIX / "algotune_repair_triage_prescribed_solver.py.txt").read_text(encoding="utf-8")
# `node_repaired.files["solver.py"]` — what actually shipped and scored 6.0212.
_SHIPPED = (_FIX / "algotune_repair_shipped_solver.py.txt").read_text(encoding="utf-8")
_PROSE = json.loads((_FIX / "algotune_repair_triage_prose.json").read_text(encoding="utf-8"))


def _specimen() -> dict:
    return repair_attribution(
        prose=(_PROSE["rationale"], _PROSE["reason_summary"]),
        prev_files={"solver.py": _PRESCRIBED_AGAINST}, prev_code="",
        files={"solver.py": _SHIPPED}, code="",
        changed=["solver.py"], deleted=[])


# ------------------------------------------------------------------ the fact the row never stated

def test_the_row_says_the_prose_was_written_before_the_session():
    """The one column that makes every other word on the row readable."""
    assert _specimen()["prose_authored"] == "before_repair"


def test_the_divergence_is_legible_as_a_number():
    """Triage said "replace the whole port"; the repair kept four fifths of it. That gap is the
    record's whole job here, and it is byte-anchored — no reading of the prose is involved."""
    wrote = _specimen()["wrote"]
    assert wrote == [{"path": "solver.py", "kept": 0.725}], wrote


def test_the_prescription_is_recorded_beside_what_ran():
    """`named` is what the prose points at, `unnamed` what the session touched and the prose never
    mentioned. On the specimen the two agree at file granularity — which is exactly why the `kept`
    number above, and not a file-set comparison, is what carries the divergence."""
    rec = _specimen()
    assert "solver.py" in rec["named"]
    assert rec["unnamed"] == []


def test_shipped_files_no_step_of_the_repair_touched_are_named():
    """`node_repaired.files` is the Developer's whole WORKING SET, not its delta, so a reader who
    takes the row's prose to describe its `files` is wrong about these paths twice over. Same field,
    same name and same argument as the build half's `plan_step_attribution`."""
    rec = repair_attribution(
        prose=("fix solver.py",), prev_files={"solver.py": "a", "conf.yaml": "k: 1"}, prev_code="",
        files={"solver.py": "b", "conf.yaml": "k: 1"}, code="", changed=["solver.py"], deleted=[])
    assert rec["unattributed"] == ["conf.yaml"]
    assert [r["path"] for r in rec["wrote"]] == ["solver.py"]


def test_a_file_the_repair_created_is_new_not_a_similarity():
    """`kept` compares against a pre-image. There is none, and reporting 0.0 would read as "the
    repair replaced everything" where the truth is "there was nothing here"."""
    rec = repair_attribution(prose=(), prev_files={}, prev_code="", files={"new.py": "x"},
                             code="", changed=["new.py"], deleted=[])
    assert rec["wrote"] == [{"path": "new.py", "new": True}]


def test_the_whole_file_artifact_is_attributed_under_its_one_spelling():
    """A non-repo task ships `code`, which has no path; it is reported by the same name
    `changed_region` uses so a reader meets one spelling."""
    rec = repair_attribution(prose=(), prev_files={}, prev_code="\n".join("x" * 10 for _ in range(10)),
                             files={}, code="\n".join(["x" * 10] * 9 + ["y" * 10]),
                             changed=[], deleted=[])
    assert rec["wrote"] == [{"path": "<whole-file solution>", "kept": 0.9}]


# ------------------------------------------------------------------ why there is no verdict column

def test_no_token_rung_grades_this_prose():
    """Both halves of the measurement that decided this records rather than judges.

    One: the shipped `verify_repair` cannot see the claim at all. Two: the escape hatch — matching
    the prescribed call against the shipped source — scores the capitulation as DELIVERED, because
    the file that did NOT capitulate says `mp.nzeros` and `mp.prec` in its own docstrings.
    """
    assert claimed_tokens(_PROSE["rationale"]) == ()
    assert verify_repair(_PROSE["rationale"], changed=["solver.py"], code_changed=False,
                         region="solver.py").verdict == "unstated"
    assert _PROSE["verified"] == "unstated" and _PROSE["unmet"] == []   # what the run recorded
    assert "mp.nzeros" in _SHIPPED and "mp.prec" in _SHIPPED
    # …and neither string is in the code. Both live in prose the repair wrote about itself.
    code_only = "\n".join(ln for ln in _SHIPPED.splitlines()
                          if not ln.strip().startswith(('"""', "'''", "#", "fallback", "back")))
    assert "mp.nzeros(" not in code_only


def test_the_record_makes_no_verdict():
    """No `superseded`, no boolean, nothing a reader could mistake for the engine having an opinion
    about a repair that out-scored its own triage by 6x."""
    rec = _specimen()
    assert set(rec) == {"prose_authored", "wrote", "deleted", "unattributed", "named", "unnamed"}
    assert not any(isinstance(v, bool) for v in rec.values())


# ------------------------------------------------------------------ total on hostile input

def test_it_is_total_and_bounded():
    """It runs inside the attempt loop on whatever the Developer wrote: every input is an answer."""
    assert repair_attribution(prose=None, prev_files=None, prev_code=None, files=None, code=None,
                              changed=None, deleted=None)["wrote"] == []
    big = {f"f{i}.py": "z\n" * 100_000 for i in range(40)}
    rec = repair_attribution(prose=("x" * 100_000,), prev_files=big, prev_code="",
                             files={k: v + "\nq" for k, v in big.items()}, code="",
                             changed=sorted(big), deleted=sorted(f"d{i}.txt" for i in range(40)))
    assert len(rec["wrote"]) == 12 and len(rec["deleted"]) == 24


def test_named_files_reads_paths_and_nothing_else():
    """A path carries an extension; that is the one token in a rationale unambiguously about the
    tree. Bare words and identifiers are prose here and stay out of it."""
    assert named_files("rewrite solver.py and the mp.nzeros call, see conf.yaml") == (
        "solver.py", "conf.yaml")


# ------------------------------------------------------------------ it reaches the DURABLE row

def test_the_engine_stamps_it_on_node_repaired(tmp_path):
    """Driven, not grepped: a real inline repair must land the column on the durable event.

    Without this the wiring in `engine/evaluate.py` could be deleted and every test above would
    still pass on a function nothing calls — which is precisely the shape of the defect (a
    reconciliation that exists and never reaches the record).
    """
    import anyio

    from looplab.adapters.toytask import ToyTask
    from looplab.core.models import Idea
    from looplab.engine.orchestrator import Engine
    from looplab.events.eventstore import EventStore
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree

    bad = "import definitely_not_a_real_module_zzz\n"
    good = "import json; print(json.dumps({'metric': 0.1}))\n"

    class _Stub:
        def propose(self, state, parent):
            return Idea(operator="x", params={"x": 1.0, "y": 1.0})

    class _CrashThenFix:
        def implement(self, idea):
            return bad

        def repair(self, idea, code, error):
            return good

    run_dir = tmp_path / "run"
    eng = Engine(run_dir, task=ToyTask.load(Path(__file__).resolve().parents[1] / "examples"
                                           / "toy_task.json"),
                 researcher=_Stub(), developer=_CrashThenFix(), sandbox=SubprocessSandbox(),
                 policy=GreedyTree(n_seeds=1, max_nodes=2, debug_depth=1),
                 inline_repair=True, inline_repair_attempts=1, auto_install_deps=False)
    anyio.run(eng.run)

    rows = [e.data for e in EventStore(run_dir / "events.jsonl").read_all()
            if e.type == "node_repaired"]
    assert rows, "expected an inline node_repaired event"
    rec = rows[0]["attribution"]
    assert rec["prose_authored"] == "before_repair"
    # A whole-file task: the artifact has no path, and the rule triage rewrote it end to end.
    assert [r["path"] for r in rec["wrote"]] == ["<whole-file solution>"]
