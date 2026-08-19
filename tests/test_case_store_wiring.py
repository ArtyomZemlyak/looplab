"""Which case store the engine actually uses (doc 25 EM-11), and what reads a case back.

Two classes claimed the same I19/ADR-10 role in their docstrings — `CaseLibrary` (vector-backed,
Memora-capable) and `JsonlCaseLibrary` (on-disk JSONL) — and only one of them is reachable from a
run. Resolving that used to require grepping for constructors. The docstrings say it now; this keeps
them honest, in both directions.

The second half of the file pins the READ side, which is what the operator-facing labels claim.

**That half was RE-POINTED on 2026-08-19 and the contract it pins is the opposite one.** Until then
a case was injected into nothing: `store_case` wrote one row per finished run and
`JsonlCaseLibrary.search()` / `.all()` had no production call sites at all, so the file's only
reader was `KnowledgeTools`, which embeds a case into the `kb` index where it must first win a
top-3 semantic ranking against every knowledge note. `docs/guide/memory.md` and the Memory panel
both said so, and the test below asserted it.

What changed is that `engine/lessons_priors.py::_scan_prior_context` — the loader that already
reads the exact-task meta-note out of the file NEXT DOOR, under the same `(task_id, direction)` key
and the same `LessonScope` — now reads the case too. The argument is a measurement and it is why
the alternative (delete the kind, fold it into meta-notes) was refused: the shared store's 30 cases
are 29 `toy_quadratic` and ONE real row, and on the toy task the note really IS the case's twin
("best metric 4.483 via op 'improve' params {'x': 0.885, 'y': -0.9026}" carries both parameters
inline). On the real row it is not — `rubertlite-dr-unified-v8`'s note is a causal narrative naming
ONE hyperparameter while its case carries fifteen. `tests/data/v8_case_and_note.json` holds both
rows verbatim, so that claim is driven rather than asserted.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

from types import SimpleNamespace

from _source_scan import iter_trees

from looplab.adapters.toytask import ToyTask
from looplab.engine.orchestrator import Engine
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree

_PKG = Path(__file__).resolve().parents[1] / "looplab"
_TASK = Path(__file__).resolve().parents[1] / "examples" / "toy_task.json"


def _constructor_sites(name: str) -> list[str]:
    """Every `name(...)` call under looplab/, excluding the class statement itself."""
    sites = []
    for path, tree in iter_trees(_PKG):
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == name):
                sites.append(f"{path.relative_to(_PKG.parent)}:{node.lineno}")
    return sites


def test_the_jsonl_store_is_the_one_a_run_reaches():
    """If this ever goes empty, the engine has no case store and cross-run recall is silently off."""
    assert _constructor_sites("JsonlCaseLibrary"), (
        "nothing constructs JsonlCaseLibrary — the engine's case path is disconnected")


def test_the_vector_store_is_still_unwired_or_its_docstring_is_now_wrong():
    """`CaseLibrary` is documented as UNWIRED and kept for the Memora path.

    Wiring it in is a fine thing to do — but it needs `JsonlCaseLibrary`'s durability contract
    (whole-file reload, quarantine-preserving rewrite, retain-on-improvement across runs), and the
    two docstrings have to stop pointing at each other. Failing here is the reminder.
    """
    sites = _constructor_sites("CaseLibrary")
    assert not sites, (
        "CaseLibrary is now constructed under looplab/ at "
        + ", ".join(sites)
        + " — update both class docstrings (memory.py) and give it the durability contract "
          "JsonlCaseLibrary has, or this is a case store that loses cases across runs")


def test_both_docstrings_name_the_other_so_neither_reads_as_the_live_one_alone():
    text = (_PKG / "engine" / "memory.py").read_text(encoding="utf-8")
    unwired = text.index("class CaseLibrary:")
    live = text.index("class JsonlCaseLibrary:")
    assert "UNWIRED" in text[unwired:unwired + 900]
    assert "JsonlCaseLibrary" in text[unwired:unwired + 900]
    assert "CaseLibrary` above" in text[live:live + 900]


def _memory_dir_with_one_of_each(tmp_path, task_id: str, fingerprint: list[str]):
    """A real cross-run memory dir holding one case, one meta-note and one lesson for the SAME task.

    All three describe the same finished run, so any of the three could plausibly be the thing a
    later run is warm-started from. Each carries a distinctive marker token so the rendered prior can
    be attributed to exactly one tier.
    """
    mem = tmp_path / "mem"
    mem.mkdir(exist_ok=True)         # called twice: once to build the engine, once to re-key on its fingerprint
    (mem / "cases.jsonl").write_text(json.dumps({
        "task_id": task_id, "goal": "minimize the toy quadratic",
        # `case_only_param` is deliberately NOT in the note beside it: the note already spells
        # `params {'x': 3.0, 'y': -1.0}` in prose, so asserting on those would pass through the note
        # and prove nothing about the case. The distinct key is what makes the tier attributable.
        "direction": "min", "params": {"x": 3.0, "y": -1.0, "case_only_param": 7.5}, "metric": 0.0,
        "rationale": "CASEMARKER evaluate at the closed-form minimizer",
    }) + "\n", encoding="utf-8")
    (mem / "meta_notes.jsonl").write_text(json.dumps({
        "task_id": task_id, "run_id": "earlier",
        "direction": "min",
        "note": "NOTEMARKER best metric 0 via op 'draft' params {'x': 3.0, 'y': -1.0}",
    }) + "\n", encoding="utf-8")
    (mem / "lessons.jsonl").write_text(json.dumps({
        "task_id": task_id, "fingerprint": fingerprint, "run_id": "earlier",
        "direction": "min",
        "statement": "LESSONMARKER moving x toward 3 improves the metric",
        "outcome": "supported", "confidence": 0.7, "delta": 1.0, "role": "researcher",
    }) + "\n", encoding="utf-8")
    return mem


def _engine_over(tmp_path, mem):
    task = ToyTask.load(_TASK)
    researcher, developer = task.build_roles()
    return Engine(tmp_path / "run", task=task, researcher=researcher, developer=developer,
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                  reflection_priors=True, memory_dir=str(mem))


def _prior_over(tmp_path, mem, role=None):
    """Build the engine twice so the lesson can be re-keyed on THIS task's own fingerprint, then
    render the injected prior. The M2 similarity gate must never be what a case assertion trips on."""
    engine = _engine_over(tmp_path, mem)
    fp = [t for t in engine._task_fingerprint(engine._empty_state_for_fp())
          if not t.startswith("param:")]
    return fp, engine


def test_the_injected_prior_carries_all_three_tiers(tmp_path):
    """The three Memory tabs and what each one puts in the next run's prompt.

    RE-POINTED 2026-08-19: the case used to be asserted ABSENT here. It is present now because the
    loader that already reads the exact-task meta-note reads the case beside it — see the module
    docstring for why the note does not cover it. The role split is asserted below and the
    operator-facing copy moved in the same change (`docs/guide/memory.md`, `ui/src/conceptShelf.js`,
    `ui/src/panels.jsx`).
    """
    task = ToyTask.load(_TASK)
    mem = _memory_dir_with_one_of_each(tmp_path, task.id, [])
    fp, _ = _prior_over(tmp_path, mem)
    _memory_dir_with_one_of_each(tmp_path, task.id, fp)

    prior = _engine_over(tmp_path, mem)._load_reflection_priors()

    assert "NOTEMARKER" in prior, "exact-task meta-notes are the E4 warm start; they must reach the prior"
    assert "LESSONMARKER" in prior, "fingerprint-matched lessons are the M2/M3 transfer path"
    # The case's PAYLOAD, not its prose: the parameter dict is the only thing it holds that the note
    # beside it does not, so that is what has to arrive.
    assert "case_only_param" in prior, (
        "the winning configuration did not reach the prior — a case store nothing reads is a file "
        "that looks like memory")


def test_the_winning_configuration_is_what_the_note_does_not_carry(tmp_path):
    """THE argument for keeping the kind, driven on a real run's own two rows.

    `tests/data/v8_case_and_note.json` is `rubertlite-dr-unified-v8`'s case and meta-note, copied
    verbatim out of the shared store. Both describe the same finished run. The note names R-Drop's
    alpha and two recall numbers; the case carries the fifteen-key parameter dict that produced
    0.762048. Delete the kind and the run's configuration is gone — prose is the CAUSE, the case is
    the CONFIGURATION, and only one of them can be re-run.

    (The rows are re-keyed onto the toy task's own `task_id`/`direction` so the exact-task join
    fires; every byte of the payload under test — params, metric, rationale, note — is verbatim.)
    """
    real = json.loads((Path(__file__).parent / "data" / "v8_case_and_note.json")
                      .read_text(encoding="utf-8"))
    task = ToyTask.load(_TASK)
    mem = tmp_path / "mem"
    mem.mkdir(exist_ok=True)
    (mem / "cases.jsonl").write_text(json.dumps(
        {**real["case"], "task_id": task.id, "direction": "min"}) + "\n", encoding="utf-8")
    (mem / "meta_notes.jsonl").write_text(json.dumps(
        {**real["meta_note"], "task_id": task.id, "direction": "min"}) + "\n", encoding="utf-8")

    prior = _engine_over(tmp_path, mem)._load_reflection_priors()

    note = str(real["meta_note"]["note"])
    carried_only_by_the_case = [key for key in real["case"]["params"] if key not in note]
    assert len(carried_only_by_the_case) >= 10, (
        "the fixture's note now carries the configuration — re-derive this test before deleting the "
        "case kind")
    for key in carried_only_by_the_case:
        assert key in prior, f"{key} reaches the next run through the case and nothing else"
    assert str(real["case"]["metric"]) in prior


def test_the_developer_never_sees_the_case(tmp_path):
    """Same role gate as the meta-notes it rides beside: a winning HYPERPARAMETER set is research
    context, and `§role-split` exists so the Developer's context stays about code."""
    task = ToyTask.load(_TASK)
    mem = _memory_dir_with_one_of_each(tmp_path, task.id, [])
    engine = _engine_over(tmp_path, mem)
    researcher, developer = engine.lessons.load_reflection_priors_both()
    assert "case_only_param" in researcher and "NOTEMARKER" in researcher
    assert "case_only_param" not in developer and "NOTEMARKER" not in developer


