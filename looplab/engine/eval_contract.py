"""The EVALUATION CONTRACT of a run: what its numbers were measured BY.

WHY THIS EXISTS, MEASURED ON THIS BOX (2026-08-16, 46 run directories under `runs/` that carry a
`task.snapshot.json`; 13 more carry none and are unknown to every rule here).

`rubertlite-dr-unified-v8` scores `python -m vectorsearch.test` over `/home/jovyan/data/vectorizer-
unified` with the v2 local data root. `rubert-dr-0807` runs a different harness entirely — `python
looplab_eval.py --save_path models/rubertlite_run --gpus 1` over `/home/jovyan/data/vectorizer/dense-
retrieval` against `/home/jovyan/data/datasets/dense-retrieval/rubertlite`. They are two different
evaluations of two different artifacts. Yet v8's Researcher wrote, at `at_node: 0` with `trigger:
run_start`, into `research_completed.data.memo.findings[1]`:

    "The strongest verified anchor in the portfolio is rubert-dr-0807 #9: recall@100=0.8776"

and again in `.summary` ("the prior sibling landscape is decisive"), and the engine re-emitted it as a
`hint` ("already proven to take the same backbone from ~0.74 to 0.8776 in sibling rubert-dr-0807 node
9"), from where it reached the builder's prompt and node 9's repair rationale ("the OneCycleLR idea is
sound (sibling hit 0.8776 with it)"). v8's own champion is 0.762048. The run spent decisions climbing
toward a number from an evaluation it cannot be measured on.

THE FIELD THAT WOULD HAVE SAID SO DOES NOT EXIST ANYWHERE, and that is the finding rather than an
aside. Measured over the live store `/home/jovyan/data/looplab-memory` (132 rows: 23 lessons, 63
research claims, 4 capsules, 21 cases, 21 meta notes):

  * 132 of 132 carry `run_id` and `task_id`. 0 of 132 carry a metric NAME, a dataset path, an eval
    command, or any contract identity. `research_claims.metric` exists on 42 rows and is the empty
    string on all 42 — the number lives only inside `statement` prose.
  * So a retrieval partition keyed on a stored contract is INERT on the entire existing corpus, and a
    fail-CLOSED one would blank all 132 rows. That is why nothing here reads the memory store.

WHY THE METRIC NAME IS NOT THE KEY, and this is the trap a first pass walks into. Three of the four
`RECALL@100` contracts on this box are byte-identical in metric name AND reader:
`stdout_regex` / `RECALL@100: ([0-9.]+)` spans `repo_task`, `rubert_dr_0804` AND `rubert_dr_0807`.
Partitioning on the metric name MERGES exactly the runs this module exists to separate. What actually
differs is the eval COMMAND and the DECLARED PATHS, so those are the identity.

WHY `(task_id, direction)` IS NOT THE KEY EITHER — the same refusal `ui/src/crossRunRank.js` states
in its own words ("a shared task_id is an operational lookup key … two runs of `repo_task` may have
optimized recall@100 against different corpora"). It is wrong in BOTH directions on this corpus:

  * UNDER-SPLIT: `rubertlite-dense-retrieval` folds to `task_id: repo_task` and is listed by
    `list_sibling_runs` as a sibling of v8 — with `best=0.8077` — while running the human's own
    argparse harness over `/home/jovyan/data/vectorizer/dense-retrieval`. One header, two evaluations.
  * OVER-SPLIT: `rubert_dr_0804` and `rubert_dr_0807` are the SAME contract (identical command,
    identical declared paths) under two task ids.

WHAT THIS MODULE IS AND IS NOT. It is a pure read of a run's OWN `task.snapshot.json` — the operator's
declaration, written by the engine at setup, which `docs/36` classifies as authenticated evidence. It
NEVER asks a model whether two runs are comparable, and it never reaches a metric, a champion, a
selectability decision or a violation: nothing here mutates `RunState`, writes an event, or is read by
`events/replay.py`. It answers ONE question, and returns `None` — never a guess — when it cannot.

FAIL OPEN, DELIBERATELY. `comparable()` returns `None` whenever either side is unreadable, and every
caller treats `None` exactly like `True`. An unknown contract must never be presented as a different
one: hiding a legitimate prior result is worse than the defect this module was written for, and 13 of
the 59 run directories on this box have no snapshot to read.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

# The snapshot the engine writes at setup. It is the run's own recorded declaration of what it would
# run and score, not a later reconstruction.
TASK_SNAPSHOT = "task.snapshot.json"

# Bounds. A snapshot is operator-authored JSON and this runs inside a never-raise tool path, so every
# collection read here is capped rather than trusted. These are far above anything on this box (the
# widest real command is 5 tokens, the widest path set 3).
_MAX_COMMAND_TOKENS = 64
_MAX_PATHS = 32
_MAX_FIELD_CHARS = 1024


class EvalContract:
    """What one run's numbers were measured BY: the reader, the command, and the declared paths.

    Deliberately NOT part of the identity:
      * `direction` and `task_id` — the existing scope predicates already gate on both, and folding
        them in here would make this key a strictly stronger version of a rule that is applied
        elsewhere, so a change to one would silently move the other.
      * the GOAL text — model- and operator-authored prose that drifts between reruns of the same
        evaluation (v6/v7/v8 have three different goal strings and one identical contract).
      * timeouts, seeds, footprints — they change what a run COSTS, never what its number MEANS.
    """

    __slots__ = ("metric_kind", "metric_key", "command", "paths")

    def __init__(self, *, metric_kind: str, metric_key: str, command: tuple,
                 paths: tuple):
        self.metric_kind = metric_kind
        self.metric_key = metric_key
        self.command = command
        self.paths = paths

    def key(self) -> tuple:
        """The comparison key. A plain tuple, so equality is Python's and not a similarity score."""
        return (self.metric_kind, self.metric_key, self.command, self.paths)

    def __eq__(self, other) -> bool:
        return isinstance(other, EvalContract) and self.key() == other.key()

    def __hash__(self) -> int:
        return hash(self.key())

    def differences(self, other: "EvalContract") -> list[str]:
        """WHICH facets differ, named, for a receipt that has to be checkable by the operator.

        A receipt saying only "different contract" is not auditable — the operator cannot tell a real
        boundary from a bug in this file. Naming the facet lets them look.
        """
        out = []
        if (self.metric_kind, self.metric_key) != (other.metric_kind, other.metric_key):
            out.append(f"metric reader ({other.metric_kind}:{other.metric_key!r} vs "
                       f"{self.metric_kind}:{self.metric_key!r})")
        if self.command != other.command:
            out.append(f"eval command ({' '.join(other.command) or '—'!r} vs "
                       f"{' '.join(self.command) or '—'!r})")
        if self.paths != other.paths:
            only_other = [p for p in other.paths if p not in self.paths]
            out.append("declared paths (" + (", ".join(only_other[:3]) or "—") + ")")
        return out


