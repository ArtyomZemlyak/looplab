"""Real MLE-bench adapter (D1): official prepare/grade wiring, host-side grading, and an offline
end-to-end run scored by mle-bench's REAL grader.

These tests need the mle-bench package importable (`pip install -e mle-bench-src --no-deps` plus
pandas/scikit-learn) but NO network and NO Kaggle download: a tiny prepared competition dir with
the real spooky schema is built in a temp data-dir, so the real registry/grader/leaderboard run
against it. They auto-skip if mlebench isn't installed.
"""
from __future__ import annotations

import csv
from pathlib import Path

import anyio
import pytest

mlebench = pytest.importorskip("mlebench")

from looplab.adapters.kaggle_dl import KaggleAuthError, KaggleRulesError, resolve_token  # noqa: E402
from looplab.adapters.mlebench_grade import grade, grade_in_subprocess  # noqa: E402
from looplab.adapters.mlebench_real import MLEBenchRealTask, competition_meta  # noqa: E402
from looplab.engine.orchestrator import Engine  # noqa: E402
from looplab.search.policy import GreedyTree  # noqa: E402
from looplab.runtime.sandbox import SubprocessSandbox  # noqa: E402

SPOOKY = "spooky-author-identification"

# A tiny spooky dataset with a clean per-class word signal so Naive Bayes predicts the held-out
# rows correctly (low log-loss) — enough to exercise the real grader end-to-end.
_TRAIN = [
    ("t1", "the dread shadow horror eldritch fear", "HPL"),
    ("t2", "horror dread eldritch shadow ancient fear", "HPL"),
    ("t3", "love heart sorrow tears gentle soul", "MWS"),
    ("t4", "sorrow heart love gentle tears soul", "MWS"),
    ("t5", "ratiocination detective analysis logic clue method", "EAP"),
    ("t6", "logic analysis detective clue ratiocination method", "EAP"),
]
_TEST = [
    ("e1", "eldritch horror dread shadow fear"),      # HPL
    ("e2", "love sorrow heart gentle tears"),          # MWS
    ("e3", "detective logic clue analysis method"),    # EAP
]
_ANSWER_CLASS = {"e1": "HPL", "e2": "MWS", "e3": "EAP"}
CLASSES = ["EAP", "HPL", "MWS"]


