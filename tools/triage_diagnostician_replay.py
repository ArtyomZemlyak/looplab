"""Replay the SHIPPED failure diagnostician over `failure_triage.v1` and capture its answers.

    python tools/triage_diagnostician_replay.py --arm durable  -o cand.durable.jsonl
    python tools/triage_diagnostician_replay.py --arm widened  -o cand.widened.jsonl
    python -m looplab.judgebench score-triage --answers cand.durable.jsonl

WHY THIS IS NOT IN `looplab/judgebench/`. That package promises `score` makes no network call at
all, and this file's whole purpose is to make 95 of them. It is the producer of the `--answers`
file the bench already accepts, so the bench stays offline and the paid arm is a thing you have to
type.

WHAT IT DRIVES, and why nothing here re-implements a prompt. The diagnostician IS the triage call
(`engine/failure_diagnosis.py`'s cost argument), so this harness drives
`engine/crash_repair.py::CrashRepairMixin._triage_crash` — the shipped method, with its `[failure
kind: ...]` tag, its `_TRIAGE_REASK_LIMIT` re-ask and its fixed-key verdict rebuild — bound to a
stub that carries only what that method reads off an Engine (`researcher`, `tracer`). The seam it
calls is the shipped `agents/unified_agent.py::UnifiedAgent.triage_crash`, and the classification is
read out with the shipped `failure_diagnosis.diagnosed_failure_reason` /`coerce_evidence` /
`evidence_citation_resolves`. A number produced by a lookalike measures the lookalike.

THE TWO ARMS ARE THE BENCH'S OWN TWO WINDOWS, deliberately, so a score here is directly comparable
to `--arm frozen` and `--arm frozen-widened`:

  durable  `evidence.at_classification` — the ~500-char durable stderr tail, the failed stage's
           exit code and the attempt's stage rows. What the ENGINE held at the instant it
           classified, and what a diagnostician that never looks would be answering from.
  widened  that tail PLUS `evidence.on_demand` — the `read_log` outputs the triage agent really
           pulled at that moment and the stage log that is provably paired to this attempt. What
           looking actually RETURNED, on the day.

Both go through `judgebench/triage_score.py::replay_result`, so the evidence window is the bench's
definition and not a second one written here.

WHAT THIS HARNESS CANNOT REPRODUCE, said out loud because a limit that lives in a commit message is
a limit nobody reads:

  * NO LIVE TOOLS. `tools=None` and `pilot_tools=None`, so the diagnostician answers from the
    prompt and spends none of its `DIAGNOSIS_CODE_LOOK_TURNS`. The alternative was to root
    `diagnosis_tools` at the node workdir on disk — but a workdir holds the LAST attempt's files
    and every re-run truncates the stage logs, so for any row that is not a node's final attempt
    those bytes are a LATER attempt's output. The corpus already solved this problem once
    (`stage_log.paired_to_this_attempt`), and the widened arm is that solution: period-correct
    evidence, spliced, instead of anachronistic evidence, fetched. The cost this measures is
    therefore a FLOOR in provider calls and the accuracy a floor in what looking could add.
  * The prompt's brief, standing hints, repair history, `stages_passed` and `attempts_left` ARE
    reconstructed, and every one from the durable event log at the row's own `seq` (`events/
    replay.py::fold`, `engine/evaluate.py::_durable_repair_ledger`, the run's own
    `config.snapshot.json`). Nothing here is invented and nothing is read from the recorded reason.

DO NOT TUNE ANYTHING TO THE CORPUS. There is no prompt override switch in this file on purpose.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
# `python tools/<this>.py` puts `tools/` on sys.path, not the repo root, and this tree is run from a
# checkout rather than an install. Prepend the root so the SHIPPED code under test is this
# checkout's — never some other looplab that happens to be importable.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from looplab.judgebench import triage_corpus, triage_score  # noqa: E402
DEFAULT_RUNS_ROOT = REPO_ROOT / "runs"

ARMS = ("durable", "widened")

_CREDENTIAL_FIELDS = ("llm_api_key", "llm_api_key_base_url")


def credential_pair(env_file, runs_root) -> tuple[dict, str]:
    """ONE WHOLE credential tier, in `serve/settings_store.py::_effective_secret_values`' own order
    and with its own rule: a tier is selected entire, never merged half from one source and half
    from another, because the key and the endpoint it was issued for are one atomic value.

    Process env, then a dotenv file, then the run root's `secrets.json`. The dotenv path is EXPLICIT
    here where the store reads `.env` from the process CWD — this harness runs from a worktree and
    the operator's pair lives beside `runs/`, and silently picking up a different tree's `.env`
    would be exactly the mixing the store refuses.
    """
    names = {field: "LOOPLAB_%s" % field.upper() for field in _CREDENTIAL_FIELDS}
    if any(name in os.environ for name in names.values()):
        return {f: os.environ.get(n, "") for f, n in names.items()}, "environment"
    if env_file is not None and Path(env_file).is_file():
        from dotenv import dotenv_values
        raw = {str(k).upper(): ("" if v is None else str(v))
               for k, v in dotenv_values(str(env_file)).items()}
        if any(name in raw for name in names.values()):
            return ({f: raw.get(n, "") for f, n in names.items()}, "dotenv:%s" % env_file)
    from looplab.serve.settings_store import SettingsStore
    return SettingsStore(Path(runs_root)).load_secrets(), "stored"


# --------------------------------------------------------------------------------------------
# The engine stub. Every attribute below is one `_triage_crash`/`_ask_triage` actually reads; the
# point of the stub is that the METHOD is the shipped one, not that the Engine is.

class _NullTracer:
    @contextmanager
    def span(self, *_a, **_kw):
        yield None


class _StubEngine:
    """Carries `_triage_crash` from the shipped mixin over the two attributes it reads."""

    from looplab.engine.crash_repair import CrashRepairMixin as _Mixin
    _triage_crash = _Mixin._triage_crash
    _ask_triage = _Mixin._ask_triage
    del _Mixin

    def __init__(self, researcher, inline_repair_attempts: int):
        self.researcher = researcher
        self.tracer = _NullTracer()
        self._inline_repair_attempts = inline_repair_attempts
        self._memo_verdict_cue = False


# --------------------------------------------------------------------------------------------
# Per-run context: settings from the run's OWN snapshot, events for the period-correct prompt.

class RunContext:
    """One run's replay inputs. Built once, read by every row of that run."""

    def __init__(self, run_dir: Path, secrets: dict):
        from looplab.cli import load_run_settings
        from looplab.core.models import Event
        from looplab.serve.settings_store import SettingsStore

        self.run_dir = run_dir
        settings = load_run_settings(run_dir, strict=True)
        # The credential pair, applied through the shipped store rather than assembled here.
        self.settings = SettingsStore._with_secret_pair(settings, secrets)
        self.events = [Event(**json.loads(line))
                       for line in (run_dir / "events.jsonl").open("r", encoding="utf-8")
                       if line.strip()]
        self._fold_lock = threading.Lock()
        self._fold_cache: dict[int, object] = {}

    def state_at(self, seq: int):
        """The RunState as the log had it just BEFORE this classification's event."""
        from looplab.events.replay import fold
        with self._fold_lock:
            if seq not in self._fold_cache:
                self._fold_cache[seq] = fold([e for e in self.events if e.seq < seq])
            return self._fold_cache[seq]

    def events_before(self, seq: int):
        return [e for e in self.events if e.seq < seq]

    def new_agent(self):
        """A fresh pilot client (own CostAccountant, so per-row cost is per-row) behind the
        shipped `UnifiedAgent`. `pilot_tools=None` — see the module docstring."""
        from looplab.agents.agent import loop_opts_from_settings
        from looplab.agents.unified_agent import UnifiedAgent
        from looplab.core.llm import make_llm_client, make_llm_client_for
        client = make_llm_client_for(self.settings, role="pilot", factory=make_llm_client)
        agent = UnifiedAgent(
            researcher=None, developer=None, pilot_client=client, pilot_tools=None,
            prompts=None,
            agent_max_turns=getattr(self.settings, "agent_max_turns", 0),
            agent_time_budget_s=getattr(self.settings, "agent_time_budget_s", 0.0),
            loop_opts=loop_opts_from_settings(self.settings))
        return agent, client


