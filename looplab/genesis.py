"""Genesis on the CLI: turn a plain-text goal into a runnable task — the LLM picks the task `kind`
and authors the inline spec, so the user never has to name a task type.

This is the headless counterpart of the Web UI's "New run" Genesis chat (`server.py /api/genesis`):
same idea — a model reads your words (and any data/repo path you mention) and decides *what kind of
task this is* — exposed for `looplab run --goal "..."` with no `--kind`.

On `kind` itself: it is **not** removed. It is the dispatch key that selects one of nine
``TaskAdapter`` semantics (each a different eval / grader / trust / data model — e.g. a self-reported
``dataset`` metric vs a held-out ``mlebench`` grader vs your own protected ``repo`` eval). Collapsing
those into one type would erase real differences in how a candidate is scored and trusted. What
Genesis removes is the *burden* of naming it: where an LLM is already in the loop, the LLM infers the
kind from the goal instead of making the human pick.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel

from .parse import parse_structured

# Canonical, CLI-focused guide to choosing a task kind from a plain goal. Kept compact and in one
# place (the richer, repo-scouting variant the Web UI uses lives in server.py's genesis endpoint).
TASK_KIND_GUIDE = (
    "Choose the task KIND that fits the user's goal and author an inline `task` object for it:\n"
    "- dataset — they point at a data file and want a prediction; the agent writes the WHOLE solution "
    'and picks the metric. {"kind":"dataset","goal":"<what to predict>","direction":"max",'
    '"data_path":"<path to their data>"}. The simplest "here is my data, get the best metric" case.\n'
    "- repo — they have an EXISTING code project to improve, with their own way to run/score it. "
    '{"kind":"repo","goal":"...","direction":"max"|"min","editable_path":"<repo path>",'
    '"edit_surface":["**/*.py"],"eval":{"command":["python","train.py"],'
    '"metric":{"kind":"stdout_json","key":"metric"},"timeout":1800}}. Copy any path/command/metric '
    "key they give VERBATIM; never invent a path you weren't given.\n"
    "- mlebench_real — a known Kaggle / MLE-bench competition. "
    '{"kind":"mlebench_real","competition":"<full-kaggle-slug>"}.\n'
    "- quadratic — a pure numeric objective with named variables and bounds, no data or code "
    '(great offline). {"kind":"quadratic","goal":"minimize ...","direction":"min",'
    '"bounds":{"x":[-10,10],"y":[-10,10]}}.\n'
    "- classification / regression / timeseries — tune a fixed model template (knobs, not free code) "
    "for a synthetic/tabular objective.\n"
    "- code_regression / mlebench — the LLM writes a numpy script scored by a held-out grader the "
    "agent can't see (use when an anti-cheat guarantee matters and there is no repo).\n"
    "Rules: author exactly ONE `task`. Set `direction` (max for score/accuracy, min for error/loss). "
    "If the goal is too vague to choose, leave `task` empty and ask ONE clarifying question in `reply`."
)


# Kinds whose work is inherently LLM-driven (the agent writes/edits code or reasons over data). When
# Genesis infers one of these and the user didn't choose a backend, the run defaults to backend=llm —
# the offline-optimizable kinds (quadratic/classification/regression/timeseries) stay on their default.
GENERATIVE_KINDS = frozenset({"dataset", "code_regression", "mlebench", "mlebench_real", "repo"})


class _TaskPlan(BaseModel):
    """What Genesis emits for a fresh CLI run: just the inline task (it picks the kind), plus a short
    human-facing line. Engine settings stay on the CLI/file — Genesis only decides *what* to solve."""
    task: dict = {}
    rationale: str = ""
    reply: str = ""


@dataclass
class GenesisResult:
    task: dict = field(default_factory=dict)
    rationale: str = ""
    reply: str = ""

    @property
    def kind(self) -> Optional[str]:
        return self.task.get("kind") if isinstance(self.task, dict) else None


def author_task(goal: str, *, client, kinds: tuple[str, ...], data: Optional[str] = None,
                repo: Optional[str] = None, direction: Optional[str] = None,
                parser: str = "tool_call") -> GenesisResult:
    """Ask the model to author an inline task from a plain goal (it chooses the kind). Returns a
    GenesisResult; on a vague goal the task may be empty and `reply` carries a clarifying question.
    Never raises on a model hiccup — a failed parse yields an empty result the caller reports."""
    hints = []
    if data:
        hints.append(f"The user's data/input is at: {data}")
    if repo:
        hints.append(f"The user's repository is at: {repo}")
    if direction:
        hints.append(f"Optimization direction: {direction}")
    hint_block = ("\n" + "\n".join(hints)) if hints else ""
    sys_prompt = (
        "You bootstrap a new autonomous-ML run from the user's goal. Decide what KIND of task this is "
        "and author it.\n\n" + TASK_KIND_GUIDE +
        f"\n\nRegistered task kinds: {list(kinds)}.")
    user = f"Goal: {goal}{hint_block}"
    messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}]
    try:
        plan = parse_structured(client, messages, _TaskPlan, parser)
    except Exception:  # noqa: BLE001 - any parse/transport failure -> empty result (caller reports it)
        return GenesisResult()
    task = plan.task if isinstance(plan.task, dict) else {}
    # The model may echo the kind under a different key or forget direction we were told — fill the
    # obvious gaps so the result validates without a second round-trip.
    if task and direction and not task.get("direction"):
        task["direction"] = direction
    if task and "goal" not in task:
        task["goal"] = goal
    return GenesisResult(task=task, rationale=plan.rationale, reply=plan.reply)