def _text(value, *, cap: int = _MAX_FIELD_CHARS) -> str:
    """One bounded string from an arbitrary snapshot value. Never raises."""
    if value is None:
        return ""
    try:
        return str(value)[:cap]
    except Exception:  # noqa: BLE001 - a snapshot value with a hostile __str__ is not a crash
        return ""


def contract_from_task(task) -> Optional[EvalContract]:
    """Build the contract from an already-parsed task snapshot mapping. `None` when unreadable.

    `None` is returned for a snapshot carrying NO evaluation identity at all — neither a metric reader
    nor a command nor a declared path. Such a run (14 of the 46 snapshots here: the toy/probe runs,
    whose eval is a builtin) genuinely has nothing to compare, and inventing an all-empty key for it
    would make every one of them "the same contract" as every other.
    """
    if not isinstance(task, dict):
        return None
    ev = task.get("eval")
    ev = ev if isinstance(ev, dict) else {}

    metric = ev.get("metric")
    if isinstance(metric, str):
        # A metric declared as a bare string is the legacy shorthand; it names the KEY, not the reader.
        metric_kind, metric_key = "", _text(metric)
    elif isinstance(metric, dict):
        metric_kind = _text(metric.get("kind"), cap=64)
        # `pattern` for a regex reader, `key` for a JSON one, `name` in the oldest snapshots. One of
        # the three is the identity; which one it is is decided by `kind`, so both are carried.
        metric_key = _text(metric.get("pattern") or metric.get("key") or metric.get("name"))
    else:
        metric_kind, metric_key = "", ""

    raw_cmd = ev.get("command") or task.get("cmd") or []
    if isinstance(raw_cmd, str):
        raw_cmd = [raw_cmd]
    command = tuple(_text(c, cap=256) for c in raw_cmd[:_MAX_COMMAND_TOKENS]) if isinstance(
        raw_cmd, (list, tuple)) else ()

    # The DECLARED artifact/data surfaces. `data:` mounts are included because a run scoring the same
    # command over a different corpus is a different evaluation — which is refusal 1 of
    # `crossRunRank.js` stated as a field instead of as a caveat.
    paths = []
    for field in ("editable_path", "data_path", "repo"):
        value = _text(task.get(field))
        if value:
            paths.append(value)
    data = task.get("data")
    if isinstance(data, dict):
        for entry in list(data.values())[:_MAX_PATHS]:
            if isinstance(entry, dict):
                value = _text(entry.get("path"))
            elif isinstance(entry, str):
                value = _text(entry)
            else:
                value = ""
            if value:
                paths.append(value)

    contract = EvalContract(metric_kind=metric_kind, metric_key=metric_key, command=command,
                            paths=tuple(sorted(set(paths))[:_MAX_PATHS]))
    if not (contract.metric_kind or contract.metric_key or contract.command or contract.paths):
        return None
    return contract


