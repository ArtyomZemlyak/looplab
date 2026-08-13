"""M2 task fingerprinting + M3 lessons-from-failures (this session, Phase 4).

Cross-run memory used to warm-start only the EXACT same task_id, and remembered only the winner.
Now: (M2) a task fingerprint lets a SIMILAR-but-new task retrieve priors by content overlap, and
(M3) run-end lessons include NEGATIVE results (tested/abandoned hypotheses + the dominant failure
reason) so a later run is steered away from known dead ends. All offline."""
from __future__ import annotations

import tempfile
from pathlib import Path

import anyio
import pytest
import orjson

from looplab.engine.memory import fingerprint_similarity, task_fingerprint
from looplab.engine.lessons import LessonMemory
from looplab.engine.orchestrator import Engine
from looplab.events.eventstore import read_jsonl_lenient
from looplab.search.policy import GreedyTree
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.adapters.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


def test_lesson_append_after_torn_tail_keeps_new_record_readable(tmp_path):
    mem = tmp_path / "mem"
    mem.mkdir()
    path = mem / "lessons.jsonl"
    path.write_bytes(b'{"crashed":')

    class _Engine:
        memory_dir = str(mem)

    LessonMemory(_Engine()).append_lessons(
        [{"statement": "new durable lesson", "outcome": "supported"}], hygiene=False)

    assert read_jsonl_lenient(path) == [
        {"statement": "new durable lesson", "outcome": "supported"}
    ]


def test_reflection_note_after_torn_tail_keeps_new_record_readable(tmp_path):
    mem = tmp_path / "mem"
    mem.mkdir()
    path = mem / "meta_notes.jsonl"
    path.write_bytes(b'{"crashed":')
    task = ToyTask.load(TASK)
    researcher, developer = task.build_roles()
    eng = Engine(
        tmp_path / "run",
        task=task,
        researcher=researcher,
        developer=developer,
        sandbox=SubprocessSandbox(),
        policy=GreedyTree(n_seeds=1, max_nodes=1),
        reflection_priors=True,
        memory_dir=str(mem),
    )
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": ""},
    })
    eng.store.append("node_evaluated", {"node_id": 0, "metric": 0.5})
    eng.store.append("run_finished", {"reason": "done", "finalization_required": True})
    eng._causal_meta_note = lambda *_args: "new durable note"  # type: ignore[method-assign]
    eng._reflect_lessons = lambda *_args: []  # type: ignore[method-assign]
    eng._comparative_lessons_on = False

    from looplab.events.replay import fold
    eng._write_reflection_note(fold(eng.store.read_all()))

    rows = read_jsonl_lenient(path)
    assert len(rows) == 1
    assert rows[0]["note"] == "new durable note"


def test_fingerprint_similar_beats_different():
    a = task_fingerprint("regression", "min", "select polynomial degree and ridge lambda for CV MSE",
                         metric="mse", param_names=["degree", "lam"])
    b = task_fingerprint("regression", "min", "choose polynomial degree plus ridge lambda, CV MSE",
                         metric="mse", param_names=["degree", "lam"])
    c = task_fingerprint("classification", "max", "tune a classifier for accuracy", metric="acc")
    assert fingerprint_similarity(a, b) > fingerprint_similarity(a, c)
    assert fingerprint_similarity(a, a) == 1.0
    assert fingerprint_similarity(a, c) == 0.0


def test_run_writes_lessons_with_fingerprint(tmp_path):
    mem = tmp_path / "mem"
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    anyio.run(Engine(tmp_path / "r1", task=task, researcher=r, developer=d,
                     sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=4),
                     reflection_priors=True, memory_dir=str(mem)).run)
    rows = [orjson.loads(l) for l in (mem / "lessons.jsonl").read_text().splitlines() if l.strip()]
    assert rows and any(x["outcome"] == "supported" for x in rows)
    assert all(x.get("fingerprint") for x in rows)          # every lesson is fingerprinted (M2)


def test_reflection_is_idempotent_across_reopen(tmp_path):
    """Run-end reflection must run at most ONCE per run. A reopened run (resume + budget_extend)
    re-enters finalize; a second `write_reflection_note` must NOT re-append the run's lessons — they
    consolidate by (statement, task_id) and a duplicate would inflate `evidence_count` so one run
    reads as 'verified on 2 runs' — nor duplicate the meta-note, nor re-spend the reflection LLM."""
    from looplab.events.replay import fold
    mem = tmp_path / "mem"
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    eng = Engine(tmp_path / "r1", task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=4),
                 reflection_priors=True, memory_dir=str(mem))
    anyio.run(eng.run)
    lessons_before = (mem / "lessons.jsonl").read_text()
    meta_path = mem / "meta_notes.jsonl"
    meta_before = meta_path.read_text() if meta_path.exists() else ""
    assert lessons_before.strip()                              # first pass really wrote lessons
    # simulate the reopen re-finalize at the SAME node count: reflect again over the already-reflected
    # log → skipped (nothing new), so no duplicate lessons/meta-note and no re-spent LLM.
    eng._write_reflection_note(fold(eng.store.read_all()))
    assert (mem / "lessons.jsonl").read_text() == lessons_before   # no duplicate lessons appended
    assert (meta_path.read_text() if meta_path.exists() else "") == meta_before  # no duplicate meta-note