def _write_csv(path: Path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


@pytest.fixture()
def prepared_spooky(tmp_path) -> str:
    """Build a prepared spooky competition (public + private/answers) under a temp data-dir; return
    that data-dir. Mirrors what `mlebench prepare` would create, minus the download."""
    data_dir = tmp_path / "mle-data"
    base = data_dir / SPOOKY / "prepared"
    _write_csv(base / "public" / "train.csv", ["id", "text", "author"], _TRAIN)
    _write_csv(base / "public" / "test.csv", ["id", "text"], _TEST)
    _write_csv(base / "public" / "sample_submission.csv", ["id"] + CLASSES,
               [[i] + [1 / 3, 1 / 3, 1 / 3] for i, _ in _TEST])
    (base / "public" / "description.md").write_text("Spooky test fixture.", encoding="utf-8")
    # private answers: one-hot of the true class per test id
    ans = [[i] + [1 if c == _ANSWER_CLASS[i] else 0 for c in CLASSES] for i, _ in _TEST]
    _write_csv(base / "private" / "test.csv", ["id"] + CLASSES, ans)
    competition_meta.cache_clear()
    return str(data_dir)


# --- token / downloader (no network) --------------------------------------------------------------

def test_resolve_token_from_env(monkeypatch):
    monkeypatch.setenv("LOOPLAB_KAGGLE_TOKEN", "KGAT_test123")
    assert resolve_token() == "KGAT_test123"
    assert resolve_token("explicit_wins") == "explicit_wins"


def test_resolve_token_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("LOOPLAB_KAGGLE_TOKEN", raising=False)
    monkeypatch.delenv("KAGGLE_KEY", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)   # no ~/.kaggle/kaggle.json
    with pytest.raises(KaggleAuthError):
        resolve_token()


def test_rules_error_message():
    e = KaggleRulesError(SPOOKY)
    assert SPOOKY in str(e) and "rules" in str(e).lower() and e.competition_id == SPOOKY


# --- adapter wiring ------------------------------------------------------------------------------

def test_meta_and_direction(prepared_spooky):
    task = MLEBenchRealTask(competition=SPOOKY, data_dir=prepared_spooky)
    assert task.id == SPOOKY
    assert task.direction == "min"                       # log-loss: lower is better
    assert "spooky" in task.goal.lower()


def test_assets_expose_public_not_answers(prepared_spooky):
    task = MLEBenchRealTask(competition=SPOOKY, data_dir=prepared_spooky)
    a = task.assets()
    assert "train.csv" in a and "test.csv" in a and "sample_submission.csv" in a
    assert "description.md" in a
    # the held-out answers must never be exposed to the candidate
    assert not any("private" in name or name == "answers.csv" for name in a)
    assert "HPL" in a["train.csv"]


def test_host_grader_spec(prepared_spooky):
    g = MLEBenchRealTask(competition=SPOOKY, data_dir=prepared_spooky).host_grader()
    assert g["kind"] == "mlebench" and g["competition"] == SPOOKY
    assert g["submission"] == "submission.csv" and g["scorer"] == "multi-class-log-loss"
    assert "labels" not in g                              # answers held in the data dir, not here


def test_unprepared_competition_raises(tmp_path):
    competition_meta.cache_clear()
    task = MLEBenchRealTask(competition=SPOOKY, data_dir=str(tmp_path / "empty"))
    with pytest.raises(RuntimeError, match="not prepared"):
        task.assets()


# --- real grader -----------------------------------------------------------------------------------

def test_real_grader_scores_submission(prepared_spooky, tmp_path):
    # a perfect one-hot submission -> log-loss ~ 0 (clipped); a uniform one -> ln(3) ~ 1.0986
    perfect = tmp_path / "perfect.csv"
    _write_csv(perfect, ["id"] + CLASSES,
               [[i] + [1 if c == _ANSWER_CLASS[i] else 0 for c in CLASSES] for i, _ in _TEST])
    rep = grade(SPOOKY, perfect, prepared_spooky)
    assert rep["score"] is not None and rep["score"] < 1e-3 and rep["valid_submission"]

    uniform = tmp_path / "uniform.csv"
    _write_csv(uniform, ["id"] + CLASSES, [[i, 1 / 3, 1 / 3, 1 / 3] for i, _ in _TEST])
    rep2 = grade(SPOOKY, uniform, prepared_spooky)
    assert abs(rep2["score"] - 1.0986) < 1e-2


def test_grader_subprocess_matches_inprocess(prepared_spooky, tmp_path):
    sub = tmp_path / "s.csv"
    _write_csv(sub, ["id"] + CLASSES, [[i, 1 / 3, 1 / 3, 1 / 3] for i, _ in _TEST])
    metric, report = grade_in_subprocess(SPOOKY, sub, prepared_spooky, timeout=120)
    assert metric is not None and report is not None
    assert abs(metric - grade(SPOOKY, sub, prepared_spooky)["score"]) < 1e-9


def test_invalid_submission_scores_none(prepared_spooky, tmp_path):
    bad = tmp_path / "bad.csv"
    _write_csv(bad, ["id"] + CLASSES, [["e1", 0.5, 0.5, 0.5]])    # wrong length + rows don't sum to 1
    rep = grade(SPOOKY, bad, prepared_spooky)
    assert rep["score"] is None and not rep["valid_submission"]


# --- end-to-end through the engine (offline baseline, host-graded) --------------------------------

def test_offline_engine_run_host_graded(prepared_spooky, tmp_path):
    task = MLEBenchRealTask(competition=SPOOKY, data_dir=prepared_spooky)
    researcher, developer = task.build_roles()
    engine = Engine(tmp_path / "run", task=task, researcher=researcher, developer=developer,
                    sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=4))
    state = anyio.run(engine.run)
    assert state.finished
    best = state.best()
    assert best is not None and best.metric is not None
    assert best.metric < 1.0986                          # NB beats the uniform baseline (ln 3)
    assert state.host_grading and state.host_grading["scorer"] == "multi-class-log-loss"
    # the candidate workdir has submission.csv and NO answer key
    nd0 = tmp_path / "run" / "nodes" / "node_0"
    assert (nd0 / "submission.csv").exists()
    # the held-out answer key must never be materialized into the candidate workdir (test.csv here is
    # the public FEATURES file, which is expected; the private answers live only in the data dir)
    assert not (nd0 / "answers.csv").exists()
    assert not (nd0 / "private").exists()
    # the official report (medals/above_median) is persisted as a per-node artifact (NOT in
    # extra_metrics, which is a numeric dict the UI treats as Pareto objectives)
    import json as _j
    rep_path = nd0 / "mlebench_report.json"
    assert rep_path.exists()
    rep = _j.loads(rep_path.read_text(encoding="utf-8"))
    assert rep["competition_id"] == SPOOKY and "above_median" in rep


# --- the other two competitions: baseline -> real grader, end to end -------------------------------

INSULTS = "detecting-insults-in-social-commentary"
NOMAD = "nomad2018-predict-transparent-conductors"


