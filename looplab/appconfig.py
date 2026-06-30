"""Human-friendly run configuration: one YAML (or JSON) file describes BOTH *what* to solve (the
task) and *how* to run it (engine settings), plus CLI escape hatches so a run needs no file at all.

    # looplab.yaml
    out: runs/demo
    task:
      kind: dataset
      goal: predict `target` from the features
      data_path: data.csv
      direction: max
    settings:
      backend: llm
      max_nodes: 20

Design (why this is a thin *converter*, not a new source of truth):

- **Back-compat.** A document with no top-level ``task:`` key is treated as a bare task dict — the
  legacy JSON task format — so every existing ``examples/*.json`` keeps working unchanged.
- **YAML is input-only.** The task and settings become the same dicts the engine already consumes,
  and the run dir still records canonical JSON snapshots, so ``replay``/``resume`` are untouched.
- **One precedence order**, highest first: explicit CLI flags / ``--set`` > the file's ``settings:``
  block > ``LOOPLAB_*`` env (+ ``.env``) > field defaults. (pydantic-settings makes ``__init__``
  kwargs win over env, so building ``Settings(**merged)`` realizes exactly this.)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .config import Settings


def _read_doc(path: Path) -> dict:
    """Parse a YAML or JSON document into a dict. YAML is a superset of JSON, so a single
    ``yaml.safe_load`` would read both — but we keep the stdlib ``json`` path for ``.json`` so the
    core never needs PyYAML for the legacy format, and only import PyYAML (with a clear install hint)
    when a YAML file is actually used."""
    text = path.read_text(encoding="utf-8-sig")   # utf-8-sig tolerates a BOM from Windows editors
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ModuleNotFoundError as e:   # pragma: no cover - environment-dependent
            raise ValueError(
                "reading a YAML config needs PyYAML: pip install pyyaml "
                "(or use a .json file)") from e
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level, got {type(data).__name__}")
    return data


def split_document(doc: dict) -> tuple[dict, dict, Optional[str]]:
    """Split a loaded document into ``(task, settings, out)``.

    A *unified* document has a ``task:`` (and optionally ``settings:`` / ``out:``) key. Anything else
    is a *bare task* (the legacy format) — the whole dict is the task, with no settings or out."""
    if "task" in doc or "settings" in doc:
        task = dict(doc.get("task") or {})
        settings = dict(doc.get("settings") or {})
        out = doc.get("out")
        return task, settings, (str(out) if out is not None else None)
    return dict(doc), {}, None


def load_document(path: Path) -> tuple[dict, dict, Optional[str]]:
    """Read a config file and split it into ``(task, settings, out)``."""
    return split_document(_read_doc(path))


def coerce_scalar(raw: str) -> Any:
    """Turn a ``--set key=value`` string into a typed value: try JSON (so ``3``, ``true``, ``1.5``,
    ``["*.py"]``, ``{"a":1}`` parse as their natural types), else keep the literal string (so
    ``--set llm_model=qwen3:8b`` stays a string and doesn't need quoting)."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def parse_sets(pairs: list[str]) -> dict:
    """Parse repeatable ``--set key=value`` pairs into a dict, validating each key against the real
    ``Settings`` fields so a typo (``--set max_node=9``) errors loudly instead of being silently
    dropped by ``extra="ignore"``."""
    valid = set(Settings.model_fields)
    out: dict = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"--set expects key=value, got {pair!r}")
        key, _, value = pair.partition("=")
        key = key.strip()
        if key not in valid:
            raise ValueError(f"unknown setting {key!r}; see `looplab init` or "
                             f"docs/guide/configuration.md for the full list")
        out[key] = coerce_scalar(value)
    return out


# Convenience CLI flags that map to a task field, so simple runs need no file. `--data` maps to the
# field that names the input for that kind (repo edits a tree; everything else points at a data file).
def apply_task_flags(task: dict, *, kind: Optional[str], goal: Optional[str],
                     direction: Optional[str], data: Optional[str]) -> dict:
    """Overlay the task-building CLI flags onto a (possibly empty) task dict. Only provided flags are
    applied, so they refine a file without clobbering its other fields."""
    task = dict(task)
    if kind is not None:
        task["kind"] = kind
    if goal is not None:
        task["goal"] = goal
    if direction is not None:
        task["direction"] = direction
    if data is not None:
        field = "editable_path" if task.get("kind") == "repo" else "data_path"
        task[field] = data
    return task


def build_settings(file_settings: dict, typed_overrides: dict, sets: dict) -> Settings:
    """Merge settings in precedence order (file < typed flags < --set) and build a validated
    ``Settings``. Unspecified fields fall back to env/.env/defaults because pydantic-settings ranks
    ``__init__`` kwargs above env, so the merged dict wins only where it actually sets a value."""
    merged = {**file_settings, **typed_overrides, **sets}
    return Settings(**merged)