def test_a_case_from_another_task_or_polarity_never_reaches_the_prior(tmp_path):
    """The case rides the SAME fail-closed `LessonScope` as every other cross-run reader — exact
    task plus compatible polarity — so this cannot become a route around the fence the notes and
    lessons are behind."""
    task = ToyTask.load(_TASK)
    mem = tmp_path / "mem"
    mem.mkdir(exist_ok=True)
    (mem / "cases.jsonl").write_text("".join(json.dumps(row) + "\n" for row in (
        {"task_id": "another_task", "goal": "g", "direction": "min",
         "params": {"foreign": 1.0}, "metric": 0.0, "rationale": "FOREIGN"},
        {"task_id": task.id, "goal": "g", "direction": "max",
         "params": {"opposite": 1.0}, "metric": 0.0, "rationale": "POLARITY"},
        {"task_id": task.id, "goal": "g", "direction": "min", "active": False,
         "params": {"retired": 1.0}, "metric": 0.0, "rationale": "INACTIVE"},
    )), encoding="utf-8")

    prior = _engine_over(tmp_path, mem)._load_reflection_priors()

    assert "foreign" not in prior and "opposite" not in prior and "retired" not in prior


def test_a_case_is_still_reachable_through_kb_search(tmp_path):
    """The other half: "not injected" must not quietly become "not read at all".

    `agents/factory.py` hands `cases.jsonl` to KnowledgeTools, which is what makes a past winner
    retrievable by a tool-using role. If this goes silent the case store is write-only and the
    Cases tab should say so instead.
    """
    from looplab.tools.knowledge_tools import KnowledgeTools

    task = ToyTask.load(_TASK)
    mem = _memory_dir_with_one_of_each(tmp_path, task.id, [])
    tools = KnowledgeTools(None, cases_path=str(mem / "cases.jsonl"))
    out = tools.execute("kb_search", {"query": "minimize the toy quadratic"})

    assert "CASEMARKER" in out and "PAST CASE" in out


