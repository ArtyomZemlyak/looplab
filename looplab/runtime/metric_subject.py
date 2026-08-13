"""METRIC SUBJECT — bind a recorded number to the CONTENT IDENTITY of the artifact it is about.

THE OMISSION THIS CLOSES
------------------------
Measured across the six repo runs that have an event log (doc 35, E1-E3):

    83   node_evaluated events carrying a metric
    82   of them record NO metric_provenance at all                       (98.8 %)
     2   are provably about bytes the node did not produce                 (2.4 %)

The one exception is the SALVAGE path — provenance is written only once something has already gone
wrong. On the happy path the engine records which stage ran, how long it took and what number came
out, and nothing whatsoever about what the number is ABOUT.

    read_metric() returns Optional[float]. The reader functions build a Path, stat it, and DISCARD
    it. RunResult has no source field. `node_evaluated.data` names no artifact. Every fact the
    system derives about a stage-written file is consumed as a boolean and thrown away.

A `float` has no referent, so every gate LoopLab owns reasons about the metric's VALUE and none about
its SUBJECT.

THE SHARPEST MEASUREMENT, and why nothing cheaper works
--------------------------------------------------------
`rubertlite-dr-unified-v6` node 4 trained a good model and scored a HUMAN's checkpoint that an
absolute path in an editable config named. The two files:

    92,174,712 bytes   node 4's own model.safetensors   sha256 e33abc1f972aa443…  inode 93213
    92,174,712 bytes   the human's model.safetensors    sha256 273885e69075c9a5…  inode 181848

**Byte-identical in SIZE.** Every predicate the artifact contract owns — exists, non-empty, fresh —
is satisfied by both. The contract is not merely aimed at the wrong side; on this incident it is
INCAPABLE of discriminating even pointed at the right one. Only content or inode identity separates
them, and the engine recorded neither. That is what this module records.

SUBJECT, NOT SOURCE — and the re-specification is load-bearing
---------------------------------------------------------------
The original wording was "bind the metric to the digest of the artifact it came FROM". That is
unimplementable and the measurement says so: **83 of 83 corpus metrics are `stdout_regex`**. The
number is parsed out of a stage's stdout; there is no source file to digest. So:

    bind the metric not to its SOURCE (where the number was parsed)
    but to its SUBJECT (what the number is a claim ABOUT),
    which the operator DECLARES and the engine VERIFIES.

WHAT THIS DOES NOT PROVE, stated because it is the half people over-read
-------------------------------------------------------------------------
Binding the subject proves "this identity is what the declared artifact held when the scorer
started". It does **not** prove the score stage READ it. Node 4's subject was present, fresh,
correct and byte-recordable, and the scorer read elsewhere anyway — which is also why floor option 1
(`needs` on the score stage) passes the incident: `verify_stage_inputs` was called against the
preserved node-4 workdir with a perfectly correct `needs` and returned `None`. It is a PRESENCE
check. The partner that makes "read elsewhere" impossible is the read allow-list
(`runtime/read_allowlist.py` + `runtime/landlock.py`); this module is the half that gives the number
a referent so replay can check the claim at all.

WHY IT IS THE OPERATOR'S FIELD
-------------------------------
`subject` lives on `EvalSpec.metric` — the operator's own spec on the operator's protected `score`
stage — and never in an agent-authored manifest. Agents author `expect` correctly 112 times out of
113 (doc 35, E6), so the declaration channel works; the point is not distrust of the agent's
accuracy but that the subject is what the metric MEANS, and the trust boundary
(`engine/metric_salvage.py`'s governing rule) is that the agent writes the training script and
therefore writes the very text an extractor would read. The subject has to come from the side that
owns scoring.

COST (measured on the 92 MB checkpoint, doc 35 §8)
---------------------------------------------------
    os.stat identity                                  ~1 syscall
    sampled digest (size + 1 MiB head + 1 MiB tail)    29.7 ms
    full sha256                                       336.2 ms
On the 5,303 s eval that produced the incident, the full digest is 0.006 % of the run. `SAMPLE_ABOVE`
picks between them, and the mode is RECORDED — a sampled digest must never be mistaken for a full
one by a later reader comparing two runs.

Layering: `runtime` imports nothing above `core`.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional

from looplab.core.atomicio import file_identity

# At most this many subjects per metric. A metric is a claim about a THING; a spec naming twenty
# artifacts is either a mis-declaration or a request for something this record cannot express (which
# of the twenty is the number about?). The same order of magnitude as `MAX_STAGE_EXPECT_FILES`.
MAX_SUBJECTS = 8

# Above this size the digest is SAMPLED rather than full. 256 MiB is the same ceiling
# `command_eval`'s OOM guard already uses for a metric-source file, so a subject and a metric source
# agree about what "too big to read whole" means. A modern checkpoint is routinely above it and a
# full sha256 of a 20 GB shard on a geesefs mount would dominate the eval it is describing.
SAMPLE_ABOVE = 256 * 1024 * 1024
# How much of an over-ceiling file the sampled digest covers, at each end. Head+tail rather than a
# stride: a truncated or half-written checkpoint differs at the tail, and a swapped one differs at
# the head (the header names the tensors), so the two ends carry the discriminating bytes at the
# lowest possible cost. It is NOT a security claim — a party that can choose both files can defeat a
# sampled digest — which is exactly why `digest_mode` is on the record.
SAMPLE_BYTES = 1024 * 1024

DIGEST_FULL = "sha256"
DIGEST_SAMPLED = "sha256-sampled"

# WHY a metric could not be bound to its subject — a CLOSED vocabulary, because this slug rides on
# the provenance record and, under the `require` rung, into a violation row the UI renders. A reader
# that has to distinguish "the operator never declared one" from "the declaration named a file that
# is not there" cannot do it from a free-text reason, and those two need opposite fixes.
#
#   not_declared  — `metric.subject` is absent/empty. The operator has not said what the number is
#                   about. This is the state 82 of 83 corpus metrics are in.
#   escapes       — a declared entry did not land inside the workdir (`_confined` refused it): an
#                   absolute path, a `..`, or a symlink pointing out. The subject of a metric is by
#                   definition something this node produced, so an outside path is never one.
#   missing       — the declared artifact is not there. The pipeline did not produce what the
#                   operator says the number is about.
#   empty         — it is there and zero-length (or an empty directory).
#   stale         — it is there and predates this eval attempt. The number cannot be about it: a
#                   leftover from an earlier repair attempt in a deliberately reused workdir is the
#                   measured shape here (v6 nodes ran up to 4 repair attempts), and admitting it is
#                   how a metric ends up describing a checkpoint from two attempts ago.
#   unreadable    — it exists and could not be stat'ed/digested (a FUSE error, a permission).
UNBOUND_REASONS = ("not_declared", "escapes", "missing", "empty", "stale", "unreadable")

# The rungs, in increasing strictness. See `Settings.metric_subject` for which is the default and for
# the evidence that would move it.
#
#   off     — record nothing. Byte-identical to the behaviour before this shipped.
#   audit   — RECORD, always: every node_evaluated carries `metric_provenance`, bound or not. This is
#             the half that turns 82/83-with-no-referent into 0/83, and it costs one stat plus a
#             bounded digest. No violation, no selection effect.
#   require — the INVERSION. An unbound metric additionally gets the existing `metric_salvaged`
#             violation row, so it is counted, visible in the UI and the lineage, and never
#             selectable. The absence of provenance stops meaning "fine" and starts meaning
#             "unproven".
MODES = ("off", "audit", "require")


def settle_mode(mode) -> str:
    """`mode` as one of `MODES`, defaulting to the conservative rung.

    Total over junk for the same reason `metric_salvage.settle_mode` is: this value arrives from a
    `run_started` settings snapshot that may have been written by another binary, and an unknown rung
    must not silently become the strictest one (which would make every node of a resumed run
    unselectable) nor the loosest (which would silently stop recording).
    """
    m = str(mode or "").strip().lower()
    return m if m in MODES else "audit"


def _sha256(path: Path, size: int) -> tuple:
    """`(digest_mode, hexdigest)` for `path`, full under `SAMPLE_ABOVE` and sampled above it.

    The sampled preimage is `size || head || tail` — the size is IN the hash, not merely beside it,
    so two files that share a head and a tail but not a length cannot collide by construction. (Which
    is the case the incident's two 92 MB files are one step away from: they are the same size, so on
    THAT pair only the content bytes discriminate, and both fall under the ceiling anyway.)
    """
    h = hashlib.sha256()
    if size <= SAMPLE_ABOVE:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return DIGEST_FULL, h.hexdigest()
    h.update(str(size).encode("ascii"))
    with open(path, "rb") as fh:
        h.update(fh.read(SAMPLE_BYTES))
        fh.seek(max(0, size - SAMPLE_BYTES))
        h.update(fh.read(SAMPLE_BYTES))
    return DIGEST_SAMPLED, h.hexdigest()


def _dir_identity(path: Path) -> tuple:
    """`(entries, total_bytes)` for a directory subject, bounded.

    A subject may legitimately be a DIRECTORY — a SentenceTransformer checkpoint is a directory of
    six files, and `expect.files`/`needs` already accept one. Digesting a whole model directory
    recursively is not affordable and is not what discriminates: the file COUNT and the byte TOTAL,
    plus the directory's own inode, already separate "the checkpoint this node wrote" from "a
    different checkpoint of the same shape" in every case the corpus contains. A caller that needs
    more should declare the specific file (which is what the incident's own subject is).
    """
    entries, total = 0, 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            entries += 1
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                pass
            if entries >= 10_000:
                return entries, total
    return entries, total


def bind_one(workdir, rel: str, *, since: Optional[float] = None,
             producer: Optional[str] = None, confine=None) -> dict:
    """The identity record for ONE declared subject, or `{"bound": False, "reason": …}`.

    `confine` is `command_eval._confined` (injected rather than imported, so this module stays a leaf
    `command_eval` imports and not the other way round). Absent, containment falls back to the same
    resolve-and-compare rule; the injected one is preferred so there is a single definition in play.

    FRESHNESS IS ENFORCED HERE and it is the one predicate the artifact contract's input side
    deliberately does NOT have. `verify_stage_inputs` has no freshness rule on purpose — a stage
    legitimately reads a base checkpoint, a mounted dataset, a deliberately REUSED `train` output. A
    SUBJECT is a different claim: it is what this eval's number is about, so an artifact that predates
    the attempt cannot be it. That asymmetry is the whole reason this is not just another `needs`
    entry.
    """
    p = confine(workdir, rel) if confine is not None else _fallback_confine(workdir, rel)
    if p is None:
        return {"path": rel, "bound": False, "reason": "escapes"}
    try:
        st = os.stat(p)
    except OSError:
        return {"path": rel, "bound": False, "reason": "missing"}
    row: dict = {"path": rel, "bound": True,
                 "identity": list(file_identity(st)),
                 "size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}
    if producer:
        row["producer"] = producer
    try:
        if os.path.isdir(p):
            entries, total = _dir_identity(Path(p))
            if entries == 0:
                return {"path": rel, "bound": False, "reason": "empty"}
            row["kind"] = "dir"
            row["entries"] = entries
            row["bytes"] = total
        else:
            if st.st_size <= 0:
                return {"path": rel, "bound": False, "reason": "empty"}
            row["kind"] = "file"
            mode, digest = _sha256(Path(p), int(st.st_size))
            row["digest_mode"] = mode
            row["digest"] = digest
    except OSError:
        return {"path": rel, "bound": False, "reason": "unreadable"}
    # Freshness LAST: a stale artifact is a real, identified file and the record is more useful for
    # carrying its identity alongside the refusal — an operator debugging "why is my subject stale"
    # needs to see which file the engine looked at, not just the word.
    if since is not None and float(st.st_mtime) < float(since) - 2.0:
        row["bound"] = False
        row["reason"] = "stale"
    return row


def _fallback_confine(workdir, rel):
    try:
        path = (Path(workdir) / rel).resolve()
        root = Path(workdir).resolve()
        return path if (path == root or root in path.parents) else None
    except (OSError, ValueError, RuntimeError):
        return None


def bind(subject, workdir, *, since: Optional[float] = None,
         producers: Optional[dict] = None, confine=None, stage: str = "") -> dict:
    """The `metric_provenance` SUBJECT record for one eval — always a dict, never None.

    Shape (additive to whatever else `metric_provenance` carries; every key is optional to a reader
    and old logs have none of it, which is invariant #5's requirement):

        {"subject_bound": bool,
         "subjects": [ {path, bound, identity, size, mtime_ns, kind, digest, digest_mode,
                        producer?, reason?} … ],
         "unbound_reason": <UNBOUND_REASONS slug>   # only when subject_bound is False
         "subject_stage": "score"}                  # which stage the identity was captured at

    `subject_bound` is True only when EVERY declared subject bound. A metric that is a claim about
    two artifacts is not half-true when one of them is missing, and `unbound_reason` reports the
    first failure so the record names one fix rather than a set.

    `producers` is `command_eval.stage_output_producers(...)` — {declared output path -> the stage
    that promised it}. Recording it is what closes the loop doc 35 §1 describes: given identity,
    `expect` and `needs` become two projections of one relation ("this stage produced/consumed THIS
    artifact") and the write-only asymmetry disappears.
    """
    rels = [r for r in (subject or []) if isinstance(r, str) and r.strip()][:MAX_SUBJECTS]
    if not rels:
        return {"subject_bound": False, "unbound_reason": "not_declared", "subjects": []}
    rows = [bind_one(workdir, rel, since=since, producer=(producers or {}).get(rel), confine=confine)
            for rel in rels]
    bad = next((r for r in rows if not r.get("bound")), None)
    out: dict = {"subject_bound": bad is None, "subjects": rows}
    if bad is not None:
        out["unbound_reason"] = bad.get("reason") or "unreadable"
    if stage:
        out["subject_stage"] = stage
    return out


# What an operator is told when a metric could not be bound. Per-reason, because the fixes are
# genuinely different and a message naming the wrong one sends them hunting the wrong failure — the
# same rule `command_eval._PATHLESS_COST` records for the reader slots.
UNBOUND_MESSAGES = {
    "not_declared": ("this metric names no SUBJECT: `eval.metric.subject` is empty, so the number "
                     "has no recorded referent and nothing can check what it is about. Declare the "
                     "workdir-relative artifact the metric is a claim about, e.g. "
                     "\"subject\": [\"outputs/final/model.safetensors\"]."),
    "escapes": ("the declared subject {path!r} does not resolve inside the node's own workdir. A "
                "metric's subject is by definition something THIS node produced; an absolute path "
                "or a `..` names somebody else's artifact."),
    "missing": ("the declared subject {path!r} was not produced. The metric was read, but the "
                "artifact it is supposed to be about is not there — either the pipeline never wrote "
                "it or the declaration names the wrong path."),
    "empty": "the declared subject {path!r} is empty, so the number cannot be a claim about it.",
    "stale": ("the declared subject {path!r} predates this eval attempt, so this attempt's number "
              "cannot be about it — it is a leftover from an earlier attempt in the reused workdir."),
    "unreadable": "the declared subject {path!r} could not be read to establish its identity.",
}


def unbound_message(prov: Optional[dict]) -> str:
    """One sentence naming the fix for `prov`'s failure, or "" when it bound."""
    if not isinstance(prov, dict) or prov.get("subject_bound"):
        return ""
    reason = str(prov.get("unbound_reason") or "unreadable")
    template = UNBOUND_MESSAGES.get(reason, UNBOUND_MESSAGES["unreadable"])
    path = next((r.get("path") for r in (prov.get("subjects") or []) if not r.get("bound")), "")
    return template.format(path=path) if "{path" in template else template
