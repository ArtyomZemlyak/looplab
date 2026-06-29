"""Role backends (I5, ADR-7). `Researcher` proposes an Idea (params to try);
`Developer` turns an Idea into runnable code. Both are Protocols so an LLM-backed
or external-coding-agent backend drops in with zero orchestrator change.

The Toy* implementations make the P0 loop runnable fully offline (no API keys):
the Researcher is a blind seeded optimizer (random seeds, then hill-climbs around
the current best using only *observed* metrics); the Developer emits a script whose
executed objective is the ground truth the Researcher never sees. This exercises
the real loop (draft -> run -> evaluate -> improve -> select) deterministically.
"""
from __future__ import annotations

import random
from typing import Optional, Protocol

from .models import Idea, Node, RunState
from .parse import LLMClient, ParseError, extract_code, parse_structured
from .prompts import PromptStore, render
from .validate import AgentReport, validate_agent_code

_RESEARCHER_SYSTEM = ("You are an ML researcher proposing the next experiment as "
                      "parameters to try. Also set `theme`: a short, reusable lower-case slug "
                      "(e.g. \"loss-fn\", \"architecture\", \"regularization\", \"learning-rate\") "
                      "that groups this experiment with related ones — reuse the SAME slug across "
                      "experiments that explore the same idea. "
                      "Optionally, when a hyperparameter is cheap to vary and the task data loads "
                      "fast, you MAY propose a SWEEP instead of a single point: set `space` to a "
                      "small discrete grid {name: [values, ...]} (keep the total grid small, "
                      "<= ~12 points). The Developer then evaluates every grid point in ONE process "
                      "(loading the data once), so a sweep is far cheaper than the same points run "
                      "as separate nodes. Leave `space` empty for an ordinary single-config "
                      "experiment; fixed/shared hyperparameters still go in `params`. "
                      "If THIS experiment is genuinely compute-heavy and needs more wall-clock than a "
                      "light model — a neural network (CNN/RNN/transformer), a large ensemble, many CV "
                      "folds/seeds, or a big grid — set `eval_timeout` to a realistic per-run budget in "
                      "SECONDS (e.g. 300-1800). Leave it null for ordinary/light experiments so they use "
                      "the run default. "
                      "Respond ONLY with the requested structured fields.")
_DEVELOPER_SYSTEM = ("You are an expert ML engineer. Output ONLY a single fenced "
                     "```python``` block containing a complete, self-contained script. ")
# Appended to the Developer's system prompt when the Idea carries a `space` (intra-node sweep).
_SWEEP_CONTRACT = (
    "\nThis is an INTRA-NODE SWEEP: evaluate EVERY point of the given grid in ONE process — load "
    "the data ONCE and reuse it across all grid points. Report ALL results by printing, as the "
    "FINAL stdout line, a JSON object: {\"trials\": [{\"params\": {..}, \"metric\": <float>, "
    "\"seconds\": <float>, \"extra_metrics\": {..}}, ...]} — one entry per grid point. The easiest "
    "way is `from looplab.sweep import run_sweep` and call run_sweep(space, train_fn) where "
    "train_fn(params, seed) returns the metric (it prints the required line for you); but you may "
    "use Optuna/GridSearchCV/joblib instead as long as you emit that exact JSON line. If the task "
    "is host-graded (it asks you to write predictions/submission), write them for the SINGLE BEST "
    "grid point so the host can grade it.")


class Researcher(Protocol):
    def propose(self, state: RunState, parent: Optional[Node]) -> Idea: ...


class Developer(Protocol):
    def implement(self, idea: Idea) -> str: ...


# --------------------------------------------------------------------------- #
# Toy backends (offline, deterministic given a seed)
# --------------------------------------------------------------------------- #

_OBJECTIVE_TEMPLATE = '''\
import json, os, random
# Generated solution. The objective below is the toy "ground truth" the
# Researcher optimizes blindly via observed metrics only.
x = {x}
y = {y}
loss = (x - 3.0) ** 2 + (y + 1.0) ** 2
noise = {noise}
if noise:
    # Seeded eval noise: lets the multi-seed confirmation phase (I12) measure
    # variance. LOOPLAB_EVAL_SEED is unset (-> "0") during normal evaluation, so
    # search stays deterministic; the confirm phase varies it across seeds.
    rng = random.Random(int(os.environ.get("LOOPLAB_EVAL_SEED", "0")))
    loss += rng.gauss(0.0, noise)
print(json.dumps({{"metric": loss}}))
'''


