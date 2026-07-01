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

import json
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

# How to turn data locations the user mentions in plain words into the task's data fields. The user
# may not pass an explicit path — they just say where things live; author the mounts yourself.
DATA_GUIDE = (
    "DATA: the user may describe where their data lives in plain words — ONE path or SEVERAL, a single "
    "file or a whole folder, possibly in different places. Author it from what they say; don't make "
    "them pre-format it:\n"
    '- A dataset task: put a single file/folder in "data_path", and/or several named locations in '
    '"data" ({"<short_name>":"<abs path>", ...}) when there are multiple. A folder is fine as one '
    "entry — the agent reads what's inside.\n"
    '- A repo task: runtime data goes in "data" ({"<name>":"<abs path>"}) — each is copied to '
    "./<name> in the eval workdir.\n"
    "Use the paths EXACTLY as given (~ and $HOME/$VARS are expanded); never invent a path the user "
    "didn't mention. If they clearly have data but named no location, ask ONE clarifying question in "
    "`reply` instead of guessing."
)


# How to author a `repo` task for maximum autonomy — the optional knobs that let the run adapt to the
# environment. Author them from the user's WORDS (they may say "use all GPUs", "install deps once",
# "the repo is huge, only touch code"); when unsaid, prefer the sensible defaults noted here.
REPO_AUTONOMY_GUIDE = (
    "REPO TASK AUTONOMY KNOBS (optional; author from the user's words, else use the default):\n"
    "- seed_mode (\"auto\"|\"tracked\"|\"all\"): how the editable repo is copied into each experiment "
    "workdir. Default \"auto\" = copy only git-tracked source when it's a git repo (so a tree bloated "
    "with untracked model checkpoints/data is NOT deep-copied). Use \"all\" only for a small repo or "
    "when untracked files are needed at eval time. If the repo is large, keep \"auto\"/\"tracked\".\n"
    "- Data/large inputs that live OUTSIDE the repo go in `data` ({name: abs_path}) — they are mounted "
    "(symlinked) at ./<name>, never deep-copied. Prefer this for multi-GB datasets/pretrained models.\n"
    "- Dependencies: put a ONE-TIME install in `eval.run_setup` (runs once at run start into the shared "
    "interpreter — the default when deps are stable) OR a per-experiment install in `eval.setup` (runs "
    "before EVERY eval — use when the agent edits requirements and each node needs its own). Don't set "
    "either if deps are already present.\n"
    "- Hyperparameters: only set `params` bounds when the user wants a specific bounded search; "
    "otherwise LEAVE `params` EMPTY and let the coding agent estimate sane values from the model size, "
    "available GPU memory and any README recipe — and use ALL available GPUs by default.\n"
    "- Put operational guidance the agent needs (use all GPUs, expected metric, which script to run) in "
    "the task `goal` in plain words — the coding agent reads it.\n")


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
    error: str = ""           # set when the model/endpoint failed (vs an empty task from a vague goal)

    @property
    def kind(self) -> Optional[str]:
        return self.task.get("kind") if isinstance(self.task, dict) else None


