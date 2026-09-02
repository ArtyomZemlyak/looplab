"""Run-config loader: how a run file / CLI / env becomes a ``Settings`` (the schema lives in
``looplab.core.config``). One YAML (or JSON) file describes BOTH *what* to solve (the task) and
*how* to run it (engine settings), plus CLI escape hatches so a run needs no file at all.

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

- **Back-compat.** A document with neither a top-level ``task:`` nor ``settings:`` key is treated as a
  bare task dict — the legacy JSON task format — so every existing ``examples/*.json`` keeps working
  unchanged. Either discriminator makes the document unified; a settings-only document has no task.
- **YAML is input-only.** The task and settings become the same dicts the engine already consumes,
  and the run dir still records canonical JSON snapshots, so ``replay``/``resume`` are untouched.
- **One precedence order** (canonical — stated in full only here), highest first: explicit CLI
  flags / ``--set`` > the file's ``settings:`` block > ``LOOPLAB_*`` env (+ ``.env``) > field
  defaults. (pydantic-settings makes ``__init__`` kwargs win over env, so building
  ``Settings(**merged)`` realizes exactly this.)
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Optional

from looplab.core.config import (Settings, canonicalize_parallelism_source,
                                 flatten_parallelism_layers)

_RUNTIME_CREDENTIAL_FIELDS = {"llm_api_key", "llm_api_key_base_url"}


def parse_document_text(text: str, *, suffix: str, label: str) -> dict:
    """Parse ALREADY-READ config text into a dict. YAML is a superset of JSON, so a single
    ``yaml.safe_load`` would read both — but we keep the stdlib ``json`` path for ``.json`` so the
    core never needs PyYAML for the legacy format, and only import PyYAML (with a clear install hint)
    when a YAML file is actually used.

    Split out of ``_read_doc`` so a caller that must control the READ can still share the one parser.
    ``serve/launch.py`` is that caller: it reads a launch ``task_file`` through a descriptor it has
    already fstat-fenced, and re-opening the same NAME here to parse it would reintroduce the
    check-to-open race that boundary exists to close. ``suffix`` selects the format (the caller's
    already-lowercased ``Path.suffix``) and ``label`` is what error messages name — the path, for a
    file-backed caller, so every message reads exactly as it did when this took a ``Path``.
    """
    if suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ModuleNotFoundError as e:   # pragma: no cover - environment-dependent
            raise ValueError(
                "reading a YAML config needs PyYAML: pip install pyyaml "
                "(or use a .json file)") from e
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            # YAMLError is not a ValueError, so callers' `except ValueError` would otherwise miss it
            # and dump a raw traceback instead of the intended one-line error.
            raise ValueError(f"{label}: invalid YAML: {e}") from e
    else:
        data = json.loads(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{label}: expected a mapping at the top level, got {type(data).__name__}")
    return data


def _read_doc(path: Path) -> dict:
    """Read a config file and parse it with the shared parser above."""
    text = path.read_text(encoding="utf-8-sig")   # utf-8-sig tolerates a BOM from Windows editors
    return parse_document_text(text, suffix=path.suffix.lower(), label=str(path))


def split_document(doc: dict) -> tuple[dict, dict, Optional[str]]:
    """Split a loaded document into ``(task, settings, out)``.

    A *unified* document has a ``task:`` or ``settings:`` key (and optionally ``out:``). Anything else
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
    ``--set llm_model=qwen3:8b`` stays a string and doesn't need quoting). JSON's non-finite
    extensions (``NaN``/``Infinity``/``-Infinity``) are kept as the literal string a user clearly
    meant, never coerced to a float — those are never valid setting values."""
    try:
        val = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw
    if isinstance(val, float) and not math.isfinite(val):   # NaN / inf / -inf -> literal string
        return raw
    return val


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
        if key in _RUNTIME_CREDENTIAL_FIELDS:
            raise ValueError(
                f"{key} is runtime-only; configure one key+endpoint pair through environment")
        out[key] = coerce_scalar(value.strip())
    return out


# `--data` only names an input for the two kinds that take one: a repo (an editable tree) and a
# dataset (a data file/dir). Mapping it onto any other kind would inject a field the model drops
# silently (pydantic extra="ignore"), losing the path with no warning — so we reject it instead.
_DATA_FIELD = {"repo": "editable_path", "dataset": "data_path"}


def apply_task_flags(task: dict, *, kind: Optional[str], goal: Optional[str],
                     direction: Optional[str], data: Optional[str]) -> dict:
    """Overlay the task-building CLI flags onto a (possibly empty) task dict. Only provided flags are
    applied, so they refine a file without clobbering its other fields. Raises ValueError if ``--data``
    is given for a kind that has nowhere to put it (so the path is never silently dropped)."""
    task = dict(task)
    if kind is not None:
        task["kind"] = kind
    if goal is not None:
        task["goal"] = goal
    if direction is not None:
        task["direction"] = direction
    if data is not None:
        k = task.get("kind")
        # kind unknown yet (Genesis will pick it) -> default to data_path; it's overwritten when
        # Genesis authors the task and passed to it as a hint anyway.
        field = _DATA_FIELD.get(k, "data_path") if k in (None, *_DATA_FIELD) else None
        if field is None:
            raise ValueError(
                f"--data is only meaningful for a dataset or repo task, not kind={k!r}. "
                f"Put the data in a config file, or describe its location in --goal for Genesis.")
        task[field] = data
    return task


# OPEN[config-file-keys-are-silently-ignored] `--set` validates its keys against `Settings.model_fields`
# and the FILE layer validates nothing: probed, a `settings:` block carrying `max_node: 30` yields the
# default 8 with no diagnostic. The YAML file is the documented primary launch surface, so a renamed
# or mistyped knob is the run-looks-configured failure the enum table exists to stop, one layer up.
# proof:absent:refuse_unknown_settings_keys@looplab/core/appconfig.py
def build_settings(file_settings: dict, typed_overrides: dict, sets: dict) -> Settings:
    """Merge settings in precedence order (file < typed flags < --set) and build a validated
    ``Settings``. Unspecified fields fall back to env/.env/defaults because pydantic-settings ranks
    ``__init__`` kwargs above env, so the merged dict wins only where it actually sets a value."""
    # These layers all become one Pydantic "init" source. Promote aliases BEFORE flattening them,
    # otherwise a file canonical value can shadow a higher-priority CLI legacy override.
    for source in (file_settings, typed_overrides, sets):
        forbidden = sorted(_RUNTIME_CREDENTIAL_FIELDS.intersection(source or {}))
        if forbidden:
            raise ValueError(
                "runtime credentials cannot be set in a run config/flag: " + ", ".join(forbidden))
    merged = flatten_parallelism_layers([
        canonicalize_parallelism_source(file_settings),
        canonicalize_parallelism_source(typed_overrides),
        canonicalize_parallelism_source(sets),
    ])
    return Settings(**merged)


# --- `looplab init` template -------------------------------------------------------------------
# An ACTIVE (uncommented) task: block per kind, so `looplab init --kind <k>` always scaffolds the
# kind the user asked for (not a one-size-fits-all dataset block). The four hand-authored kinds are
# the common hand-written entry points; the synthetic/templated kinds get a concise correct starter.
_TASK_BLOCKS = {
    "dataset": (
        "  # Describe-and-run: point at data, say what to predict; the agent writes the whole solution.\n"
        "  kind: dataset\n"
        "  goal: predict `target` from the features; pick the metric you judge most appropriate\n"
        "  direction: max          # max (accuracy/score) or min (error/loss)\n"
        "  data_path: data.csv     # a file OR folder; multiple inputs -> data: {train: a.csv, extra: b/}"),
    "repo": (
        "  # Edit/tune an existing repo; success = the repo's own eval. The agent edits only edit_surface.\n"
        "  kind: repo\n"
        "  goal: improve the eval metric\n"
        "  direction: max\n"
        "  editable_path: ./my_project        # the repo the agent may edit\n"
        '  edit_surface: ["**/*.py"]          # globs the agent is allowed to touch\n'
        '  protect: ["eval.py"]               # files it must never edit (the grader/metric)\n'
        "  eval:\n"
        '    command: ["python", "eval.py"]   # must print a final line like {"metric": 0.93}\n'
        "    metric: {kind: stdout_json, key: metric}\n"
        "    timeout: 1800"),
    "quadratic": (
        "  # Pure offline numeric objective — no LLM, no data (great for a smoke test).\n"
        "  kind: quadratic\n"
        "  goal: minimize (x-3)^2 + (y+1)^2\n"
        "  direction: min\n"
        "  bounds: {x: [-10.0, 10.0], y: [-10.0, 10.0]}"),
    "mlebench_real": (
        "  # A real Kaggle / MLE-bench competition (official split + grader). See docs/MLEBENCH.md.\n"
        "  kind: mlebench_real\n"
        "  competition: spooky-author-identification   # the FULL Kaggle slug\n"
        "  direction: max"),
    "regression": (
        "  # Synthetic polynomial+ridge model selection via CV (templated knobs, not free-form code).\n"
        "  kind: regression\n"
        "  goal: select polynomial degree + ridge lambda minimizing 5-fold CV MSE\n"
        "  direction: min\n"
        "  max_degree: 6\n"
        "  cv_k: 5"),
    "classification": (
        "  # Synthetic feature-map + classifier tuning via K-fold CV (concentric rings: the true\n"
        "  # boundary is a circle, so degree 1 cannot beat chance and degree >= 2 can).\n"
        "  kind: classification\n"
        "  goal: choose a polynomial feature-map degree + the learner's lr/l2/iters to maximize"
        " K-fold CV accuracy\n"
        "  direction: max\n"
        "  max_degree: 4\n"
        "  cv_k: 5"),
    "timeseries": (
        "  # Synthetic forecaster smoothing/seasonality via backtest.\n"
        "  kind: timeseries\n"
        "  goal: choose smoothing weight + seasonal period to minimize backtest MASE\n"
        "  direction: min\n"
        "  period: 7\n"
        "  max_period: 12"),
    "code_regression": (
        "  # The LLM writes a numpy script scored by a held-out grader (needs settings.backend: llm).\n"
        "  kind: code_regression\n"
        "  goal: write numpy code fitting a polynomial+ridge model minimizing 5-fold CV MSE\n"
        "  direction: min\n"
        "  max_degree: 6\n"
        "  cv_k: 5"),
    "mlebench": (
        "  # Competition-shaped synthetic task with a private held-out grader the agent can't see.\n"
        "  kind: mlebench\n"
        "  goal: train a classifier and maximize held-out accuracy (private grader)\n"
        "  direction: max"),
}


def _task_block(kind: str) -> str:
    """The active `task:` block for `kind`. Falls back to a minimal but correctly-typed stub for any
    kind without a hand-authored block, so the scaffold always matches the requested kind."""
    body = _TASK_BLOCKS.get(kind)
    if body is None:
        body = (f"  kind: {kind}\n  goal: describe what to optimize\n  direction: max\n"
                f"  # see docs/guide/tasks.md for the fields this kind accepts")
    return "task:\n" + body


def _render_default(name: str, field) -> str:
    """Render a Settings field's default as a YAML scalar (JSON is valid YAML for scalars/lists/maps).
    Secrets and undefined factory defaults render as null."""
    from pydantic_core import PydanticUndefined
    try:
        val = field.get_default(call_default_factory=True)
    except TypeError:   # older pydantic without the kwarg
        val = field.get_default()
    if val is PydanticUndefined or name in {"llm_api_key", "llm_api_key_base_url"}:
        val = None
    if isinstance(val, tuple):
        val = list(val)
    try:
        return json.dumps(val)
    except TypeError:
        return json.dumps(str(val))


def render_template(kind: str = "dataset") -> str:
    """Build a documented config template with active common settings and a commented appendix.

    Active common values are runnable defaults for the generated file and therefore override matching
    environment variables. Every remaining setting appears commented out at its field default.
    """
    from looplab.core.task_kinds import default_backend

    task_block = _task_block(kind)
    backend_default = default_backend(kind, chosen=False) or "toy"
    # Curated common knobs shown live with a one-line comment each.
    common = [
        ("profile", "default", "preset: default/fast (lean) | thorough (confirm+trust-gate+cues on)"),
        ("backend", backend_default,
         "llm = code-writing/reasoning model; toy = fixed offline optimizer/baseline"),
        ("max_nodes", "8", "candidate budget — how many ideas the loop tries"),
        ("max_seconds", "null", "wall-clock ceiling in seconds (null = no limit)"),
        ("policy", "greedy", "search policy: greedy | evolutionary | mcts | asha | bohb"),
        ("llm_model", "qwen3:8b", "model id (only used when backend: llm)"),
        ("llm_base_url", "http://localhost:11434/v1", "any OpenAI-compatible endpoint"),
        ("developer_backend", "default", "default | opencode | aider | goose | continue"),
        # memory_dir/knowledge_dir are ON by default with real path defaults (~/.looplab/{memory,knowledge}).
        # They are rendered COMMENTED (see _COMMENTED_KNOBS below): an ACTIVE `null` line here would OVERRIDE
        # those defaults and silently disable cross-run memory + the knowledge base in every scaffolded config.
        ("knowledge_dir", "~/.looplab/knowledge",
         "markdown notes the agent may search — ON by default; set a dir, or null to disable"),
        ("memory_dir", "~/.looplab/memory",
         "cross-run case memory (learn across runs) — ON by default; set a dir, or null to disable"),
    ]
    # Discoverable but non-overriding: these knobs default ON to a real path, so we show them COMMENTED —
    # documenting the knob without emitting an active value that would replace the default (esp. a `null`
    # that silently turns the feature off). Uncomment + edit to point elsewhere, or set `null` to disable.
    _COMMENTED_KNOBS = {"knowledge_dir", "memory_dir"}
    # Credentials and their endpoint binding are runtime-only. The generated run config must not
    # advertise either as a normal persisted knob.
    shown = {k for k, _, _ in common} | {"llm_api_key", "llm_api_key_base_url"}
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
        "  # Other task shapes: re-run `looplab init --kind repo|quadratic|mlebench_real|...`,",
        "  # or see docs/guide/tasks.md for every kind and its fields.",
        "",
        "settings:",
        "  # ── Common knobs ──────────────────────────────────────────────────────────────────────",
    ]
    for key, val, comment in common:
        decl = f"  {'# ' if key in _COMMENTED_KNOBS else ''}{key}: {val}"
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
