"""M2 task fingerprinting + M3 lessons-from-failures (this session, Phase 4).

Cross-run memory used to warm-start only the EXACT same task_id, and remembered only the winner.
Now: (M2) a task fingerprint lets a SIMILAR-but-new task retrieve priors by content overlap, and
(M3) run-end lessons include NEGATIVE results (tested/abandoned hypotheses + the dominant failure
reason) so a later run is steered away from known dead ends. All offline."""
from __future__ import annotations

import tempfile
from pathlib import Path

import anyio
import orjson

from looplab.engine.memory import fingerprint_similarity, task_fingerprint
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.adapters.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


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


def _write_lessons(mem: Path, rows):
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "lessons.jsonl").write_text("\n".join(orjson.dumps(x).decode() for x in rows) + "\n")


def test_similar_task_retrieves_lessons_including_negatives(tmp_path):
    mem = tmp_path / "mem"
    fp_reg = task_fingerprint("regression", "min", "select polynomial degree and ridge lambda CV MSE",
                              metric="mse", param_names=["degree", "lam"])
    fp_cls = task_fingerprint("classification", "max", "tune a classifier", metric="acc")
    _write_lessons(mem, [
        {"task_id": "reg_A", "fingerprint": fp_reg, "kind": "regression",
         "statement": "a degree-8 polynomial overfits badly", "outcome": "tested",
         "delta": -0.05, "confidence": 0.5},
        {"task_id": "reg_A", "fingerprint": fp_reg, "kind": "regression",
         "statement": "ridge lambda near 1.0 with degree 3 works", "outcome": "supported",
         "delta": 0.12, "confidence": 0.7},
        {"task_id": "other", "fingerprint": fp_cls, "kind": "classification",
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