def test_reflection_reruns_when_a_reopened_run_grows(tmp_path):
    """A reopened + budget-extended run that adds nodes (and may find a BETTER winner) MUST re-reflect —
    else cross-run memory keeps the stale first-finalize conclusion forever. The gate is node-count-aware:
    it skips only when a prior reflection already covered at least this many nodes."""
    from looplab.events.replay import fold
    from looplab.core.models import Idea, Node, NodeStatus
    mem = tmp_path / "mem"
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    eng = Engine(tmp_path / "r1", task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=4),
                 reflection_priors=True, memory_dir=str(mem))
    anyio.run(eng.run)
    lessons_before = (mem / "lessons.jsonl").read_text()
    meta_before = (mem / "meta_notes.jsonl").read_text().count("\n")
    # simulate the extension: a folded state with MORE nodes than the first reflection covered. A real
    # reopen (resume + budget_extend + new run_finished) also produces a NEW finish sequence, so give
    # the grown state one — the crash-idempotency de-dup keys on (run_uid, finish_seq), and re-using the
    # first finish's seq would (correctly) read as a plain crash-retry rather than a grown reopen.
    grown = fold(eng.store.read_all())
    base = max(grown.nodes) + 1
    for i in range(base, base + 3):
        grown.nodes[i] = Node(id=i, operator="improve", idea=Idea(operator="improve"),
                              metric=0.01, status=NodeStatus.evaluated, feasible=True)
    grown.last_finish_seq = grown.last_finish_seq + 100
    eng._write_reflection_note(grown)
    # the append-only meta-note grew → reflection genuinely re-ran on the grown run …
    assert (mem / "meta_notes.jsonl").read_text().count("\n") > meta_before
    # … yet the run_id de-dup keeps consolidated lessons from inflating (re-reflecting itself counts once)
    from looplab.engine.memory import consolidate_lessons
    import orjson as _orjson
    rows = [_orjson.loads(x) for x in (mem / "lessons.jsonl").read_text().splitlines() if x.strip()]
    assert all(int(o.get("evidence_count", 1)) <= 1 for o in consolidate_lessons(rows))  # single run stays 1


def test_reflection_reruns_when_reset_changes_material_at_same_node_count(tmp_path):
    from looplab.events.replay import fold
    mem = tmp_path / "mem"
    task = ToyTask.load(TASK)
    researcher, developer = task.build_roles()
    eng = Engine(tmp_path / "r1", task=task, researcher=researcher, developer=developer,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=4),
                 reflection_priors=True, memory_dir=str(mem))
    anyio.run(eng.run)
    before = (mem / "meta_notes.jsonl").read_text().count("\n")
    reset = fold(eng.store.read_all())
    changed = reset.nodes[min(reset.nodes)]
    changed.attempt += 1
    changed.code += "\n# reset lifecycle"
    reset.last_finish_seq += 100
    eng._write_reflection_note(reset)
    assert (mem / "meta_notes.jsonl").read_text().count("\n") > before


def test_reflection_meta_note_deduped_after_a_crash_before_the_marker(tmp_path):
    """#3: finalization is RETRIED after a crash. If the engine dies AFTER the meta_notes line is
    written but BEFORE the reflection marker (EV_REFLECTION_NOTE) commits, the retry must NOT append a
    SECOND identical note — which would pollute a later run's warm-start prior. The (task_id,
    finish_seq) key on the file itself de-dups it; the event-log marker cannot, since it isn't written
    yet on that crash."""
    from looplab.events.replay import fold
    from looplab.events.types import EV_REFLECTION_NOTE
    mem = tmp_path / "mem"
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    eng = Engine(tmp_path / "r1", task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=4),
                 reflection_priors=True, memory_dir=str(mem))
    # A minimal finished run so last_finish_seq is set and best() returns a node.
    eng.store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "params": {}, "rationale": ""}})
    eng.store.append("node_evaluated", {"node_id": 0, "metric": 0.5})
    eng.store.append("run_finished", {"reason": "done", "finalization_required": True})
    fseq = fold(eng.store.read_all()).last_finish_seq
    assert fseq >= 0
    meta_path = mem / "meta_notes.jsonl"

    def _notes_for_finish():
        if not meta_path.exists():
            return []
        return [o for o in (orjson.loads(l) for l in meta_path.read_text().splitlines() if l.strip())
                if o.get("finish_seq") == fseq]

    # Crash the FIRST reflection right before the marker commits: the meta_notes line lands, then the
    # EV_REFLECTION_NOTE append raises (process dies).
    real_append = eng.store.append
    def crashing_append(etype, data):
        if etype == EV_REFLECTION_NOTE:
            raise RuntimeError("simulated crash before finalization marker")
        return real_append(etype, data)
    eng.store.append = crashing_append           # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        eng._write_reflection_note(fold(eng.store.read_all()))
    eng.store.append = real_append               # type: ignore[method-assign]
    assert len(_notes_for_finish()) == 1         # exactly one note written pre-crash
    assert not any(e.type == EV_REFLECTION_NOTE for e in eng.store.read_all())   # marker never committed

    # The finalization RETRY: no marker for this finish exists, so the finish_seq early-return can't
    # fire — the (task_id, finish_seq) file de-dup is what must prevent a duplicate note.
    eng._write_reflection_note(fold(eng.store.read_all()))
    assert len(_notes_for_finish()) == 1         # NOT duplicated
    assert any(e.type == EV_REFLECTION_NOTE and e.data.get("finish_seq") == fseq
               for e in eng.store.read_all())    # …and the marker committed this time

    # A THIRD retry (marker now present) short-circuits on the finish_seq gate — still one note.
    eng._write_reflection_note(fold(eng.store.read_all()))
    assert len(_notes_for_finish()) == 1


