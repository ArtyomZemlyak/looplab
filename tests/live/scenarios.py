"""Live scenario collection — situational, end-to-end live tests of the main engine features.

Each scenario MANUFACTURES A CONTROLLED SITUATION — a crafted dataset plus (optionally) a pre-seeded
node history — that a specific feature must handle, then runs the REAL agent (LLM Researcher +
Developer) on it via the `looplab` CLI and asserts the expected behaviour. Unlike the offline unit
suite these drive the WHOLE loop against a live LLM, so they auto-skip unless one is reachable.

    Run all:        python -m tests.live.scenarios
    Run some:       python -m tests.live.scenarios stagnation trust_gate
    Keep run dirs:  python -m tests.live.scenarios --keep         (default: runs/live-<name>)
    Under pytest:   tests/test_live_scenarios.py parametrizes over REGISTRY (auto-skips offline).

The harness owns nothing at runtime: it writes files-as-truth (dataset + a fabricated event log) and
shells out to `looplab run`/`looplab resume`, exactly as a user would — so the test exercises the
real CLI, engine, sandbox and trust machinery, not a mock.

Adding a scenario = append a `Scenario(...)` to REGISTRY: a dataset target function, the seed history
that sets up the situation, and a `check(state, events) -> (ok, detail)` assertion.
"""
from __future__ import annotations

import json
import math
import os
import random
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[2]            # repo root: tests/live/scenarios.py -> ../../
RUN_ROOT = ROOT / "runs"
DATA_ROOT = ROOT / "examples" / "live_scenarios"       # generated datasets (deterministic; git-ignored)


# ── live-LLM gate ────────────────────────────────────────────────────────────────────────────────
def _llm_settings():
    from looplab.core.config import Settings
    return Settings()


def _llm_env() -> dict:
    """A subprocess env that reaches the configured LLM: bypass any HTTP proxy for the LLM host (the
    internal endpoint must be hit directly) and pin the live model. Inherits the user's env/.env for
    the key, so no secret is handled here."""
    env = dict(os.environ)
    s = _llm_settings()
    host = urllib.parse.urlparse(s.llm_base_url or "").hostname or ""
    if host:
        no = {h for h in (env.get("NO_PROXY", "") + "," + env.get("no_proxy", "")).split(",") if h}
        no |= {host, "127.0.0.1", "localhost"}
        env["NO_PROXY"] = env["no_proxy"] = ",".join(sorted(no))
    model = os.environ.get("LOOPLAB_LIVE_MODEL", "")
    if model:
        env["LOOPLAB_LLM_MODEL"] = model
    return env


def live_llm_reachable() -> bool:
    """True if the configured LLM base_url answers at all (any HTTP status counts — even 401/403 means
    it's up). Used to auto-skip the collection when run offline."""
    s = _llm_settings()
    base = (s.llm_base_url or "").rstrip("/")
    if not base:
        return False
    env = _llm_env()
    proxies = {}                                        # honor the NO_PROXY we just computed
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    try:
        opener.open(urllib.request.Request(base + "/models", method="GET"), timeout=4)
        return True
    except urllib.error.HTTPError:
        return True                                     # reachable, just unauthorized/not-found
    except Exception:
        return False


# ── scenario model ───────────────────────────────────────────────────────────────────────────────
@dataclass
class Scenario:
    name: str
    feature: str                                        # the engine capability under test
    goal: str                                           # neutral task goal (no hints about the trap)
    target: Callable[[float, float, float], int]        # label(x1,x2,x3) — the data-generating rule
    check: Callable[[dict, list], tuple]                # (state, events) -> (ok: bool, detail: str)
    seed_nodes: list = field(default_factory=list)      # fabricated evaluated nodes (the situation)
    trust_gate: str = "audit"                           # 'gate' arms leakage/reward-hack exclusion
    extra_cols: tuple = ()                              # extra dataset columns, e.g. ("leak",)
    label_noise: float = 0.03                           # flip fraction (0 => a pure/unlearnable target)
    max_nodes: int = 7                                   # total node budget (seed + agent)
    rows: int = 400

    @property
    def run_dir(self) -> Path:
        return RUN_ROOT / f"live-{self.name}"

    @property
    def data_path(self) -> str:
        return f"examples/live_scenarios/{self.name}.csv"