def author_task(goal: str, *, client, kinds: tuple[str, ...], data: Optional[str] = None,
                repo: Optional[str] = None, direction: Optional[str] = None,
                kind: Optional[str] = None, draft: Optional[dict] = None,
                parser: str = "tool_call") -> GenesisResult:
    """Ask the model to author an inline task from a plain goal. With `kind=None` it also CHOOSES the
    kind; with `kind` set it is CONSTRAINED to that kind and only fills the rest (the user pinned the
    type, Genesis does the rest within it). `draft` is an existing task dict (e.g. from a config file)
    to refine in place rather than discard. Returns a GenesisResult; on a vague goal the task may be
    empty with a clarifying `reply`, and on a model/endpoint failure `error` is set (so the caller can
    tell 'reach the model' apart from 'your goal was too vague')."""
    hints = []
    if data:
        hints.append(f"The user named a data/input path: {data} (use it; add others they mention).")
    if repo:
        hints.append(f"The user's repository is at: {repo}")
    if direction:
        hints.append(f"Optimization direction: {direction}")
    if draft:
        hints.append("Refine this existing task draft in place, keeping fields the user didn't ask to "
                     f"change:\n{json.dumps(draft)[:1200]}")
    hint_block = ("\n" + "\n".join(hints)) if hints else ""
    if kind:
        kind_rule = (f"The user has PINNED the task kind to `{kind}` — author a task of EXACTLY that "
                     f"kind (do not switch kinds); fill in everything else from the goal.")
    else:
        kind_rule = TASK_KIND_GUIDE
    try:
        from .hardware import operational_attention_points
        _attn = "\n\n" + operational_attention_points()
    except Exception:  # noqa: BLE001
        _attn = ""
    sys_prompt = (
        "You bootstrap a new autonomous-ML run from the user's goal. Decide the TASK and author it as "
        "an inline `task` object.\n\n" + kind_rule + "\n\n" + DATA_GUIDE + "\n\n" + REPO_AUTONOMY_GUIDE +
        f"\n\nRegistered task kinds: {list(kinds)}." + _attn)
    user = f"Goal: {goal}{hint_block}"
    messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}]
    try:
        plan = parse_structured(client, messages, _TaskPlan, parser)
    except Exception as e:  # noqa: BLE001 - transport/parse failure: report it as an ERROR, distinct
        # from a vague goal (which parses fine but returns an empty task). The caller surfaces the two
        # differently — "reach the model" vs "your goal was too vague".
        return GenesisResult(error=str(e))
    task = plan.task if isinstance(plan.task, dict) else {}
    # Honor the pin: the kind the user gave wins over whatever the model emitted.
    if task and kind:
        task["kind"] = kind
    # The model may forget a direction/goal we already know — fill the obvious gaps so the result
    # validates without a second round-trip.
    if task and direction and not task.get("direction"):
        task["direction"] = direction
    if task and "goal" not in task:
        task["goal"] = goal
    # A user-given --data path the model dropped: put it where this kind expects it, so an explicit
    # path is never silently lost.
    if task and data and not (task.get("data_path") or task.get("editable_path") or task.get("data")):
        task["editable_path" if task.get("kind") == "repo" else "data_path"] = data
    if task and task.get("kind") == "repo":
        _normalize_repo_task(task)
    return GenesisResult(task=task, rationale=plan.rationale, reply=plan.reply)


def _normalize_repo_task(task: dict) -> None:
    """Coerce a repo task the model authored in a LOOSE shape into the canonical RepoTask schema —
    the model reliably picks the right VALUES (paths, seed_mode, run_setup) but often uses near-miss
    field names/types (repo_path, eval-as-string, metric-as-string, string setup commands). Fixing
    them here means an autonomy-authored task validates without a second round-trip. In place; only
    fills/renames — never overwrites a field already in canonical form."""
    import shlex
    def _as_list(v):
        return shlex.split(v) if isinstance(v, str) else (list(v) if isinstance(v, (list, tuple)) else v)
    # editable repo path under any of the common aliases
    if not task.get("editable_path"):
        for alias in ("repo_path", "repo", "path", "editable"):
            if isinstance(task.get(alias), str) and task[alias]:
                task["editable_path"] = task.pop(alias)
                break
    if not task.get("direction") and isinstance(task.get("optimization_direction"), str):
        task["direction"] = task.pop("optimization_direction")
    # eval: a bare string command, or a dict with loose command/metric/setup shapes
    ev = task.get("eval")
    if isinstance(ev, str):
        ev = {"command": _as_list(ev)}
        task["eval"] = ev
    if isinstance(ev, dict):
        if isinstance(ev.get("command"), str):
            ev["command"] = _as_list(ev["command"])
        for k in ("setup", "run_setup"):
            if isinstance(ev.get(k), str):
                ev[k] = _as_list(ev[k])
        # metric as a bare key string -> the stdout_json reader the repo eval prints
        m = ev.get("metric")
        if isinstance(m, str):
            ev["metric"] = {"kind": "stdout_json", "key": m}
        elif m is None and isinstance(task.get("metric"), str):
            ev["metric"] = {"kind": "stdout_json", "key": task["metric"]}
    # a top-level metric string with no eval yet -> stash it on a minimal eval so it isn't lost
    if isinstance(task.get("metric"), str) and isinstance(task.get("eval"), dict) \
            and "metric" not in task["eval"]:
        task["eval"]["metric"] = {"kind": "stdout_json", "key": task["metric"]}
    task.pop("metric", None)