def _write_lessons(mem: Path, rows):
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "lessons.jsonl").write_text("\n".join(orjson.dumps(x).decode() for x in rows) + "\n")


def test_similar_task_retrieves_lessons_including_negatives(tmp_path):
    mem = tmp_path / "mem"
    fp_reg = task_fingerprint("regression", "min", "select polynomial degree and ridge lambda CV MSE",
                              metric="mse", param_names=["degree", "lam"])
    fp_cls = task_fingerprint("classification", "max", "tune a classifier", metric="acc")
    _write_lessons(mem, [
        {"task_id": "reg_A", "fingerprint": fp_reg, "kind": "regression", "direction": "min",
         "statement": "a degree-8 polynomial overfits badly", "outcome": "tested",
         "delta": -0.05, "confidence": 0.5},
        {"task_id": "reg_A", "fingerprint": fp_reg, "kind": "regression", "direction": "min",
         "statement": "ridge lambda near 1.0 with degree 3 works", "outcome": "supported",
         "delta": 0.12, "confidence": 0.7},
        {"task_id": "other", "fingerprint": fp_cls, "kind": "classification", "direction": "max",
         "statement": "unrelated classifier trick", "outcome": "supported", "delta": 0.02,
         "confidence": 0.7},
    ])

    class SimilarRegTask:
        id = "reg_B"; kind = "regression"; direction = "min"; metric = "mse"
        goal = "choose polynomial degree plus ridge lambda, minimize CV MSE"

        def build_roles(self):
            return ToyTask.load(TASK).build_roles()

    t = SimilarRegTask()
    r, d = t.build_roles()
    eng = Engine(tmp_path / "r2", task=t, researcher=r, developer=d, sandbox=SubprocessSandbox(),
                 policy=GreedyTree(n_seeds=2, max_nodes=3), reflection_priors=True, memory_dir=str(mem))
    prior = eng._load_reflection_priors()
    assert "Lessons from related runs" in prior
    assert "overfits badly" in prior and "tested" in prior      # NEGATIVE lesson transferred (M3)
    assert "ridge lambda" in prior                              # positive lesson transferred
    assert "unrelated classifier" not in prior                 # dissimilar task filtered out (M2)


def test_merge_prompt_opts_unwraps_researcher_chain():
    # I18: the merge_system PromptStore override + configured parser live on the ROLES (tasks.py
    # wires researcher.prompts; LLMResearcher carries `parser`) — the lesson-hygiene pass must
    # find them through the same researcher→inner→fallback→developer chain reflect_client walks.
    from looplab.engine.lessons import LessonMemory

    class Inner:
        prompts = "STORE"
        parser = "json"

    class Wrapper:                       # e.g. a ForesightPanel/Surrogate wrapper without prompts
        inner = Inner()

    class Eng:
        researcher = Wrapper()
        developer = None

    assert LessonMemory(Eng())._merge_prompt_opts() == ("STORE", "json")

    class Bare:                          # toy backends: nothing wired -> the historical defaults
        researcher = None
        developer = None

    assert LessonMemory(Bare())._merge_prompt_opts() == (None, "tool_call")


def test_merge_prompt_opts_reads_through_panel_and_unified_facade():
    # H7: the search PanelResearcher and the UnifiedAgent facade now expose read-through
    # `parser`/`prompts` (panel also `client`), so a panel(unified(inner)) chain — where the walker
    # sees only the OUTERMOST object — still yields the inner researcher's configured values instead
    # of silently falling back to the (None, "tool_call") defaults.
    from looplab.agents.unified_agent import UnifiedAgent
    from looplab.engine.lessons import LessonMemory
    from looplab.search.panel import PanelResearcher

    class Inner:
        parser = "json"
        prompts = None
        client = None
        bounds = None
        space_hint = ""

        def propose(self, state, parent):
            return None

    class Dev:
        def implement(self, idea):
            return "code"

    unified = UnifiedAgent(researcher=Inner(), developer=Dev(), prompts="STORE")
    outer = PanelResearcher(unified, k=2)

    class Eng:
        researcher = outer
        developer = None

    assert LessonMemory(Eng())._merge_prompt_opts() == ("STORE", "json")


def test_consolidate_lessons_threads_prompts_and_parser(monkeypatch):
    # The parser/prompts kwargs must survive consolidate_lessons -> _agentic_merge_lessons ->
    # hybrid_merge.consolidate (where they configure the agent adjudication call).
    import looplab.search.hybrid_merge as hm
    from looplab.engine.memory import consolidate_lessons
    seen = {}

    def fake_consolidate(texts, client, *, kind="items", embed=None, cluster_k=6, goal="",
                         parser="tool_call", prompts=None):
        seen["parser"], seen["prompts"] = parser, prompts
        return [{"members": [i], "merged": t} for i, t in enumerate(texts)]

    monkeypatch.setattr(hm, "consolidate", fake_consolidate)
    rows = [{"statement": "a lesson about tree depth", "outcome": "supported", "task_id": "t"},
            {"statement": "an entirely different finding", "outcome": "supported", "task_id": "t"}]
    sentinel = object()
    consolidate_lessons(rows, client=object(), parser="json", prompts=sentinel)
    assert seen == {"parser": "json", "prompts": sentinel}