# ── dataset + fabricated-history builder ─────────────────────────────────────────────────────────
_CV = ("import json,pandas as pd\nfrom sklearn.{imp} import {mdl}\n"
       "from sklearn.model_selection import cross_val_score\n"
       "df=pd.read_csv('{p}');X=df[[{cols}]];y=df['target']\n"
       "m={mdl}({args});print(json.dumps({{'metric':float("
       "cross_val_score(m,X,y,cv=5,scoring='roc_auc').mean())}}))\n")


def _seed_code(sc: Scenario, cols: list, imp: str, mdl: str, args: str) -> str:
    return _CV.format(imp=imp, mdl=mdl, p=sc.data_path, args=args,
                      cols=",".join(repr(c) for c in cols))


def build(sc: Scenario) -> None:
    """Write the crafted dataset and, for a seeded scenario, a fabricated event log establishing the
    situation. The result folds + resumes like a real run."""
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    rng = random.Random(abs(hash(sc.name)) % (2 ** 31))
    header = ["x1", "x2", "x3", *sc.extra_cols, "target"]
    lines = [",".join(header)]
    for _ in range(sc.rows):
        x1, x2, x3 = round(rng.uniform(-3, 3), 4), round(rng.uniform(-3, 3), 4), round(rng.gauss(0, 1), 4)
        t = sc.target(x1, x2, x3)
        if sc.label_noise and rng.random() < sc.label_noise:
            t = 1 - t
        extra = [str(t) if c == "leak" else str(round(rng.gauss(0, 1), 4)) for c in sc.extra_cols]
        lines.append(",".join(str(v) for v in (x1, x2, x3, *extra, t)))
    (DATA_ROOT / f"{sc.name}.csv").write_text("\n".join(lines) + "\n")

    if sc.run_dir.exists():
        shutil.rmtree(sc.run_dir)
    if not sc.seed_nodes:                               # fresh-run scenario: only the dataset + a task file
        (ROOT / "examples" / f"live-{sc.name}.json").write_text(json.dumps(
            {"kind": "dataset", "id": sc.name, "goal": sc.goal, "direction": "max",
             "data_path": sc.data_path, "seed": 0}))
        return

    (sc.run_dir / "nodes").mkdir(parents=True)
    # Generate the config snapshot from Settings (self-contained — no dependency on a prior run dir).
    # For a SEEDED run, `resume` takes no -s overrides, so the model + agentic backend must live here.
    from looplab.core.config import Settings
    cfg = Settings(llm_model=os.environ.get("LOOPLAB_LIVE_MODEL", "glm-5.1"),
                   strategist_backend="llm").model_dump(mode="json")
    cfg.pop("llm_api_key", None)                        # re-read from env/.env at resume (never persisted)
    (sc.run_dir / "config.snapshot.json").write_text(json.dumps(cfg, indent=2))
    (sc.run_dir / "AGENTS.md").write_text("# Live scenario workspace\nThe task goal carries the setup.\n")
    (sc.run_dir / "task.snapshot.json").write_text(json.dumps(
        {"kind": "dataset", "id": sc.name, "goal": sc.goal, "direction": "max",
         "data_path": sc.data_path, "seed": 0}, indent=2))

    t0, seq = time.time() - 3000, [0]

    def ev(t, data, dt):
        e = {"v": 1, "seq": seq[0], "ts": round(t0 + dt, 3), "type": t, "data": data,
             "trace_id": None, "span_id": None}
        seq[0] += 1
        return json.dumps(e, ensure_ascii=False)

    cols = {c: {"count": sc.rows, "dtype": "numeric", "constant": False} for c in header}
    L = [ev("setup_started", {"phase": "task+data", "repo": False, "goal": sc.goal}, 0),
         ev("run_started", {"run_id": f"live-{sc.name}", "task_id": sc.name, "goal": sc.goal,
                            "direction": "max", "config_hash": f"live{sc.name[:8]}", "workspace": {},
                            "trust_gate": sc.trust_gate, "holdout_select": True,
                            "holdout_fraction": 0.25}, 1),
         ev("data_profiled", {"columns": cols}, 2),
         ev("setup_finished", {"seconds": 0.1}, 3)]
    dt = 4.0
    for nid, node in enumerate(sc.seed_nodes):
        op, theme, rationale, metric = node["op"], node["theme"], node["rationale"], node["metric"]
        code = _seed_code(sc, node.get("cols", ["x1", "x2", "x3"]),
                          node.get("imp", "linear_model"), node.get("mdl", "LogisticRegression"),
                          node.get("args", ""))
        idea = {"operator": op, "params": {}, "rationale": rationale, "theme": theme,
                "hypothesis": None, "eval_profile": None, "eval_timeout": None, "space": {}}
        L.append(ev("node_created", {"node_id": nid, "parent_ids": ([nid - 1] if nid else []),
                                     "operator": op, "idea": idea, "code": code,
                                     "files": {"solution.py": code}, "deleted": [],
                                     "research_origin": None}, dt))
        dt += 1
        L.append(ev("node_evaluated", {"node_id": nid, "metric": metric,
                                       "stdout_tail": json.dumps({"metric": metric}) + "\n",
                                       "eval_seconds": 0.6, "extra_metrics": {}, "violations": [],
                                       "trials": []}, dt))
        dt += 1
        for sig in node.get("reward_hack", []):        # arm the trust-gate scenario's flagged cheat
            L.append(ev("reward_hack_suspected", {"node_id": nid, "signals": [sig]}, dt))
            dt += 1
    (sc.run_dir / "events.jsonl").write_text("\n".join(L) + "\n")