def test_a_kb_search_hit_delivers_the_params_and_not_the_readers_own_task_prompt(tmp_path):
    """The OTHER reader, and the same defect in it.

    A `kb_search` hit is delivered head-clipped at `_KB_HIT_CHARS` (600). The case record used to
    put the GOAL first, so on `rubertlite-dr-unified-v8`'s real case `best params=` began at char
    691 of a 1,610-char record and could not fit — and because the scope gate admits a case only on
    an exact task id or a strict goal-fingerprint overlap, the 600 chars that DID arrive were the
    reader's own task prompt restated. The toy cases fit (two parameters), which is why this held
    for the whole life of the store.
    """
    from looplab.tools import knowledge_tools
    from looplab.tools.knowledge_tools import KnowledgeTools

    # Read the bound off the module rather than importing it: this test must fail on the PROPERTY
    # (the params did not arrive), not on an ImportError, which would silently retire it.
    hit_chars = getattr(knowledge_tools, "_KB_HIT_CHARS", 600)

    real = json.loads((Path(__file__).parent / "data" / "v8_case_and_note.json")
                      .read_text(encoding="utf-8"))["case"]
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(real) + "\n", encoding="utf-8")
    tools = KnowledgeTools(None, cases_path=str(path))
    tools.bind_state(SimpleNamespace(run_id="a-later-run", run_uid="later",
                                     task_id=real["task_id"], direction=real["direction"],
                                     goal=real["goal"]))

    out = tools.execute("kb_search", {"query": "best known configuration"})

    hit = out[out.index("PAST CASE"):]
    assert len(hit.split("\n…[")[0]) <= hit_chars + 200, "the hit is no longer bounded"
    for key in real["params"]:
        assert key in out, f"{key} did not survive the hit clip"
    assert str(real["metric"]) in out
    # ...and the cut is stated rather than silent — a hit that stops mid-recipe and looks whole is
    # the same class of defect one layer up.
    assert "not shown" in out