def test_settings_defaults_enable_phase3_and_4():
    # Product default (via Settings): hypotheses + cross-run memory are ON out of the box.
    from looplab.core.config import Settings
    s = Settings()
    assert s.track_hypotheses is True and s.reflection_priors is True


def test_lessons_engine_level_off_when_flag_not_passed(tmp_path):
    # Engine's low-level param default stays False, so building Engine directly without the flag
    # writes no lessons file (the product turns it on via Settings -> cli, tested above).
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    mem = tmp_path / "mem"
    anyio.run(Engine(tmp_path / "r", task=task, researcher=r, developer=d,
                     sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=3),
                     memory_dir=str(mem)).run)
    assert not (mem / "lessons.jsonl").exists()


# --------------------------------------------------------- read-path slot hygiene (prompt budget)
def _prior_engine(tmp_path, mem, task_id="slotted"):
    """An Engine whose task id is `task_id`, so every stored lesson carrying that task_id scores an
    EXACT-task similarity of 1.0 and the prior's ordering is decided purely by the rank key."""

    class SlotTask:
        id = task_id; kind = "regression"; direction = "min"; metric = "mse"
        goal = "minimize the error"

        def build_roles(self):
            return ToyTask.load(TASK).build_roles()

    t = SlotTask()
    r, d = t.build_roles()
    return Engine(tmp_path / f"run-{task_id}", task=t, researcher=r, developer=d,
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=3),
                  reflection_priors=True, memory_dir=str(mem))


def test_one_template_family_cannot_eat_the_whole_lesson_prior(tmp_path):
    """A prior has FIVE lesson slots. The offline producers (`lessons_distill._winner_lesson`,
    `memory.param_credit_statement`) are f-string templates whose rows differ only in their digits,
    so the old `statement[:80]` de-dup key never fired on them and ONE family could take all five —
    leaving a genuinely different finding unshown. Measured on the real shared store before this
    fix: 5/5 slots for `toy_quadratic`, 7/42 (17%) of slots across every task."""
    mem = tmp_path / "mem"
    # Lowest file index => ranked LAST among equal-confidence rows (recency breaks the tie), so this
    # only reaches the prior if the template family stops consuming every slot.
    rows = [{"task_id": "slotted", "fingerprint": [], "kind": "regression", "direction": "min",
             "statement": "feature standardization is what made the linear model competitive",
             "outcome": "supported", "delta": None, "confidence": 0.55}]
    # Six siblings of ONE template — identical modulo digits, all well inside the old 80-char key.
    for i in range(6):
        rows.append({"task_id": "slotted", "fingerprint": [], "kind": "regression", "direction": "min",
                     "statement": f"changing x {i}.2255->{i}.4047, y -1.9013->-2.7324 "
                                  f"regressed the metric by {i}.227",
                     "outcome": "failed", "delta": None, "confidence": 0.55})
    _write_lessons(mem, rows)

    prior = _prior_engine(tmp_path, mem)._load_reflection_priors()
    body = prior.split("Lessons from related runs")[1]
    kept = [ln for ln in body.split(";") if "regressed the metric by" in ln]
    assert len(kept) == 1, f"one template family took {len(kept)} slots:\n{body}"
    # The surviving row is rendered VERBATIM — de-dup rations slots, it never rewrites a statement.
    assert "5.2255->5.4047" in body, body        # the newest sibling (highest file index) wins
    # ...and the distinct finding it used to crowd out is now shown.
    assert "feature standardization" in body, body


def test_repeated_meta_notes_do_not_eat_the_warm_start_budget(tmp_path):
    """`meta_notes.jsonl` has no consolidation pass at all, and `write_reflection_note`'s only
    de-dup is per modern (run_uid, finish_seq), with legacy (run_id, finish_seq) fallback — which
    by construction cannot see a DIFFERENT run that
    landed on the same winner. So the three-slot warm-start tail repeated: on the real shared store
    two of the three `toy_quadratic` slots were byte-identical."""
    mem = tmp_path / "mem"
    mem.mkdir(parents=True, exist_ok=True)
    notes = [
        {"task_id": "slotted", "direction": "min", "note": "best metric 0.5 via op 'draft' params {'x': 1}; 2 nodes"},
        {"task_id": "slotted", "direction": "min", "note": "best metric 0.4 via op 'merge' params {'x': 9}; 7 nodes"},
    ]
    # Three separate runs that all rediscovered the same winner: distinct rows, identical text.
    notes += [{"task_id": "slotted", "run_id": f"r{i}", "direction": "min", "finish_seq": i,
               "note": "best metric 0.3 via op 'improve' params {'x': 3}; 4 nodes"}
              for i in range(3)]
    (mem / "meta_notes.jsonl").write_text(
        "\n".join(orjson.dumps(x).decode() for x in notes) + "\n")

    prior = _prior_engine(tmp_path, mem)._load_reflection_priors()
    line = prior.split("Prior-run insights for this task (meta-learned): ")[1].split("\n")[0]
    assert line.count("op 'improve'") == 1, f"the repeated note took >1 slot: {line}"
    # The freed slots go to the notes the repeat used to push out of the [-3:] tail.
    assert "op 'draft'" in line and "op 'merge'" in line, line


