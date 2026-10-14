"""EFFECTIVE CONFIG — the hyperparameters one evaluation actually ran with, by source (doc 79).

WHY (doc 77 §2, audit 2026-10-14 L-11). On the external-agent MiniOneRec runs the node's only
committed carrier was a one-line env file (`G_ARM=G1`); the learning rate, batch, warmup, steps and
seed were resolved INSIDE the operator's guard script and existed only in a stage log and in files
outside the run. `applied_params` (`runtime/applied_params.py`) answers a narrower question — what
the committed or resolved configuration DOCUMENT says each DECLARED param is worth — so its record
was `{}` on every such node, and the declared `stage2_lr` / `stage2_warmup` were never checked
against anything.

This record is the complement: every hyperparameter-SHAPED value LoopLab can see, regardless of
whether anything declared it, from three sources it can name exactly:

  * `env`   — the environment the engine handed the evaluation command (the task/run declared
              eval env; never the host environment, never a secret-shaped name),
  * `file`  — `KEY=VALUE` lines of the node's own overlay env/config files (`*.env`),
  * `log`   — configuration dumps in the stage logs (`LR:       2e-4`, `Batch: 1024 (micro=128)`,
              `SFT_LEARNING_RATE=2e-4`), read from the head of each `<stage>.log` in the workdir.

Raw names are mapped to a small CANONICAL vocabulary (`lr`, `batch_size`, `micro_batch_size`,
`grad_accum`, `world_size`, `seed`, `max_steps`, `epochs`, `warmup_steps`, `warmup_ratio`,
`scheduler`, `weight_decay`) by `canonical_key`; a name about evaluation, tokenization or caching
(`SFT_TOKENIZE_BATCH_SIZE`, `Eval steps`) is not a training coordinate and maps to nothing.

THEN THE RECONCILIATION. Each declared numeric `Idea.params` entry whose name maps to a canonical
key (a `stage2_` prefix scopes it to that stage's log) is `checked` when some observed value equals
it, a `mismatch` when values were observed and none equals it, and `unknown` when nothing was
observed — missing is never zero and never agreement. Two different values for one key within one
stage are a `conflict`. A self-report is still a self-report: a value in a log the candidate's code
printed is evidence of what that code SAID it used, which is why every value carries its source.

Pure reads, bounded, never raises (a record may not cost a node its terminal).
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Iterable, Optional

VERSION = 1
LOG_HEAD_BYTES = 256 * 1024
MAX_VALUES_PER_KEY = 8
CANONICAL_KEYS = ("lr", "batch_size", "micro_batch_size", "grad_accum", "world_size", "seed",
                  "max_steps", "epochs", "warmup_steps", "warmup_ratio", "scheduler",
                  "weight_decay")
# A name carrying one of these tokens is about evaluation, inference, data preparation or caching —
# not the training coordinate the canonical key names.
_DENY = frozenset({"eval", "evaluation", "val", "valid", "validation", "test", "infer", "inference",
                   "predict", "prediction", "gen", "generation", "tokenize", "tokenizer", "cache",
                   "writer", "shard", "log", "logging", "save", "print", "per_rank_eval", "score",
                   "beam", "topk", "top"})
_STAGE_TOKEN = re.compile(r"^stage\d+$")
_LOG_KV = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_ .\-]{0,48}?)\s*[:=]\s*([^\s,;()]+)")
_PAREN_KV = re.compile(r"[(,]\s*([A-Za-z][A-Za-z0-9_]{0,32})\s*=\s*([^\s,)]+)")


def _tokens(raw: str) -> list:
    text = re.sub(r"([a-z])([A-Z])", r"\1_\2", str(raw))
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


def split_stage(raw: str) -> tuple:
    """`(stage_hint, tokens without it)`: `stage2_lr` -> ("stage2", ["lr"])."""
    toks = _tokens(raw)
    stage = next((t for t in toks if _STAGE_TOKEN.match(t)), None)
    return stage, [t for t in toks if not _STAGE_TOKEN.match(t)]


def canonical_key(raw) -> Optional[str]:
    """The canonical coordinate a raw config name denotes, or None."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    _, toks = split_stage(raw)
    if not toks:
        return None
    joined = "_".join(toks)
    tset = set(toks)
    if tset & _DENY and not ("seed" in tset and len(tset) <= 3):
        return None
    if "warmup" in tset:
        return "warmup_ratio" if ("ratio" in tset or "frac" in tset) else "warmup_steps"
    if "scheduler" in tset or "schedule" in tset or joined.endswith("lr_type"):
        return "scheduler"
    if "weight" in tset and "decay" in tset:
        return "weight_decay"
    if tset == {"micro"} or ("micro" in tset and ("batch" in tset or "bs" in tset)) or ("per" in tset and "device" in tset and "batch" in tset):
        return "micro_batch_size"
    if ("accum" in tset or "accumulation" in tset) and ("grad" in tset or "gradient" in tset):
        return "grad_accum"
    if "lr" in tset or "learning_rate" in joined:
        return None if ("min" in tset or "decay" in tset or "final" in tset) else "lr"
    if "seed" in tset:
        return "seed"
    if "batch" in tset or "bs" in tset:
        return "batch_size"
    if ("world" in tset and "size" in tset) or tset & {"gpus", "nproc", "ngpus"}:
        return "world_size"
    if "epochs" in tset or ("epoch" in tset and ("num" in tset or "train" in tset or len(toks) <= 2)):
        return "epochs"
    if "steps" in tset or "iters" in tset or "iterations" in tset:
        if tset & {"max", "train", "total", "num"} or toks in (["steps"], ["iters"]):
            return "max_steps"
    return None