class ToyResearcher:
    """Blind seeded optimizer: random seeds, then Gaussian hill-climb around best."""

    def __init__(self, bounds: dict[str, tuple[float, float]], seed: int = 0, step: float = 1.0):
        self.bounds = bounds
        self.step = step
        self.rng = random.Random(seed)

    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        keys = list(self.bounds)
        if parent is None:
            params = {k: round(self.rng.uniform(*self.bounds[k]), 4) for k in keys}
            return Idea(operator="draft", params=params, rationale="random seed point")
        params = {}
        for k in keys:
            lo, hi = self.bounds[k]
            v = parent.idea.params.get(k, 0.0) + self.rng.gauss(0.0, self.step)
            params[k] = round(max(lo, min(hi, v)), 4)
        return Idea(operator="improve", params=params,
                    rationale=f"perturb best node {parent.id} (metric={parent.metric})")


class ToyObjectiveDeveloper:
    """Renders an Idea's params into a runnable script (the objective is fixed here).
    `noise` (>0) injects seeded eval noise so the confirmation phase has variance to
    measure; 0 (default) keeps the objective deterministic."""

    def __init__(self, noise: float = 0.0):
        self.noise = noise

    def implement(self, idea: Idea) -> str:
        return _OBJECTIVE_TEMPLATE.format(
            x=idea.params.get("x", 0.0),
            y=idea.params.get("y", 0.0),
            noise=self.noise,
        )


# --------------------------------------------------------------------------- #
# LLM-backed backends (I2, ADR-7/14). Same Protocols; swap-in needs no loop change.
# Tested against a fake LLMClient (no live calls); go-live needs a model endpoint.
# --------------------------------------------------------------------------- #


def _clamp_fill(idea: Idea, bounds: Optional[dict]) -> Idea:
    """Clamp numeric params into bounds and fill any missing ones with the midpoint, so
    a stray/empty proposal can't crash the objective."""
    if bounds:
        for k, (lo, hi) in bounds.items():
            if k in idea.params:
                idea.params[k] = max(lo, min(hi, float(idea.params[k])))
            else:
                idea.params[k] = (lo + hi) / 2.0
    return idea


def _state_brief(state: RunState, parent: Optional[Node]) -> str:
    best = state.best()
    lines = [f"Goal: {state.goal}", f"Optimize direction: {state.direction}."]
    if best is not None:
        lines.append(f"Best so far: node {best.id} metric={best.metric} params={best.idea.params}")
    if parent is not None:
        lines.append(f"Refine from node {parent.id}: params={parent.idea.params} metric={parent.metric}")
    # Append the always-on "working set": a compact view of the whole search (top winners, weakest /
    # failures, theme map) so the Researcher proposes with awareness of what's already been tried,
    # not just `best` + `parent`. Depth (full experiments, code, data) lives behind the run tools.
    from .digest import experiments_digest
    lines.append(experiments_digest(state))
    return "\n".join(l for l in lines if l)


class LLMResearcher:
    """Proposes an `Idea` via structured output (tool_call default, baml fallback).

    `space_hint` describes the task's parameter space in the prompt; `bounds` clamps
    (and fills missing) numeric params so a small model's stray proposal can't crash
    the objective — quality robustness, not a correctness crutch."""

    def __init__(self, client: LLMClient, space_hint: str = "",
                 bounds: Optional[dict] = None, parser: str = "tool_call",
                 prompts: Optional[PromptStore] = None):
        self.client = client
        self.space_hint = space_hint
        self.bounds = bounds
        self.parser = parser
        self.prompts = prompts

    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        # Operator steering (Phase 5 `hint` control events): fold them into the prompt so a live
        # human can nudge the search ("try higher degree", "focus on regularization"). Advisory —
        # the model still proposes; bounds still clamp.
        from .hints import render_hint_directives
        hint_block = render_hint_directives(state.pending_hints)
        # A0d: an engine-set complexity cue keyed on the operated node's breadth (empty when off).
        cue = getattr(self, "_complexity_hint", "")
        # Strategist `prefer_sweep` bias (engine-set, empty when off): nudges — but never forces —
        # the Researcher toward an intra-node sweep when the cost model favors in-process execution.
        sweep_hint = getattr(self, "_sweep_hint", "")
        messages = [
            {"role": "system",
             "content": render(self.prompts, "researcher_system", _RESEARCHER_SYSTEM)},
            {"role": "user", "content": _state_brief(state, parent) + "\n" + self.space_hint +
                                        hint_block + cue + sweep_hint +
                                        "\nPropose the next Idea (operator, params, rationale; "
                                        "optionally a `space` grid for a sweep). The `rationale` is "
                                        "your conclusion the operator reads: in 1-3 sentences state "
                                        "WHY this experiment next and WHAT you expect it to "
                                        "learn/improve given the results so far — not a restatement "
                                        "of the params."},
        ]
        # Small models occasionally emit unparseable output; retry, then fall back to a
        # safe default so one bad response never crashes the run.
        idea: Optional[Idea] = None
        last: Optional[Exception] = None
        for _ in range(2):
            try:
                idea = parse_structured(self.client, messages, Idea, self.parser)
                break
            except ParseError as e:
                last = e
        if idea is None:
            idea = Idea(operator="draft", params={}, rationale=f"fallback (parse failed: {last})")
        return _clamp_fill(idea, self.bounds)