def test_prompt_slot_key_is_not_a_substitute_for_normalize_statement():
    """The two keys must stay SEPARATE. `normalize_statement` decides what `consolidate_lessons`
    MERGES and what `filter_contradicted` treats as one claim's verdict history; collapsing digits
    THERE would fold two distinct measurements into one row and let one run's delta silently retire
    another's. `prompt_slot_key` only rations prompt slots. Folding them together would be a silent
    data-loss bug, so pin the difference behaviourally rather than trusting the docstrings."""
    from looplab.engine.memory import consolidate_lessons, normalize_statement, prompt_slot_key

    a = "changing x 0.2255->0.4047 regressed the metric by 1.227"
    b = "changing x 0.7176->0.3338 regressed the metric by 0.099"
    assert normalize_statement(a) != normalize_statement(b)   # distinct MEASUREMENTS
    assert prompt_slot_key(a) == prompt_slot_key(b)           # one SENTENCE for prompt purposes

    rows = [{"task_id": "t", "statement": s, "outcome": "failed", "run_id": f"r{i}"}
            for i, s in enumerate((a, b))]
    assert len(consolidate_lessons(rows)) == 2, "the write path must not merge two measurements"


# --------------------------------------------------------------------------- #
# M3, the half that had quietly stopped working: a run with NO winner.
#
# `write_reflection_note`'s own comment promises it ("a run in which every node failed is exactly
# the negative lesson M3 exists to record") and `reflect_lessons` refused to make the call, so the
# entire learning of every crash-only run was discarded. Reproduced from `runs/rubert-dr-0805`:
# 2 nodes, 0 evaluated, node 0 crashed after six real library-migration repairs; finalization ran
# every step and `reflection_note` recorded `n_lessons: 0`.
# --------------------------------------------------------------------------- #


def _crash_only_engine(tmp_path, mem, *, nodes=(0, 1)):
    """An Engine whose log holds only FAILED nodes — no `node_evaluated`, so `final.best()` is None."""
    task = ToyTask.load(TASK)
    researcher, developer = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=researcher, developer=developer,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=2),
                 reflection_priors=True, memory_dir=str(mem))
    eng.store.append("run_started", {"run_id": "crash-only-1", "task_id": task.id,
                                     "goal": task.goal, "direction": "min"})
    for nid in nodes:
        eng.store.append("node_created", {
            "node_id": nid, "parent_ids": [], "operator": "draft",
            "idea": {"operator": "draft", "params": {}, "rationale": ""}})
        eng.store.append("node_failed", {
            "node_id": nid, "reason": "crash",
            "error": "RuntimeError: LightningModule has parameters not used in producing the loss"})
    eng.store.append("run_finished", {"reason": "stuck", "finalization_required": True})
    eng._comparative_lessons_on = False
    return eng


class _ScriptedReflect:
    """Stands in for `agentic_text`: records the prompt it was handed, returns canned lesson lines."""

    def __init__(self, reply):
        self.reply = reply
        self.prompts: list[str] = []

    def __call__(self, _client, _tools, messages, **_kw):
        self.prompts.append(messages[0]["content"])
        return self.reply


def test_crash_only_run_still_distils_lessons_to_the_shared_store(tmp_path, monkeypatch):
    """The defect: 0 evaluated nodes -> `reflect_lessons` returned before calling the model, so the
    shared store learned NOTHING from a run whose failures are the most transferable thing it had."""
    from looplab.events.replay import fold
    import looplab.agents.agent as agent_mod

    mem = tmp_path / "mem"
    eng = _crash_only_engine(tmp_path, mem)
    scripted = _ScriptedReflect(
        "[BAD] DDP with an unused-parameter module needs find_unused_parameters or the head dropped\n"
        "[GOOD] pin the trainer's strategy explicitly before porting a script to a newer Lightning")
    monkeypatch.setattr(agent_mod, "agentic_text", scripted)
    eng._reflect_client = lambda: object()          # a real run HAS a client; the toy roles do not

    eng._write_reflection_note(fold(eng.store.read_all()))

    rows = read_jsonl_lenient(mem / "lessons.jsonl")
    assert len(rows) == 2, "a crash-only run must still write its failure lessons"
    assert {r["outcome"] for r in rows} == {"failed", "supported"}
    # Every row carries the retrieval + polarity provenance a later run reads it back through.
    for r in rows:
        assert r["fingerprint"] and r["direction"] == "min" and r["run_id"] == "crash-only-1"
        assert r["evidence"] == [0, 1]              # grounded in the two failed nodes
    # ...and the run's own audit sidecar agrees, instead of reporting n_lessons: 0.
    note = [e.data for e in eng.store.read_all() if e.type == "reflection_note"][-1]
    assert note["n_lessons"] == 2


