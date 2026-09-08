"""EFFECTIVE TRAIN BATCH — the batch size the training PROCESS says it ran at, read at the metric read.

THE FOURTH SIDE OF THE METRIC RECORD, and it is the one the other three cannot state:

    metric_subject    the OUTPUT: which artifact this number is a claim ABOUT.
    metric_inputs     the INPUTS:  which bytes it was measured AGAINST.
    applied_params    the COORDINATES: what the CONFIGURATION that ran said they were worth.
    effective_batch   what the RUNNING PROCESS recorded about one of those coordinates.

`applied_params` states its own bound out loud: *"this is a statement about a DOCUMENT, not about an
execution. A key the loader never reads, a section a different code path ignores, an environment
variable that wins over the file — none of them are visible to any reader of bytes."* The batch size
is the coordinate where that bound has a name and a cost. HuggingFace's
`TrainingArguments.auto_find_batch_size` halves the batch on OOM and retries; measured in the
installed transformers 4.51.0, `Trainer.train` sets `self._train_batch_size` and
`self.state.train_batch_size` to the reduced value and writes back to
`args.per_device_train_batch_size` only inside the DeepSpeed branch, restoring `original_bs` two
lines later. So every saved config — the one `applied_params` reads — keeps the DECLARED number, and
the value that actually ran survives in `trainer_state.json::train_batch_size` and a `logger.debug`
line. `docs/45-claim-surfaces-2026-08-20.md` §3.2 REFUSED `auto_find_batch_size` as the memory answer
on exactly that measurement: a run that reports "trained at 8192" while it trained at 1024 is the
record-diverging-from-reality defect that whole document is about, made worse here because LoopLab
RANKS nodes — two nodes whose recorded configs differ only in batch could have trained at the same
one. It named the condition that lifts the refusal: **the effective batch being lifted into a durable
LoopLab event**. This module is the reader half; `EV_EFFECTIVE_TRAIN_BATCH` is the record.

WHAT IT READS AND WHAT IT REFUSES TO SAY.

  * The artifact is `trainer_state.json` — written by the trainer itself, into each checkpoint and by
    `Trainer.save_state()` — and the field is `train_batch_size`, which is what that library means by
    it: the per-STEP batch of one process (`per_device_train_batch_size * max(1, n_gpu)`), not the
    global batch. Nothing here multiplies by accumulation steps or world size to invent an "effective
    batch": `trainer_state.json` carries neither, and a number composed from facts the artifact does
    not hold is the typed-constraint failure this record exists to end. The record says which file it
    read and what that file said.
  * A pipeline legitimately holds SEVERAL trainings (a mine stage and a train stage each resolve
    their own), and a training holds several checkpoints. So every readable state file becomes a
    READING, deduplicated on the value, and a scalar answer is published ONLY when every reading
    agrees. Two readings that disagree are two true facts about two trainings; picking one would
    record a number nobody chose, which is `applied_params`' `conflict` rule and its reason.
  * FRESHNESS IS ENFORCED, unlike on the committed-carrier tier. A `trainer_state.json` is by
    definition something THIS attempt's process produced, so one that predates the attempt belongs to
    the previous attempt and must not be read as this one's. The floor is the caller's
    (`command_eval.attempt_freshness_floor` — a stage-scoped re-run passes `None`, because there the
    reused stage's output IS this attempt's).
  * ABSENCE IS SILENCE. `None` — never an empty record — when no state file was found or none could
    be read. Every task that is not a transformers training is in that state permanently, and "the
    trainer never said" and "the trainer said nothing changed" are opposite claims.

It never raises for anything a filesystem or a malformed document can do: a record may not cost a
node its terminal, which is `metric_inputs`' rule and `metric_salvage`'s before it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from looplab.runtime.metric_subject import bind_one

# The artifact, its field, and what that library means by the field. A REGISTRY of one: the string is
# the trainer's, not ours, and a reader that guessed a second spelling would report a number from a
# file whose semantics nobody checked.
TRAINER_STATE_NAME = "trainer_state.json"
TRAINER_STATE_FIELD = "train_batch_size"

# State files read for one node. A training writes one per checkpoint, so the plural is the ordinary
# case rather than the exotic one — but a workdir naming more than this is a checkpoint archive, and
# over the bound the rest are unread, which can only UNDER-report a disagreement. The largest real
# training on this box saves ~15.
MAX_STATE_FILES = 64

# Bytes read from one state file. `trainer_state.json` is a log of loss points and is legitimately
# large; over the bound the file is skipped entirely rather than parsed from a truncated prefix,
# because half a JSON document is not a smaller document.
MAX_STATE_BYTES = 8 * 1024 * 1024


def _state_files(workdir) -> list:
    """Workdir-relative paths of every `trainer_state.json` under the workdir, sorted, bounded.

    A plain filesystem walk that reads NOTHING the candidate wrote about itself — the same property
    `metric_subject.resolve_glob` keeps for a declared pattern, and the reason a discovered path is
    as trustworthy here as a declared one would be: the engine establishes what is there, and the
    RECORD names it. `Path.rglob` and not a hand-rolled `os.walk`, so a symlinked loop costs one
    failed `scandir` rather than a walk of the box.
    """
    root = Path(workdir)
    out: list = []
    try:
        for p in root.rglob(TRAINER_STATE_NAME):
            try:
                out.append(p.relative_to(root).as_posix())
            except ValueError:                        # pragma: no cover — rglob yields under root
                continue
            if len(out) > MAX_STATE_FILES:
                break
    except (OSError, ValueError, RuntimeError):
        # Total over anything a filesystem can do, for `resolve_glob`'s reason: an unreadable tree
        # must cost the RECORD, never the node.
        return []
    return sorted(out)


def _read_state(path) -> Optional[dict]:
    """One state file's parsed object, or None. Bounded, and total over everything it can raise."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            return None
        obj = json.loads(raw.decode("utf-8", "replace"))
    except (OSError, ValueError, UnicodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _batch_value(state: dict) -> Optional[int]:
    """The state's own `train_batch_size`, or None. Positive int only — `bool` is an `int` subclass
    and `true` is not a batch size, the same rejection the fold applies to `at_vocab`."""
    value = state.get(TRAINER_STATE_FIELD)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _step(state: dict) -> Optional[int]:
    """`global_step` when the state carries a usable one — ordering context for the reader, never a
    tie-break: this module publishes a scalar only when the readings AGREE."""
    value = state.get("global_step")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def bind_effective_train_batch(workdir, *, since: Optional[float] = None,
                               confine=None) -> Optional[dict]:
    """The effective-train-batch record for one eval, or `None`.

    Shape (additive; every key is optional to a reader, and every log written before today has none
    of it — invariant #5)::

        {"readings": [{"path", "train_batch_size", "global_step", "digest"}],  # sorted by path
         "train_batch_size": int | None,   # only when every reading agrees; None when they do not
         "disagree": bool,                 # two trainings, two true numbers — never settled here
         "files_seen": int,                # state files found, INCLUDING the unreadable/stale ones
         "truncated": bool}                # more than `MAX_STATE_FILES` exist; the rest are unread

    `None` when no `trainer_state.json` was found, or none of the ones found could be read — the
    permanent state of every task that is not a transformers training, and not the same statement as
    an empty reading list.
    """
    found = _state_files(workdir)
    if not found:
        return None
    truncated = len(found) > MAX_STATE_FILES
    readings: list = []
    for rel in found[:MAX_STATE_FILES]:
        # `bind_one` and not a second `os.stat`: identity, containment, the digest and the FRESHNESS
        # floor are one rule here and in `metric_subject`/`applied_params`, and a fourth spelling of
        # it is how the three came apart before. A stale or escaping file is simply not a reading.
        row = bind_one(workdir, rel, since=since, confine=confine)
        if not row.get("bound"):
            continue
        state = _read_state(Path(workdir) / rel)
        if state is None:
            continue
        value = _batch_value(state)
        if value is None:
            continue
        readings.append({"path": rel, TRAINER_STATE_FIELD: value, "global_step": _step(state),
                         "digest": str(row.get("digest") or "")})
    if not readings:
        return None
    distinct = {row[TRAINER_STATE_FIELD] for row in readings}
    return {"readings": readings,
            TRAINER_STATE_FIELD: readings[0][TRAINER_STATE_FIELD] if len(distinct) == 1 else None,
            "disagree": len(distinct) > 1,
            "files_seen": len(found), "truncated": truncated}