def parse_value(raw):
    """A float when the text is a finite number (`2e-4`, `1_024`), else a bounded string."""
    text = str(raw).strip().strip("'\"")
    try:
        value = float(text.replace("_", ""))
    except ValueError:
        return text[:80] if text else None
    return value if math.isfinite(value) else text[:80]


def _offer(values: dict, key: str, value, *, source: str, where: str, raw: str,
           stage: Optional[str]) -> None:
    rows = values.setdefault(key, [])
    if len(rows) < MAX_VALUES_PER_KEY:
        rows.append({"value": value, "source": source, "where": where, "key": raw[:64],
                     "stage": stage})


def _scan_env_lines(text: str, values: dict, *, source: str, where_prefix: str,
                    stage: Optional[str]) -> None:
    for n, line in enumerate(text.splitlines()[:2000], 1):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        key, _, raw = line.partition("=")
        canon = canonical_key(key.strip())
        if canon and raw.strip():
            _offer(values, canon, parse_value(raw.strip().split()[0]), source=source,
                   where=f"{where_prefix}:L{n}", raw=key.strip(), stage=stage)


def _scan_log(text: str, values: dict, *, where: str, stage: Optional[str]) -> None:
    for n, line in enumerate(text.splitlines(), 1):
        if len(line) > 400 or line.lstrip().startswith(("{", "[", "'")):
            continue                     # progress dicts and data dumps are not configuration
        m = _LOG_KV.match(line)
        if m:
            canon = canonical_key(m.group(1))
            if canon:
                _offer(values, canon, parse_value(m.group(2)), source="log",
                       where=f"{where}:L{n}", raw=m.group(1).strip(), stage=stage)
        for pm in _PAREN_KV.finditer(line):
            canon = canonical_key(pm.group(1))
            if canon:
                _offer(values, canon, parse_value(pm.group(2)), source="log",
                       where=f"{where}:L{n}", raw=pm.group(1), stage=stage)


def _read_head(path: Path, limit: int) -> Optional[str]:
    from looplab.core.node_evidence import read_bounded_regular_file
    raw = read_bounded_regular_file(path, limit)        # None, never raises, on any unreadable file
    if raw is None:
        return None
    return raw.decode("utf-8", "replace")