def test_crash_only_lessons_reach_the_developer_too(tmp_path, monkeypatch):
    """A run with no measured result learns about what BLOCKED it — library/API/hardware constraints,
    which is the Developer's category. Stamping those `role: researcher` routes them away from the
    role that will hit the same constraint next time, so they are left untagged = shared (the same
    thing `lessons_reconcile` does for an unattributed comparative line)."""
    from looplab.events.replay import fold
    import looplab.agents.agent as agent_mod

    mem = tmp_path / "mem"
    eng = _crash_only_engine(tmp_path, mem)
    monkeypatch.setattr(agent_mod, "agentic_text",
                        _ScriptedReflect("[BAD] a newer Lightning needs the DDP strategy pinned"))
    eng._reflect_client = lambda: object()
    eng._write_reflection_note(fold(eng.store.read_all()))

    assert "role" not in read_jsonl_lenient(mem / "lessons.jsonl")[0]

    # Drive it through the real read side: a LATER run on the same task must see it as BOTH roles.
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    later = Engine(tmp_path / "later", task=task, researcher=r, developer=d,
                   sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                   reflection_priors=True, memory_dir=str(mem))
    researcher_prior, developer_prior = later._load_reflection_priors_both()
    assert "DDP strategy pinned" in researcher_prior
    assert "DDP strategy pinned" in developer_prior


def test_a_winners_lessons_stay_researcher_tagged(tmp_path, monkeypatch):
    """The other half of §role-split: a run that DID measure something still routes its
    technique/strategy generalizations to the Researcher only."""
    from looplab.events.replay import fold
    import looplab.agents.agent as agent_mod

    mem = tmp_path / "mem"
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=2),
                 reflection_priors=True, memory_dir=str(mem))
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"lr": 0.1}, "rationale": ""}})
    eng.store.append("node_evaluated", {"node_id": 0, "metric": 0.5})
    eng.store.append("run_finished", {"reason": "done", "finalization_required": True})
    eng._comparative_lessons_on = False
    monkeypatch.setattr(agent_mod, "agentic_text",
                        _ScriptedReflect("[GOOD] a smaller learning rate converged more reliably"))
    eng._reflect_client = lambda: object()
    eng._causal_meta_note = lambda *_a: "note"      # type: ignore[method-assign]

    eng._write_reflection_note(fold(eng.store.read_all()))

    assert read_jsonl_lenient(mem / "lessons.jsonl")[0]["role"] == "researcher"


def test_no_winner_prompt_names_the_blockers_instead_of_an_empty_what_worked(tmp_path, monkeypatch):
    """The prompt is a contract: with a winner it must stay byte-identical, and without one it must
    not ship a dangling `What worked (best first):` header followed by nothing."""
    from looplab.events.replay import fold
    import looplab.agents.agent as agent_mod

    mem = tmp_path / "mem"
    eng = _crash_only_engine(tmp_path, mem)
    scripted = _ScriptedReflect("[BAD] something went wrong in a reusable way")
    monkeypatch.setattr(agent_mod, "agentic_text", scripted)
    eng._reflect_client = lambda: object()

    eng._write_reflection_note(fold(eng.store.read_all()))

    assert len(scripted.prompts) == 1
    prompt = scripted.prompts[0]
    assert "What worked (best first):" not in prompt
    assert "NOTHING in this run reached a measured result" in prompt
    assert "read_experiment / read_logs" in prompt          # error_reason is a bucket, not the error
    assert "\nWhat failed:\n#0 draft failed: crash" in prompt


def test_reflection_makes_no_call_when_the_run_has_no_evidence_at_all(tmp_path, monkeypatch):
    """The guard `best is None` was standing in for: an empty run has nothing to distil and the
    model can only invent, so it must still return without spending a call."""
    from looplab.events.replay import fold
    import looplab.agents.agent as agent_mod

    mem = tmp_path / "mem"
    eng = _crash_only_engine(tmp_path, mem, nodes=())       # run_finished, and not one node
    scripted = _ScriptedReflect("[GOOD] invented from nothing")
    monkeypatch.setattr(agent_mod, "agentic_text", scripted)
    eng._reflect_client = lambda: object()

    eng._write_reflection_note(fold(eng.store.read_all()))

    assert scripted.prompts == [], "no evidence -> no paid reflection call"
    assert not (mem / "lessons.jsonl").exists()


def test_winner_prompt_is_unchanged(tmp_path, monkeypatch):
    """The other side of the same contract: an evaluated run's prompt keeps the original wording."""
    from looplab.events.replay import fold
    import looplab.agents.agent as agent_mod

    mem = tmp_path / "mem"
    task = ToyTask.load(TASK)
    researcher, developer = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=researcher, developer=developer,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=2),
                 reflection_priors=True, memory_dir=str(mem))
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"lr": 0.1}, "rationale": ""}})
    eng.store.append("node_evaluated", {"node_id": 0, "metric": 0.5})
    eng.store.append("run_finished", {"reason": "done", "finalization_required": True})
    eng._comparative_lessons_on = False
    scripted = _ScriptedReflect("[GOOD] a smaller learning rate converged more reliably")
    monkeypatch.setattr(agent_mod, "agentic_text", scripted)
    eng._reflect_client = lambda: object()
    eng._causal_meta_note = lambda *_a: "note"      # type: ignore[method-assign]

    eng._write_reflection_note(fold(eng.store.read_all()))

    prompt = scripted.prompts[0]
    assert "\nWhat worked (best first):\n#0 draft metric=0.5 params={'lr': 0.1}" in prompt
    assert "NOTHING in this run reached a measured result" not in prompt


# --------------------------------------------------------------------------------------------
# SALVAGE must not leak into the prompt that writes a CROSS-RUN lesson
# --------------------------------------------------------------------------------------------