# --------------------------------------------------------------------------------------------

def workdir_for(row: dict, runs_root: Path) -> Path:
    """The node workdir the engine would have re-resolved the citation against."""
    prov = row.get("provenance") or {}
    return runs_root / str(prov.get("run")) / "nodes" / ("node_%s" % prov.get("node_id"))


def diagnose_row(row: dict, ctx: RunContext, arm: str, runs_root: Path) -> dict:
    """One row through the shipped diagnostician. Returns the answer plus its measured cost."""
    from looplab.engine.evaluate import (_durable_repair_ledger, _effective_repair_cap,
                                         _repair_attempts_left, _JUDGE_HISTORY_ROWS)
    from looplab.engine.failure_diagnosis import (DIAGNOSABLE_ENGINE_REASONS, coerce_evidence,
                                                  diagnosed_failure_reason, engine_observed_facts,
                                                  evidence_citation_resolves)
    from looplab.engine.triage import _failure_reason

    prov = row.get("provenance") or {}
    at = ((row.get("evidence") or {}).get("at_classification")) or {}
    case_id = row.get("case_id")
    res = triage_score.replay_result(row, widened=(arm == "widened"))
    if res is None:
        # No `res` can be rebuilt from this row at all (the bench's own `live` arm answers None
        # here too). Unanswered is the honest record; it is NOT a wrong answer.
        return {"case_id": case_id, "reason": None, "engine_reason": None,
                "reason_source": None, "skipped": "no_replayable_result"}
    deterministic = _failure_reason(res)
    if deterministic not in DIAGNOSABLE_ENGINE_REASONS:
        # ENGINE-FINAL: `diagnosed_failure_reason` returns it unchanged and never looks at a
        # verdict, so production would not spend the classification on this row either. No call.
        reason, source = diagnosed_failure_reason(deterministic, None)
        return {"case_id": case_id, "reason": reason, "engine_reason": deterministic,
                "reason_source": source, "asked": False, "calls": 0, "seconds": 0.0}

    seq = int(prov.get("seq") or 0)
    node_id = prov.get("node_id")
    attempt = int(prov.get("attempt") or 1)
    state = ctx.state_at(seq)
    node = (state.nodes or {}).get(node_id)
    if node is None:
        node = SimpleNamespace(id=node_id, code="")
    # `Node.attempt` IS the generation (`core/models.py`: "the field keeps its original `attempt`
    # name for projection/backward compatibility"), which is what `_evaluate` binds at line 1591 and
    # what the durable ledgers key on. Not `node.generation`, which does not exist.
    generation = int(getattr(node, "attempt", 0) or 0)
    _, ledger_rows, _ = _durable_repair_ledger(ctx.events_before(seq), node_id, generation)
    cap = _effective_repair_cap(int(getattr(ctx.settings, "inline_repair_attempts", 0) or 0))

    err = str(res.stderr or "")
    agent, client = ctx.new_agent()
    engine = _StubEngine(agent, int(getattr(ctx.settings, "inline_repair_attempts", 0) or 0))
    acct = client.accountant
    t0 = time.time()
    verdict = engine._triage_crash(
        state, node, err, attempt, reason=deterministic,
        repair_log=ledger_rows[-_JUDGE_HISTORY_ROWS:],
        depth=at.get("stages_passed"),
        attempts_left=_repair_attempts_left(attempt - 1, cap),
        log_tools=None,
        engine_facts=engine_observed_facts(res))
    seconds = time.time() - t0

    reason, source = diagnosed_failure_reason(deterministic, verdict)
    evidence = coerce_evidence(verdict)
    workdir = workdir_for(row, runs_root)
    resolved = evidence_citation_resolves(evidence, workdir)
    return {
        "case_id": case_id,
        "reason": reason,
        "engine_reason": deterministic,
        "reason_source": source,
        "asked": True,
        "action": verdict.get("action"),
        "failure_kind_raw": verdict.get("failure_kind"),
        "evidence": evidence,
        "evidence_resolved": resolved,
        "workdir_exists": workdir.is_dir(),
        "rationale": str(verdict.get("rationale", ""))[:400],
        "calls": int(getattr(acct, "calls", 0)),
        "prompt_tokens": int(getattr(acct, "prompt_tokens", 0)),
        "completion_tokens": int(getattr(acct, "completion_tokens", 0)),
        "cost": float(getattr(acct, "spent", 0.0)),
        "priced_calls": int(getattr(acct, "priced_calls", 0)),
        "peak_prompt": int(getattr(acct, "peak_prompt", 0)),
        "seconds": round(seconds, 2),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arm", choices=ARMS, default="durable")
    ap.add_argument("--dataset", type=Path, default=triage_corpus.DEFAULT_DATASET)
    ap.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    ap.add_argument("--env-file", type=Path, default=None,
                    help="dotenv holding the credential pair; default <runs-root>/../.env")
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--concurrency", type=int, default=2,
                    help="at most 2: the live run shares this provider endpoint")
    ap.add_argument("--limit", type=int, default=0, help="stop after N rows (a smoke run)")
    ap.add_argument("--only", default=None, help="comma-separated case_id substrings")
    args = ap.parse_args(argv)
    if args.concurrency > 2:
        ap.error("--concurrency above 2 is refused: the endpoint is shared with a live run")

    if not args.runs_root.is_dir():
        ap.error("--runs-root %s is not a directory" % args.runs_root)
    env_file = args.env_file if args.env_file is not None else args.runs_root.parent / ".env"
    secrets, source = credential_pair(env_file, args.runs_root)
    sys.stderr.write("credential tier: %s (key=%s binding=%s)\n"
                     % (source, bool(secrets.get("llm_api_key")),
                        secrets.get("llm_api_key_base_url") or "<none>"))
    if not secrets.get("llm_api_key"):
        sys.stderr.write("no llm_api_key in tier %s — nothing to replay with\n" % source)
        return 2

    rows = triage_corpus.read_dataset(args.dataset)["rows"]
    if args.only:
        wanted = [s for s in args.only.split(",") if s]
        rows = [r for r in rows if any(w in str(r.get("case_id")) for w in wanted)]
    if args.limit:
        rows = rows[:args.limit]

    contexts: dict[str, RunContext] = {}
    for row in rows:
        run = str((row.get("provenance") or {}).get("run"))
        if run not in contexts:
            contexts[run] = RunContext(args.runs_root / run, secrets)

    done = 0
    lock = threading.Lock()
    started = time.time()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        def work(row):
            nonlocal done
            ctx = contexts[str((row.get("provenance") or {}).get("run"))]
            try:
                out = diagnose_row(row, ctx, args.arm, args.runs_root)
            except Exception as exc:                              # noqa: BLE001
                out = {"case_id": row.get("case_id"), "reason": None,
                       "harness_error": "%s: %s" % (type(exc).__name__, exc)}
            out["arm"] = args.arm
            with lock:
                handle.write(json.dumps(out, ensure_ascii=False) + "\n")
                handle.flush()
                done += 1
                sys.stderr.write("[%3d/%3d] %-44s %-13s %s\n"
                                 % (done, len(rows), out.get("case_id"),
                                    out.get("reason"), out.get("harness_error", "")))

        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
            list(pool.map(work, rows))
    sys.stderr.write("arm=%s rows=%d wall=%.1fs -> %s\n"
                     % (args.arm, len(rows), time.time() - started, args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