def contract_for_run_dir(run_dir) -> Optional[EvalContract]:
    """Read one run directory's contract. `None` for a missing/unreadable/contract-free snapshot.

    Broad `except` on purpose, and it is the fail-open guarantee rather than laziness: this is called
    from a never-raise tool path, and every failure mode here — no snapshot, bad JSON, a permission
    error, a directory that is not a run — means UNKNOWN. Unknown must reach the caller as `None` so
    it can be treated as comparable, never as a difference that withholds or annotates a real result.
    """
    try:
        path = Path(run_dir) / TASK_SNAPSHOT
        with path.open(encoding="utf-8") as handle:
            return contract_from_task(json.load(handle))
    except Exception:  # noqa: BLE001 - see the docstring: every failure is UNKNOWN, not a difference
        return None


def comparable(this: Optional[EvalContract],
               other: Optional[EvalContract]) -> Optional[bool]:
    """Tri-state: `True` same evaluation, `False` provably different, `None` not decidable.

    `None` is NOT `False`. Callers must treat it as `True` — see the module docstring. It is a
    separate value rather than a default so a caller cannot collapse the two by accident.
    """
    if this is None or other is None:
        return None
    return this == other


def contract_notice(this: Optional[EvalContract], other: Optional[EvalContract],
                    *, other_run_id: str = "") -> str:
    """The one sentence a surface prints beside a FOREIGN run's number. Empty when it must not.

    Empty for comparable AND for undecidable, so the sentence carries information every time it
    appears. It states a fact about the two declarations and stops: it does not tell the reader what
    to conclude, does not rank, and does not say the number is wrong. The number itself is untouched —
    this annotates, it does not withhold, and it CANNOT withhold, because the value it sits beside was
    correctly measured by a real evaluation and is a true fact about that run.
    """
    if comparable(this, other) is not False:
        return ""
    assert this is not None and other is not None  # narrowed by the `is not False` above
    facets = this.differences(other)
    who = f"run {other_run_id} " if other_run_id else ""
    return ("DIFFERENT EVALUATION CONTRACT: " + who
            + "measured its number with a different " + "; ".join(facets)
            + ". Its value is not on this run's scale and is not a target for it.")