def _reflection_state(*, salvaged_feasible: bool):
    """A run whose BEST number belongs to a node whose metric was salvaged, not measured."""
    from looplab.core.models import Idea, Node, NodeStatus, RunState

    st = RunState(goal="g", direction="max", task_id="t", run_id="r")
    st.nodes[0] = Node(id=0, operator="draft", status=NodeStatus.evaluated, metric=0.50,
                       idea=Idea(operator="draft", params={"lr": 0.1}, rationale="baseline"))
    st.nodes[1] = Node(
        id=1, operator="improve", status=NodeStatus.evaluated, metric=0.95,
        idea=Idea(operator="improve", params={"lr": 0.3}, rationale="salvaged one"),
        violations=[] if salvaged_feasible else [{"name": "metric_salvaged", "value": 0.95,
                                                  "max": None, "min": None}],
        feasible=salvaged_feasible,
        metric_provenance={"salvaged": True, "source": "declared_reader", "stage": "train"})
    st.best_node_id = 0
    return st


def _captured_causal_prompt(monkeypatch, state):
    """Drive the REAL `causal_meta_note` and return the prompt it sent."""
    from looplab.agents import agent as agent_module
    from looplab.engine.lessons import LessonMemory

    seen: dict = {}

    def _fake_agentic_text(client, tools, messages, **kw):
        seen["prompt"] = messages[-1]["content"]
        return "because it did"

    monkeypatch.setattr(agent_module, "agentic_text", _fake_agentic_text)
    mem = LessonMemory.__new__(LessonMemory)
    mem._e = type("_E", (), {"_reflect_client": lambda self: object()})()
    mem._reflect_tools = lambda _state: []
    mem._reflect_loop_opts = lambda: None
    assert mem.causal_meta_note(state, state.nodes[state.best_node_id]) == "because it did"
    return seen["prompt"]


def test_an_excluded_salvaged_node_is_not_row_one_of_the_cross_run_lesson_prompt(monkeypatch):
    """`final.evaluated_nodes()` sorted by metric is neither the population the champion comes from
    nor one where every number means the same thing. Under the DEFAULT `metric_salvage: audit` the
    salvaged node is deliberately NOT feasible — and it still topped the table the reflector was
    asked "why did #0 win?" over, while `causal_meta_note` named #0 in the same breath. The lesson
    that prompt authors is CROSS-RUN, and its `ev_ids`/`ev_sig` stamped the salvaged node as the
    evidence a later run reuses."""
    prompt = _captured_causal_prompt(monkeypatch, _reflection_state(salvaged_feasible=False))
    assert "#1" not in prompt, "an unmeasured metric must not be shown as this run's best result"
    assert "#0" in prompt and "The winner is #0" in prompt


def test_a_salvaged_node_the_operator_admitted_stays_but_says_what_it_is(monkeypatch):
    """Under `select` the operator accepted it as comparable, so hiding it would be the same silence
    one layer up — but a model generalizing about a number must be told it was recovered rather than
    measured."""
    prompt = _captured_causal_prompt(monkeypatch, _reflection_state(salvaged_feasible=True))
    assert "#1" in prompt and "metric SALVAGED, not measured" in prompt
    # …and the MEASURED node carries no such annotation.
    measured_row = [ln for ln in prompt.splitlines() if ln.startswith("#0")][0]
    assert "SALVAGED" not in measured_row


def test_the_lesson_evidence_ids_come_from_the_same_filtered_population(monkeypatch, tmp_path):
    """The rank feeds `ev_ids`/`ev_sig`, which is what `reconcile_lessons` re-derives against. A
    salvaged node stamped as evidence makes a cross-run lesson claim a number nobody measured."""
    from looplab.engine.lessons_distill import _ranked_experiment_rows

    excluded = _reflection_state(salvaged_feasible=False)
    assert [n.id for n in _ranked_experiment_rows(excluded, 5)] == [0]
    admitted = _reflection_state(salvaged_feasible=True)
    assert [n.id for n in _ranked_experiment_rows(admitted, 5)] == [1, 0]


# --------------------------------------------------------------------------------------------
# …and neither may it ground the M6 COMPARATIVE pair lesson, which is the same claim by another
# route: the rank was fixed and `select_comparison_pairs` — the other producer writing into the
# SAME shared `lessons.jsonl` — went on handing the salvaged node to the credit assigner.
# --------------------------------------------------------------------------------------------

def _lineage_state(*, violations=None, provenance=None, flagged=None, trust_gate="audit"):
    """Parent #0 (measured 0.50) -> child #1 (0.95), where the CHILD is whatever the caller says."""
    from looplab.core.models import Idea, Node, NodeStatus, RunState

    st = RunState(goal="g", direction="max", task_id="t", run_id="r")
    st.trust_gate = trust_gate
    st.nodes[0] = Node(id=0, operator="draft", status=NodeStatus.evaluated, metric=0.50,
                       idea=Idea(operator="draft", params={"lr": 0.1}))
    st.nodes[1] = Node(id=1, operator="improve", parent_ids=[0], status=NodeStatus.evaluated,
                       metric=0.95, idea=Idea(operator="improve", params={"lr": 0.3}),
                       violations=list(violations or []), feasible=not violations,
                       metric_provenance=provenance)
    for nid in (flagged or ()):
        st.reward_hacks.append({"node_id": nid, "generation": 0,
                                "signals": [{"signal": "critic:hardcoded_metric"}]})
    return st