def _same(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return a == b or abs(a - b) <= 1e-9 * max(abs(a), abs(b))
    return str(a).strip().lower() == str(b).strip().lower()


def capture(workdir, *, env: Optional[dict] = None, overlay_files: Iterable = (),
            stage_names: Iterable = (), declared: Optional[dict] = None) -> Optional[dict]:
    """The `effective_config` record for one evaluation, or None when nothing was seen or declared."""
    try:
        return _capture(workdir, env=env, overlay_files=overlay_files, stage_names=stage_names,
                        declared=declared)
    except Exception:  # noqa: BLE001 - a record may never cost a node its terminal
        return None


def _capture(workdir, *, env, overlay_files, stage_names, declared) -> Optional[dict]:
    from looplab.core.envsafe import is_secret_env
    root = Path(workdir)
    values: dict = {}
    sources_read: list = []
    for key, raw in sorted((env or {}).items()):
        if not isinstance(key, str) or is_secret_env(key, str(raw)):
            continue
        canon = canonical_key(key)
        if canon:
            stage, _ = split_stage(key)
            _offer(values, canon, parse_value(raw), source="env", where=f"env:{key}", raw=key,
                   stage=stage)
    for name in sorted(set(overlay_files or ())):
        if not str(name).endswith((".env", ".cfg", ".ini", ".sh")):
            continue
        text = _read_head(root / str(name), LOG_HEAD_BYTES)
        if text is None:
            continue
        sources_read.append(f"file:{name}")
        _scan_env_lines(text, values, source="file", where_prefix=f"file:{name}", stage=None)
    stages = [s for s in dict.fromkeys(str(s) for s in (stage_names or ()) if s)]
    for stage in stages:
        log = root / f"{stage}.log"
        text = _read_head(log, LOG_HEAD_BYTES)
        if text is None:
            continue
        sources_read.append(f"log:{stage}.log")
        _scan_log(text, values, where=f"{stage}.log", stage=stage)
    declared_numeric = {}
    for name, value in (declared or {}).items():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            declared_numeric[str(name)] = float(value)
        elif isinstance(value, str) and value.strip():
            declared_numeric[str(name)] = parse_value(value)
    if not values and not any(canonical_key(name) for name in declared_numeric):
        return None                  # nothing seen and nothing checkable: silence, not a record
    by_stage: dict = {}
    conflicts = []
    for key, rows in values.items():
        groups: dict = {}
        for row in rows:
            groups.setdefault(row["stage"], []).append(row)
        for stage, group in groups.items():
            distinct = []
            for row in group:
                if not any(_same(row["value"], d) for d in distinct):
                    distinct.append(row["value"])
            if len(distinct) == 1:
                by_stage.setdefault(stage or "", {})[key] = distinct[0]
            else:
                conflicts.append({"key": key, "stage": stage, "values": distinct[:MAX_VALUES_PER_KEY]})
    effective = {}
    for key, rows in values.items():
        distinct = []
        for row in rows:
            if not any(_same(row["value"], d) for d in distinct):
                distinct.append(row["value"])
        if len(distinct) == 1:
            effective[key] = distinct[0]
    mismatches, unknown, checked = [], [], 0
    for name, want in sorted(declared_numeric.items()):
        canon = canonical_key(name)
        if canon is None:
            continue
        stage, _ = split_stage(name)
        rows = [r for r in values.get(canon, ()) if stage is None or r["stage"] in (stage, None)]
        if stage is not None and not any(r["stage"] == stage for r in rows):
            rows = []                            # a stage-scoped declaration needs that stage's value
        if not rows:
            unknown.append(name)
            continue
        if any(_same(r["value"], want) for r in rows):
            checked += 1
            continue
        mismatches.append({"param": name, "key": canon, "declared": want,
                           "effective": rows[-1]["value"], "where": rows[-1]["where"],
                           "source": rows[-1]["source"]})
    return {"version": VERSION, "values": values, "effective": effective,
            "by_stage": {k: v for k, v in by_stage.items()}, "conflicts": conflicts,
            "declared": declared_numeric, "mismatches": mismatches, "unknown": unknown,
            "checked": checked, "sources_read": sources_read}