class LLMDeveloper:
    """Writes (and repairs) a complete runnable solution script. `brief` carries the
    task's I/O contract (where to read data, what metric to print). `repair` powers the
    error-feedback debug operator: it gets the failing code + stderr and fixes it."""

    def __init__(self, client: LLMClient, brief: str = "",
                 prompts: Optional[PromptStore] = None):
        self.client = client
        self.brief = brief
        self.prompts = prompts

    def implement(self, idea: Idea) -> str:
        system = render(self.prompts, "developer_system", _DEVELOPER_SYSTEM) + self.brief
        # Render whatever params the task's Researcher proposed (task-agnostic): degree/lam
        # for regression, k for mlebench, etc. — hardcoding names dropped the value on
        # tasks that use a different hyperparameter.
        params = ", ".join(f"{k}={v}" for k, v in idea.params.items()) or "(model defaults)"
        if idea.space:
            # Intra-node sweep: render the grid and append the trials-reporting contract to the
            # system prompt so the Developer runs every point in one process and reports them all.
            system += _SWEEP_CONTRACT
            grid = "; ".join(f"{k} in {v}" for k, v in idea.space.items())
            fixed = f" Fixed/shared params: {params}." if idea.params else ""
            user = (f"Run an intra-node sweep over the grid: {grid}.{fixed} {idea.rationale}").strip()
        else:
            user = (f"Approach to implement with parameters: {params}. {idea.rationale}").strip()
        return extract_code(self.client.complete_text(
            [{"role": "system", "content": system}, {"role": "user", "content": user}]))

    def repair(self, idea: Idea, code: str, error: str) -> str:
        system = ("You are an expert Python debugger. " +
                  render(self.prompts, "developer_system", _DEVELOPER_SYSTEM) + self.brief)
        user = ("The script below failed. Return a corrected, complete script that runs "
                "and prints the required JSON metric line.\n\n--- SCRIPT ---\n" + code +
                "\n\n--- ERROR (stderr tail) ---\n" + error)
        return extract_code(self.client.complete_text(
            [{"role": "system", "content": system}, {"role": "user", "content": user}]))


# --------------------------------------------------------------------------- #
# Validating wrapper (ADR-7): audit how an external coding agent performed
# --------------------------------------------------------------------------- #