_SALVAGE_ROW = [{"name": "metric_salvaged", "value": 0.95, "max": None, "min": None}]
_SALVAGE_PROV = {"salvaged": True, "condition": "artifact_contract", "source": "declared_reader",
                 "stage": "train", "salvaged_error": "expect files missing: final/model.safetensors"}


def test_an_excluded_salvaged_node_is_never_half_of_a_comparative_pair():
    """A `solution` pair IS a metric claim — its whole content is the Δ between two numbers, and the
    lesson credits whichever difference produced it. The violation that keeps the node out of
    `feasible_nodes()` said that Δ may not decide the champion; the pair distiller wrote it into the
    SHARED store anyway, where a later run retrieves it as evidence."""
    from looplab.engine.memory import select_comparison_pairs

    child = _lineage_state(violations=_SALVAGE_ROW, provenance=_SALVAGE_PROV)
    assert select_comparison_pairs(child) == []
    # …and from the PARENT side too: the Δ is just as unmeasured when the baseline is the salvage.
    parent = _lineage_state()
    parent.nodes[0].violations = list(_SALVAGE_ROW)
    parent.nodes[0].feasible = False
    parent.nodes[0].metric_provenance = dict(_SALVAGE_PROV)
    assert select_comparison_pairs(parent) == []


def test_the_salvage_the_operator_admitted_still_pairs():
    """`select` + operator-produced bytes mints NO violation row, which is the operator saying this
    number is comparable. The boundary reads the ROW, so it must not overrule that."""
    from looplab.engine.memory import select_comparison_pairs

    admitted = _lineage_state(provenance=_SALVAGE_PROV)      # salvaged, no violation row
    assert [(p["a"], p["b"]) for p in select_comparison_pairs(admitted)] == [(1, 0)]


def test_a_constraint_violating_node_keeps_its_place_in_the_pairs():
    """The rule is about MEASUREMENT, not about feasibility. A node that breached one of the
    operator's hard bounds has a number the scoring path really produced — its exclusion is a fact
    about the bound — so `promotion_eligible_nodes` is deliberately NOT the predicate here."""
    from looplab.engine.memory import select_comparison_pairs

    bound = _lineage_state(violations=[{"name": "latency_ms", "value": 900.0, "max": 500.0,
                                        "min": None}])
    assert bound.nodes[1].feasible is False
    assert [(p["a"], p["b"]) for p in select_comparison_pairs(bound)] == [(1, 0)]


def test_a_trust_flagged_node_is_barred_from_a_pair_exactly_when_the_gate_is_on():
    """The identical leak, one violation family over: under `gate`/`block` the run refuses to select
    on a reward-hacked number, and a credit-assigned lesson generalizing from it is that refusal
    leaving the run. Under `audit` nothing is gate-excluded and this must not invent an exclusion the
    operator turned off."""
    from looplab.engine.memory import select_comparison_pairs

    assert select_comparison_pairs(_lineage_state(flagged=[1], trust_gate="gate")) == []
    audited = _lineage_state(flagged=[1], trust_gate="audit")
    assert [(p["a"], p["b"]) for p in select_comparison_pairs(audited)] == [(1, 0)]


# --------------------------------------------------------------------------------------------
# The other half of the boundary: dropped from the METRIC claims is not dropped from the run.
# --------------------------------------------------------------------------------------------

def test_a_salvaged_node_still_teaches_what_it_broke_on(monkeypatch):
    """An excluded salvaged node is `evaluated`, so the reflection prompt's failure list (keyed on
    `NodeStatus.failed`) never saw it either — and once the rank correctly refused it, the one thing
    the run had actually learnt from it went nowhere. The failure is real, is independent of the
    unmeasured number, and is the cheapest evidence to reuse (a declaration that breaks breaks for
    every later run on the same repo). So it is fed as an OBSERVATION, with its condition and its
    recorded error and no metric at all."""
    from looplab.agents import agent as agent_module
    from looplab.engine.lessons import LessonMemory

    seen: dict = {}

    def _fake_agentic_text(client, tools, messages, **kw):
        seen["prompt"] = messages[-1]["content"]
        return "[BAD] a declared artifact path must match what the testbed composes"

    monkeypatch.setattr(agent_module, "agentic_text", _fake_agentic_text)
    state = _lineage_state(violations=_SALVAGE_ROW, provenance=_SALVAGE_PROV)
    mem = LessonMemory.__new__(LessonMemory)
    mem._e = type("_E", (), {"_reflect_client": lambda self: object(), "task": None})()
    mem._reflect_tools = lambda _state: []
    mem._reflect_loop_opts = lambda: None

    lessons = mem.reflect_lessons(state, state.nodes[0], ["fp"])

    prompt = seen["prompt"]
    assert "What RAN but was NEVER MEASURED" in prompt
    assert "#1 improve ran, then FAILED its declaration (artifact_contract)" in prompt
    assert "final/model.safetensors" in prompt, "the model needs the error, not the bucket"
    # The METRIC claim is still refused: the number never appears, and #1 is not in the ranked table.
    assert "0.95" not in prompt
    assert "\nWhat worked (best first):\n#0 draft" in prompt
    assert "#1" not in prompt.split("What RAN but was NEVER MEASURED")[0]
    # …and the lesson is GROUNDED in it, so a later re-check that promotes the node to measured
    # re-derives this batch instead of leaving a stale conclusion in the shared store.
    assert lessons and 1 in lessons[0]["evidence"]
    assert "1" in (lessons[0]["evidence_sig"] or {})