# --- `looplab init` template -------------------------------------------------------------------
# The three example task blocks correspond to the user-facing difficulty ladder: describe-and-run
# (point at data, the agent writes everything), run-an-existing-script (a repo with its own eval),
# and a pure offline objective (no LLM, no data). The simplest is active; the rest are commented.
_TASK_EXAMPLES = {
    "dataset": """\
task:
  # ── Describe-and-run: point at a data file and say what to predict in plain words.
  #    The agent writes the whole solution and picks the metric. Simplest real-data start.
  kind: dataset
  goal: predict `target` from the features; pick the metric you judge most appropriate
  direction: max          # max (accuracy/score) or min (error/loss)
  data_path: data.csv     # CSV/Parquet with your columns (a `target` column here)

  # ── Run-an-existing-script: tune/edit a repo you already have; success = the repo's own eval.
  # kind: repo
  # goal: tune config.json to maximize the eval metric
  # direction: max
  # editable_path: ./my_project       # the repo the agent may edit
  # edit_surface: ["*.json"]          # globs the agent is allowed to touch
  # protect: ["train.py"]             # files it must never edit (the grader/metric)
  # eval:
  #   command: ["python", "train.py"] # how to score a candidate
  #   metric: {kind: stdout_json, key: metric}
  #   timeout: 60

  # ── Pure offline objective: no LLM, no data — a toy numeric optimum (great for a smoke test).
  # kind: quadratic
  # goal: minimize (x-3)^2 + (y+1)^2
  # direction: min
  # bounds: {x: [-10.0, 10.0], y: [-10.0, 10.0]}
""",
}


def _render_default(name: str, field) -> str:
    """Render a Settings field's default as a YAML scalar (JSON is valid YAML for scalars/lists/maps).
    Secrets and undefined factory defaults render as null."""
    from pydantic_core import PydanticUndefined
    try:
        val = field.get_default(call_default_factory=True)
    except TypeError:   # older pydantic without the kwarg
        val = field.get_default()
    if val is PydanticUndefined or name == "llm_api_key":
        val = None
    if isinstance(val, tuple):
        val = list(val)
    try:
        return json.dumps(val)
    except TypeError:
        return json.dumps(str(val))


def render_template(kind: str = "dataset") -> str:
    """Build a documented config template: a curated, commented `settings:` section with the knobs
    most runs touch, then a complete (commented-out) appendix of every remaining setting at its
    default — so the file is both approachable and exhaustive, doubling as living documentation."""
    task_block = _TASK_EXAMPLES.get(kind, _TASK_EXAMPLES["dataset"])
    # Curated common knobs shown live with a one-line comment each.
    common = [
        ("backend", "toy", "toy = offline optimizer (no LLM); llm = drive a real model"),
        ("max_nodes", "8", "candidate budget — how many ideas the loop tries"),
        ("max_seconds", "null", "wall-clock ceiling in seconds (null = no limit)"),
        ("policy", "greedy", "search policy: greedy | evolutionary | mcts | asha | bohb"),
        ("llm_model", "qwen3:8b", "model id (only used when backend: llm)"),
        ("llm_base_url", "http://localhost:11434/v1", "any OpenAI-compatible endpoint"),
        ("developer_backend", "default", "default | opencode | aider | goose | continue"),
        ("memory_enabled", "true", "cross-run case memory (learn across runs); on by default"),
        ("knowledge_enabled", "true", "knowledge base the agent can search + grow; on by default"),
        ("home_dir", ".looplab", "base dir for default memory/knowledge stores"),
        ("knowledge_dir", "null", "custom KB notes dir (null = <home_dir>/knowledge)"),
        ("memory_dir", "null", "custom memory dir (null = <home_dir>/memory)"),
    ]
    shown = {k for k, _, _ in common} | {"llm_api_key"}
    lines: list[str] = [
        "# LoopLab run config. Run it with:  looplab run looplab.yaml",
        "#",
        "# Precedence (highest first): CLI --set / flags  >  this file's settings:  >  LOOPLAB_* env",
        "# (+ .env)  >  defaults. Every key under settings: is a Settings field (see the appendix",
        "# below for the complete list); on the CLI the same key is `--set key=value` or LOOPLAB_KEY.",
        "",
        "out: runs/demo            # where the run is written (resumable from this dir alone)",
        "",
        task_block.rstrip("\n"),
        "",
        "settings:",
        "  # ── Common knobs ──────────────────────────────────────────────────────────────────────",
    ]
    for key, val, comment in common:
        decl = f"  {key}: {val}"
        # Always leave ≥2 spaces before the comment — a `#` glued to the value is NOT a YAML comment,
        # it becomes part of the value (e.g. a base_url ending in `/v1# ...`).
        lines.append(decl + " " * max(2, 40 - len(decl)) + f"# {comment}")
    lines += [
        "",
        "  # ── All other settings (defaults shown; uncomment to change) ─────────────────────────",
    ]
    for name, field in Settings.model_fields.items():
        if name in shown:
            continue
        lines.append(f"  # {name}: {_render_default(name, field)}")
    return "\n".join(lines) + "\n"