class ValidatingDeveloper:
    """Wrap a Developer and validate how it performed before the orchestrator spends a
    sandbox evaluation on its output (see `validate.py`).

    On an invalid result it re-prompts the *inner* developer with the failure folded
    into the Idea's rationale (a cheap correction loop), up to `max_retries` times. If it
    still can't produce valid code it falls back to `fallback` (typically the in-house
    `LLMDeveloper`, the known-good path) — so a flaky external agent degrades to a
    working developer instead of poisoning the search with a no-op/broken node.

    `last_report` holds the `AgentReport` for the most recent call; the orchestrator logs
    it as an `agent_validated` event, giving a per-node audit trail of the agent.

    Tool-agnostic: any Developer works as `inner`. If `inner` exposes `last_run` /
    `last_seed` (as `CliAgentDeveloper` does), the report also includes process-level
    checks (launched / not-timed-out / exit) and the no-op (`modified_seed`) check.
    """

    def __init__(self, inner, *, fallback=None, max_retries: int = 1,
                 metric_key: str = "metric", repo_mode: bool = False):
        self.inner = inner
        self.fallback = fallback
        self.max_retries = max_retries
        self.metric_key = metric_key
        # repo_mode: validate the agent's changed-FILE set (RepoTask), and treat the
        # fallback (a baseline / no-op developer) as always shippable — running the
        # unmodified repo is a valid result, not a failure.
        self.repo_mode = repo_mode
        # Audit of the most recent call. `last_report` always describes the EXTERNAL
        # AGENT (even when we fall back) — that's what we're auditing; the fallback's
        # validity is recorded separately in `last_shipped_ok`.
        self.last_report: Optional[AgentReport] = None
        self.last_attempts: int = 0
        self.last_fell_back: bool = False
        self.last_shipped_ok: bool = False
        self.last_files: dict[str, str] = {}   # multi-file output of the shipped attempt
        self.last_deleted: list[str] = []      # accepted in-surface deletions of the shipped attempt

    # forward the brief/prompt hooks make_roles pokes at, to the wrapped developer
    @property
    def brief(self) -> str:
        return getattr(self.inner, "brief", "")

    @property
    def prompts(self):
        return getattr(self.inner, "prompts", None)

    @prompts.setter
    def prompts(self, value) -> None:
        self.inner.prompts = value

    def _report(self, code: str, *, agent: bool) -> AgentReport:
        """Validate `code`. `agent=True` pulls the inner agent's process signal + seed
        (no-op detection) + patch-gate verdict; `agent=False` (fallback output) does
        static checks only."""
        return validate_agent_code(
            code,
            seed=getattr(self.inner, "last_seed", None) if agent else None,
            run=getattr(self.inner, "last_run", None) if agent else None,
            patch=getattr(self.inner, "last_patch", None) if agent else None,
            files=(getattr(self.inner, "last_files", {}) or {}) if (agent and self.repo_mode)
                  else None,
            metric_key=self.metric_key,
        )

    def _record(self, report: AgentReport, *, attempts: int, fell_back: bool,
                shipped_ok: bool) -> None:
        self.last_report = report
        self.last_attempts = attempts
        self.last_fell_back = fell_back
        self.last_shipped_ok = shipped_ok
        # Multi-file output only when the agent itself shipped; the LLM fallback is
        # single-file (its code goes to solution.py via node.code).
        self.last_files = ({} if fell_back
                           else dict(getattr(self.inner, "last_files", {}) or {}))
        self.last_deleted = ([] if fell_back
                             else list(getattr(self.inner, "last_deleted", []) or []))

    def _attempt_loop(self, idea: Idea, call, fallback_call=None) -> str:
        """Run `call(idea)` (implement or repair), validate, retry-with-feedback up to
        `max_retries`, then fall back via `fallback_call` (defaults to the fallback's
        implement). Records the agent audit on every path."""
        code, report = "", AgentReport()
        attempt = idea
        attempts = 0
        for _ in range(self.max_retries + 1):
            attempts += 1
            code = call(attempt)
            report = self._report(code, agent=True)
            if report.ok:
                self._record(report, attempts=attempts, fell_back=False, shipped_ok=True)
                return code
            attempt = idea.model_copy(deep=True)   # re-prompt with the failure as a hint
            attempt.rationale = (
                idea.rationale +
                f"\n[validator] the previous attempt was rejected: {report.feedback()}. "
                "Edit solution.py to fix this.").strip()
        if self.fallback is not None:              # exhausted retries -> known-good path
            fb = (fallback_call or (lambda: self.fallback.implement(idea)))()
            # In repo mode the fallback is the baseline (no-op) developer — running the
            # unmodified repo is always a valid shippable result.
            fb_ok = True if self.repo_mode else self._report(fb, agent=False).ok
            # last_report still describes the AGENT (it failed); shipped code is the LLM's
            self._record(report, attempts=attempts, fell_back=True, shipped_ok=fb_ok)
            return fb
        self._record(report, attempts=attempts, fell_back=False, shipped_ok=report.ok)
        return code

    def audit_extra(self) -> dict:
        """Wrapper-specific audit fields merged into the `agent_validated` event so the
        log shows whether the agent succeeded, how many tries it took, and whether we had
        to fall back to the LLM developer."""
        return {"attempts": self.last_attempts, "fell_back": self.last_fell_back,
                "shipped_ok": self.last_shipped_ok}

    def implement(self, idea: Idea) -> str:
        return self._attempt_loop(idea, self.inner.implement)

    def repair(self, idea: Idea, code: str, error: str) -> str:
        inner_repair = getattr(self.inner, "repair", None)
        if not callable(inner_repair):
            return self.implement(idea)
        # Fall back to the fallback's repair (preserving the error-feedback) when it has
        # one, else its implement — never lose the debug context on the fallback path.
        fb_repair = getattr(self.fallback, "repair", None) if self.fallback else None
        fb_call = (lambda: fb_repair(idea, code, error)) if callable(fb_repair) else None
        return self._attempt_loop(idea, lambda i: inner_repair(i, code, error), fb_call)