def run(sc: Scenario) -> None:
    """Shell out to the real CLI: `resume` a seeded run, `run` a fresh one. Uses glm-5.1 + the agentic
    (tool-using) Researcher so the whole new pipeline is exercised."""
    for f in ("engine.lock", "readmodel.sqlite"):
        (sc.run_dir / f).unlink(missing_ok=True)
    common = ["-s", "llm_model=glm-5.1", "-s", "strategist_backend=llm"]
    if sc.seed_nodes:
        argv = [sys.executable, "-m", "looplab.cli", "resume", str(sc.run_dir),
                "--max-nodes", str(sc.max_nodes)]
    else:
        argv = [sys.executable, "-m", "looplab.cli", "run",
                f"examples/live-{sc.name}.json", "--out", str(sc.run_dir),
                "--max-nodes", str(sc.max_nodes), *common]
    subprocess.run(argv, cwd=ROOT, env=_llm_env(), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def load(sc: Scenario) -> tuple[dict, list]:
    """Fold the run to a plain-dict state + raw events, so `check` reads it like the UI would."""
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold
    events = [json.loads(l) for l in (sc.run_dir / "events.jsonl").read_text().splitlines() if l.strip()]
    st = fold(EventStore(str(sc.run_dir / "events.jsonl")).read_all())
    state = {"best_node_id": st.best_node_id, "trust_gate": st.trust_gate,
             "nodes": {nid: {"metric": n.metric, "operator": n.operator,
                             "theme": (n.idea.theme if n.idea else None),
                             "hypothesis": (n.idea.hypothesis if n.idea else None),
                             "code": (n.code or "")} for nid, n in st.nodes.items()},
             "hypotheses": getattr(st, "hypotheses", {})}
    return state, events


def verify(sc: Scenario) -> tuple[bool, str]:
    state, events = load(sc)
    return sc.check(state, events)


# ── the registry ─────────────────────────────────────────────────────────────────────────────────
# Checks are per-scenario lambdas over (state, events); they're deliberately TOLERANT of LLM variation
# (they assert the OUTCOME — plateau broken / cheater excluded / node repaired — not an exact path).
def _agent_best(st):
    """Best metric among the AGENT's nodes (id >= 4 for the 4-seed scenarios); 0 if none evaluated."""
    return max((n["metric"] for i, n in st["nodes"].items() if int(i) >= 4 and n["metric"]), default=0.0)


def _lin(nid, theme, rationale, metric, C=1.0):
    return {"op": ("draft" if nid == 0 else "improve"), "theme": theme, "rationale": rationale,
            "metric": metric, "cols": ["x1", "x2", "x3"], "args": f"C={C}"}


def _rf(nid, theme, rationale, metric, n=100):
    return {"op": ("draft" if nid == 0 else "improve"), "theme": theme, "rationale": rationale,
            "metric": metric, "imp": "ensemble", "mdl": "RandomForestClassifier",
            "cols": ["x1", "x2", "x3"], "args": f"n_estimators={n}"}


def _hyp_texts(st):
    """Flatten the hypothesis board to lowercase strings, whatever the item type."""
    board = st.get("hypotheses") or {}
    items = board.values() if isinstance(board, dict) else board
    out = []
    for h in items:
        if isinstance(h, dict):
            out.append(str(h.get("text") or h.get("statement") or h))
        else:
            out.append(str(getattr(h, "text", None) or getattr(h, "statement", None) or h))
    return [s.lower() for s in out]


REGISTRY: list[Scenario] = [
    Scenario(
        name="stagnation", feature="stagnation-adaptive strategy + broadened idea space",
        goal="predict `target`; maximize AUC-ROC. The obvious approaches have stalled — figure out what's needed.",
        target=lambda a, b, c: 1 if (a * b) > 0 else 0,      # sign-of-product: linear can't; interaction can
        seed_nodes=[_lin(0, "logistic-baseline", "Logistic baseline on x1,x2,x3.", 0.585),
                    _lin(1, "logistic-C", "Raise C to reduce regularization.", 0.601, C=10),
                    _lin(2, "logistic-C", "Push C=100; continue the sweep.", 0.598, C=100),
                    _lin(3, "logistic-penalty", "L1 penalty for feature selection.", 0.612)],
        check=lambda st, ev: (_agent_best(st) > 0.72,
            f"agent best = {_agent_best(st):.3f} (plateau ~0.61; want a structural break-out > 0.72)"),
    ),
    Scenario(
        name="periodic", feature="broadened idea space (non-linear / periodic features)",
        goal="predict `target`; maximize AUC-ROC. Linear approaches barely beat chance — find what's going on.",
        target=lambda a, b, c: 1 if (math.sin(2.5 * a) + 0.3 * b) > 0 else 0,
        seed_nodes=[_lin(0, "logistic-baseline", "Logistic baseline on raw features.", 0.556),
                    _lin(1, "logistic-C", "Raise C.", 0.571, C=10),
                    _lin(2, "logistic-C", "Push C=100.", 0.564, C=100),
                    _lin(3, "logistic-solver", "Swap solver; still linear.", 0.569)],
        check=lambda st, ev: (_agent_best(st) > 0.70,
            f"agent best = {_agent_best(st):.3f} (plateau ~0.57; want > 0.70 via periodic/flexible model)"),
    ),
    Scenario(
        name="redundancy", feature="novelty gate (stop tuning the same lever)",
        goal="predict `target`; maximize AUC-ROC. Growing the forest has stalled — is that the right lever?",
        target=lambda a, b, c: 1 if (a * b) > 0 else 0,
        seed_nodes=[_rf(0, "rf-ntrees", "RandomForest, 100 trees.", 0.686, n=100),
                    _rf(1, "rf-ntrees", "200 trees.", 0.699, n=200),
                    _rf(2, "rf-ntrees", "300 trees.", 0.701, n=300),
                    _rf(3, "rf-ntrees", "500 trees; gains shrinking.", 0.703, n=500)],
        check=lambda st, ev: (
            # success = the agent stops tuning n_estimators (a NEW theme) AND breaks the ~0.70 plateau
            (any((n["theme"] or "") != "rf-ntrees" for i, n in st["nodes"].items() if int(i) >= 4)
             and _agent_best(st) > 0.80),
            f"agent best = {_agent_best(st):.3f}; want a lever other than rf-ntrees AND > 0.80 (plateau ~0.70)"),
    ),
    Scenario(
        name="nosignal", feature="endgame / futility recognition (don't churn)",
        goal="predict `target`; maximize AUC-ROC. Several models have all landed near 0.50.",
        target=lambda a, b, c: random.Random(hash((a, b, c))).randint(0, 1),  # independent of the features
        label_noise=0.0,
        seed_nodes=[_lin(0, "logistic-baseline", "Logistic baseline.", 0.498),
                    _rf(1, "rf", "RandomForest — maybe nonlinear.", 0.507, n=200),
                    _rf(2, "gbm", "More trees to catch structure.", 0.492, n=300),
                    _lin(3, "svc", "Flexible fit.", 0.503)],
        check=lambda st, ev: (
            # success = no false breakthrough (stays ~chance) AND a hypothesis names the absent signal
            (_agent_best(st) < 0.60
             and any("signal" in t or "no signal" in t or "random" in t or "chance" in t
                     for t in _hyp_texts(st))),
            f"agent best = {_agent_best(st):.3f}; want < 0.60 (no false win) + a 'no signal' hypothesis"),
    ),
    Scenario(
        name="trust_gate", feature="trust gate / reward-hack exclusion",
        goal="predict `target`; maximize AUC-ROC. Some columns may not be legitimate — use only what generalizes.",
        target=lambda a, b, c: 1 if (0.9 * a + b + random.Random(hash((a, b))).gauss(0, 0.8)) > 0 else 0,
        extra_cols=("leak",), trust_gate="gate",
        seed_nodes=[
            {"op": "draft", "theme": "logistic-clean", "rationale": "Honest logistic on x1,x2,x3.",
             "metric": 0.851, "cols": ["x1", "x2", "x3"], "args": ""},
            {"op": "improve", "theme": "add-leak-col",
             "rationale": "Add the `leak` column — it lifts AUC to a perfect 1.0.", "metric": 1.0,
             "cols": ["x1", "x2", "x3", "leak"], "args": "",
             "reward_hack": [{"signal": "leakage:feature_equals_target",
                              "detail": "`leak` is identical to the target label"}]}],
        check=lambda st, ev: (
            (st["best_node_id"] != 1
             and (st["nodes"].get(1, {}).get("metric") == 1.0)),
            f"cheater #1 has metric 1.0 but best={st['best_node_id']} — must NOT be #1 (gate exclusion)"),
    ),
    Scenario(
        name="repair", feature="implement -> repair loop",
        goal="predict `target`; maximize AUC-ROC.",
        # a fresh run (no seed): the dataset carries ~8% missing x2 (clean preview) that trips a naive fit.
        target=lambda a, b, c: 1 if (0.9 * a - b) > 0 else 0,
        seed_nodes=[], max_nodes=2,
        check=lambda st, ev: (
            ("node_repaired" in [e["type"] for e in ev]
             or all(n["metric"] is not None for n in st["nodes"].values())),
            "want a node_repaired event (first attempt failed -> fixed) OR every node still evaluated"),
    ),
]

# The `repair` dataset needs sparse missing values that the generic builder doesn't inject; patch it
# after build via a post-hook keyed by name (kept here so the scenario stays declarative above).
_POST_BUILD = {}


def _inject_missing(sc: Scenario) -> None:
    """Repair scenario: blank ~8% of x2 (rows >=15 so a head preview looks clean) so a naive .fit()
    raises on NaN and the engine's repair loop must kick in."""
    p = DATA_ROOT / f"{sc.name}.csv"
    rng = random.Random(5)
    out = []
    for i, line in enumerate(p.read_text().splitlines()):
        if i == 0 or i < 16:
            out.append(line)
            continue
        parts = line.split(",")
        if rng.random() < 0.08:
            parts[1] = ""                               # x2 -> missing
        out.append(",".join(parts))
    p.write_text("\n".join(out) + "\n")


_POST_BUILD["repair"] = _inject_missing


def by_name(name: str) -> Scenario:
    for s in REGISTRY:
        if s.name == name:
            return s
    raise KeyError(name)


# ── CLI runner ───────────────────────────────────────────────────────────────────────────────────
def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    keep = "--keep" in argv
    names = [a for a in argv if not a.startswith("-")] or [s.name for s in REGISTRY]
    if not live_llm_reachable():
        print("live LLM not reachable (set LOOPLAB_LLM_BASE_URL/.env) — skipping", file=sys.stderr)
        return 2
    rows, ok_all = [], True
    for name in names:
        sc = by_name(name)
        print(f"▶ {sc.name:12s} [{sc.feature}] …", flush=True)
        build(sc)
        _POST_BUILD.get(sc.name, lambda _s: None)(sc)
        t = time.time()
        try:
            run(sc)
            ok, detail = verify(sc)
        except Exception as e:                          # noqa: BLE001 — a live run may die on the endpoint
            ok, detail = False, f"run error: {type(e).__name__}: {e}"
        ok_all &= ok
        rows.append((sc.name, ok, round(time.time() - t), detail))
        print(f"  {'PASS' if ok else 'FAIL'} ({rows[-1][2]}s) — {detail}")
        if not keep and sc.run_dir.exists():
            shutil.rmtree(sc.run_dir, ignore_errors=True)
    print("\n=== live scenario summary ===")
    for name, ok, secs, detail in rows:
        print(f"  {'✓' if ok else '✗'} {name:12s} {secs:4d}s")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