def _run_engine(task, out):
    r, d = task.build_roles()
    eng = Engine(out, task=task, researcher=r, developer=d, sandbox=SubprocessSandbox(),
                 policy=GreedyTree(n_seeds=2, max_nodes=3))
    return anyio.run(eng.run)


@pytest.fixture()
def prepared_insults(tmp_path) -> str:
    data_dir = tmp_path / "mle-data"
    base = data_dir / INSULTS / "prepared"
    train = [("1", "2012", "you are an idiot moron stupid fool"),
             ("1", "2012", "stupid idiot you absolute fool"),
             ("0", "2012", "thanks great point well said friend"),
             ("0", "2012", "i agree nice comment well said")]
    test = [("2012", "idiot stupid moron fool"), ("2012", "great well said thanks friend")]
    _write_csv(base / "public" / "train.csv", ["Insult", "Date", "Comment"], train)
    _write_csv(base / "public" / "test.csv", ["Date", "Comment"], test)
    _write_csv(base / "public" / "sample_submission_null.csv", ["Insult", "Date", "Comment"],
               [[0, d, c] for d, c in test])
    (base / "public" / "description.md").write_text("Insults test fixture.", encoding="utf-8")
    _write_csv(base / "private" / "test.csv", ["Comment", "Insult"],
               [["idiot stupid moron fool", 1], ["great well said thanks friend", 0]])
    competition_meta.cache_clear()
    return str(data_dir)


@pytest.fixture()
def prepared_nomad(tmp_path) -> str:
    data_dir = tmp_path / "mle-data"
    base = data_dir / NOMAD / "prepared"
    cols = ["id", "x1", "x2", "x3"]
    tgt = ["formation_energy_ev_natom", "bandgap_energy_ev"]

    def targets(x1, x2, x3):           # clean non-negative linear signal ridge can recover
        return round(0.10 * x1 + 0.01 * x2, 6), round(0.20 * x2 + 0.05 * x3, 6)

    train = []
    for i in range(8):
        x1, x2, x3 = float(i + 1), float((i * 2) % 7 + 1), float((i * 3) % 5 + 1)
        f, b = targets(x1, x2, x3)
        train.append([i + 1, x1, x2, x3, f, b])
    test = [[1, 2.0, 3.0, 1.0], [2, 5.0, 1.0, 4.0]]
    _write_csv(base / "public" / "train.csv", cols + tgt, train)
    _write_csv(base / "public" / "test.csv", cols, test)
    _write_csv(base / "public" / "sample_submission.csv", ["id"] + tgt,
               [[r[0], 0.1, 1.0] for r in test])
    (base / "public" / "description.md").write_text("Nomad test fixture.", encoding="utf-8")
    ans = [[r[0]] + list(targets(r[1], r[2], r[3])) for r in test]
    _write_csv(base / "private" / "test.csv", ["id"] + tgt, ans)
    competition_meta.cache_clear()
    return str(data_dir)


def test_insults_direction_and_run(prepared_insults, tmp_path):
    task = MLEBenchRealTask(competition=INSULTS, data_dir=prepared_insults)
    assert task.direction == "max"                       # AUC: higher is better
    assert task.host_grader()["scorer"] == "auc-roc"
    state = _run_engine(task, tmp_path / "run")
    best = state.best()
    assert best is not None and best.metric is not None and 0.5 <= best.metric <= 1.0
    assert (tmp_path / "run" / "nodes" / "node_0" / "submission.csv").exists()


def test_nomad_direction_and_run(prepared_nomad, tmp_path):
    task = MLEBenchRealTask(competition=NOMAD, data_dir=prepared_nomad)
    assert task.direction == "min"                       # RMSLE: lower is better
    assert task.host_grader()["scorer"] == "mean-column-wise-rmsle"
    state = _run_engine(task, tmp_path / "run")
    best = state.best()
    assert best is not None and best.metric is not None and best.metric >= 0.0


def test_llm_brief_uses_runtime_caps(prepared_nomad):
    # When the engine threads in a capability sentence (auto-install on), it lands in the developer
    # brief verbatim so the agent knows it may use torch/etc. — instead of the hardcoded sklearn-only
    # contract that made it downgrade a neural-net idea (the bug under investigation).
    task = MLEBenchRealTask(competition=NOMAD, data_dir=prepared_nomad)

    class _C:  # fake client; LLMResearcher/LLMDeveloper only store it at construction
        pass

    _, dev = task.llm_roles(_C(), runtime_caps="SENTINEL_CAPS torch xgboost auto-installed")
    assert "SENTINEL_CAPS" in dev.brief and "torch" in dev.brief
    # No caps threaded (unit/legacy path) -> conservative contract, byte-compatible with before.
    _, dev2 = task.llm_roles(_C())
    assert "scikit-learn" in dev2.brief and "CPU only, no GPU/network" in dev2.brief