def test_bound_kb_search_scopes_cases_before_indexing(tmp_path):
    from looplab.tools.knowledge_tools import KnowledgeTools

    path = tmp_path / "cases.jsonl"
    rows = [
        {"task_id": "same", "goal": "optimize shared objective", "direction": "min",
         "params": {"x": 1}, "metric": 1.0, "rationale": "MIN_CASE"},
        {"task_id": "same", "goal": "optimize shared objective", "direction": "max",
         "params": {"x": 9}, "metric": 9.0, "rationale": "MAX_CASE"},
        {"task_id": "foreign", "goal": "optimize shared objective", "direction": "max",
         "params": {"x": 7}, "metric": 7.0, "rationale": "FOREIGN_CASE"},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    tools = KnowledgeTools(None, cases_path=str(path))
    tools.bind_state(SimpleNamespace(run_id="live", task_id="same", direction="max",
                                     goal="optimize shared objective"))

    out = tools.execute("kb_search", {"query": "shared objective"})
    assert "MAX_CASE" in out
    assert "MIN_CASE" not in out and "FOREIGN_CASE" not in out
    assert "scope=run" in out and "objective=max" in out


def test_kb_search_refreshes_when_a_source_file_changes(tmp_path):
    from looplab.tools.knowledge_tools import KnowledgeTools

    note = tmp_path / "first.md"
    note.write_text("# First\n\nalpha-only memory", encoding="utf-8")
    tools = KnowledgeTools(str(tmp_path))
    assert "alpha-only" in tools.execute("kb_search", {"query": "alpha-only"})

    note.write_text("# First\n\nbeta-only refreshed memory", encoding="utf-8")
    out = tools.execute("kb_search", {"query": "beta-only"})
    assert "beta-only refreshed" in out
    assert "KB_INDEX: revision=" in out
