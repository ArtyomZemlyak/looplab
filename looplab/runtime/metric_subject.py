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

AND FOR A YEAR THAT MADE THE MECHANISM UNUSABLE ON THE TASK FAMILY IT WAS BUILT FOR
------------------------------------------------------------------------------------
`subject` was a list of LITERAL workdir-relative paths, so declaring one requires the operator to
know the output path at SUBMIT time. Re-measured 2026-08-15 over every `looplab_stages.json` in
`runs/` — the agents' own record of where their pipelines write:

    4    repo runs whose pipeline produces a checkpoint at all (v2, v6, v7, v8)
    17   nodes across them with a declared output
    17   of those land at `vectorsearch/experiments/<AGENT-CHOSEN NAME>_<base-model leaf>/final/…`
    10   DISTINCT names for that one segment (`unified-baseline`, `nllcos_hn`, `dcl-unified`,
         `catdw`, `rdrop-dcl`, `qwen3_hn_v1`, `meanmerge_nllcos_rubert-tiny-lite`, …)

The directory name is `vectorizer-unified/vectorsearch/config.py::run_name`, i.e.
`f"{metadata.run_name}_{train.base_model.split('/')[-1]}"`, and `metadata.run_name` is a value the
AGENT picks per experiment. A single literal declared on v6 would have bound 5 of its 7 nodes and
reported `missing` on the other two; on the live v8 it would have bound **0 of 5**. So the operator
could not have declared a subject on the flagship run, and did not: all three evaluated v8 nodes
record `{'subject_bound': False, 'unbound_reason': 'not_declared'}` under `metric_subject="audit"`.
The mechanism built for the v6-node-4 incident was INERT on exactly the task family that produced it.

THE FIX IS A DECLARED PATTERN THE ENGINE RESOLVES, AND THE WHOLE SAFETY IS UNIQUENESS
--------------------------------------------------------------------------------------
`eval.metric.subject_glob` is a list of workdir-relative GLOB patterns. The operator declares the
SHAPE of the path (`vectorsearch/experiments/*/final/model.safetensors`); the ENGINE walks the real
workdir at the score stage's start and resolves it. Nothing the candidate ASSERTS is consulted — not
its `looplab_stages.json`, not its stdout, not a manifest — only what is on the filesystem, which is
the same authority the literal path already had. So the trust boundary does not move: the operator
still says what the number is about, and the engine still establishes what is there.

**A pattern binds only when it matches EXACTLY ONE path, and this is not a convenience rule.** A
careless glob is strictly WORSE than `not_declared`, because `subject_bound: True` about the wrong
artifact manufactures confidence the record did not have before. It is not hypothetical: each live
v8 node holds FOUR `model.safetensors` of 92,174,712 bytes each — `final/` plus
`checkpoint-2709/`, `checkpoint-3612/`, `checkpoint-4515/` — the incident's own "byte-identical in
SIZE" property, now WITHIN one node. `experiments/*/*/model.safetensors` matches all four. Under the
uniqueness rule that is `ambiguous` and UNBOUND; under any rule that picks one it is a lie. Zero
matches is `missing`; two or more is `ambiguous`, which is a REFUSAL and never a choice.

WHAT IS DELIBERATELY NOT DONE, because each would move the trust boundary:

  * **mtime is never a tie-breaker.** Dropping the stale matches and requiring uniqueness of the
    REST would resolve the "a leftover from a seeded human checkpoint is beside my own" case
    beautifully — and it would promote mtime from a predicate that can only ever REFUSE (`stale`,
    below) into the thing that ELECTS which bytes the number is bound to. The candidate owns its
    workdir and `os.utime` on its own files is fenced by nothing, so an mtime it can set must never
    decide a binding. Freshness stays a refusal, applied to the unique match, exactly as for a
    literal.
  * **the agent's `expect.files` is never consulted** to disambiguate. It is the ONE list that would
    always pick correctly, and it is authored by the candidate: docs/36, a wider action space must
    not widen the trusted set. (`producer` below is still recorded FROM that manifest — it is a
    descriptive note about which stage promised a path, it decides nothing, and it has been on the
    record since this module shipped.)
  * **`**` is not refused at submit.** It is a cost, not a hazard: recursion is exactly what makes a
    pattern match the four same-sized checkpoints, and the uniqueness rule already refuses that.
    Adding a second, weaker guard against a case the first one covers is how two rules come to
    disagree.

WHAT A BOUND PATTERN THEREFORE PROVES, stated narrowly: an artifact with this content identity was
the ONLY thing under the operator's declared shape when the scorer started. It does not prove the
scorer read it (that is the read fence's half, below), and it does not prove the operator meant this
one rather than another the pattern could have matched on a different node — a pattern is a claim
about a SHAPE, and the record carries the resolved path so a reader can check which one it got.

COST (measured on the 92 MB checkpoint, doc 35 §8)
---------------------------------------------------
    os.stat identity                                  ~1 syscall
    sampled digest (size + 1 MiB head + 1 MiB tail)    29.7 ms
    full sha256                                       336.2 ms
Pattern resolution is never the bill, re-measured 2026-08-15 on the real v8 node-1 workdir (median
of 5): `experiments/*/final/model.safetensors` 2.0 ms, `experiments/*/*/model.safetensors` 5.6 ms,
a recursive `**/final/model.safetensors` 32.8 ms — against 338 ms for the bind that follows it.
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
#                   how a metric ends up describing a checkpoint from two attempts ago. NOT the shape
#                   where the engine ITSELF reused the earlier stages (`start_stage`) — there the
#                   older artifact is the subject on purpose, and the caller passes no floor at all
#                   (`command_eval.attempt_freshness_floor`). This slug is a claim about an artifact
#                   nobody chose to reuse.
#   unreadable    — it exists and could not be stat'ed/digested (a FUSE error, a permission).
#   ambiguous     — a declared `subject_glob` matched MORE THAN ONE artifact, so nothing here can say
#                   which of them the number is about. It is its own slug and not `missing`, because
#                   the fixes are opposite: `missing` means produce the file, `ambiguous` means
#                   narrow the pattern (or the pipeline wrote two candidates and the operator has to
#                   decide which is the result). It is also the slug that keeps this mechanism honest
#                   — a pattern that resolved to one of four same-sized checkpoints by picking would
#                   record `subject_bound: True` about the wrong bytes, which is worse than the
#                   `not_declared` it replaced.
UNBOUND_REASONS = ("not_declared", "escapes", "missing", "empty", "stale", "unreadable", "ambiguous")

# How many matches a pattern's resolution enumerates before it stops. Two is enough for the DECISION
# (one binds, more than one refuses); the rest are collected only so the refusal can SHOW the
# operator what it matched, which is the difference between "narrow your pattern" and a slug they
# have to go and reproduce by hand. The walk stops here, so a `**` over a materialized repo pays a
# bounded number of matches rather than the whole tree's worth.
MAX_GLOB_MATCHES = 8

# The rungs, in increasing strictness. See `Settings.metric_subject` for which is the default and for
# the evidence that would move it.
#
#   off     — record nothing. Byte-identical to the behaviour before this shipped.
#   audit   — RECORD, always: every node_evaluated carries `metric_provenance`, bound or not. This is
#             the half that turns 82/83-with-no-referent into 0/83, and it costs one stat plus a
#             bounded digest. No violation, no selection effect, and — since 2026-08-14 — no STAGE
#             CONTRACT either: the score stage's derived `needs` belongs to `require` alone.
#   require — the INVERSION, and the ONLY rung that gates. An unbound metric gets the existing
#             `metric_salvaged` violation row, so it is counted, visible in the UI and the lineage,
#             and never selectable; and `engine/eval_stages.py` derives a `needs` entry from the
#             declared subject onto the protected `score` stage, so a subject the pipeline did not
#             produce refuses BEFORE the scorer runs instead of after. The absence of provenance
#             stops meaning "fine" and starts meaning "unproven".
#
# THE RUNG ORDER IS A PROPERTY, not a presentation. Each rung must do everything the one below it
# does and strictly more, and until 2026-08-14 the middle rung broke that: the derived `needs` fired
# for every mode but `off`, so a declared-but-missing subject under `audit` returned `needs_failed`
# with `metric=None` — the node lost its number outright, where `require` is documented to KEEP the
# metric and record a violation against it. The mildest rung had the harder effect. `audit` is what
# an operator turns on to measure whether `require` is affordable (see `Settings.metric_subject`'s
# "THE EVIDENCE THAT WOULD JUSTIFY FLIPPING IT"), so a selection effect there does not merely
# contradict the text — it destroys the reason the rung exists.
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

    WHAT `since` MEANS, AND WHY THE CALLER DERIVES IT. `since` is "the floor for THIS attempt", and
    on exactly one attempt shape there is no such floor: a stage-scoped re-run, where the engine
    itself skipped the earlier stages (`command_eval::reused_stage_count`) so the previous attempt's
    checkpoint IS this attempt's subject. `command_eval.attempt_freshness_floor` owns that decision
    and passes `None` there — one derivation shared with the secondary readers' relaxation, so a
    reuse cannot keep its constraint readers and lose its metric's referent. This module does not
    re-derive it: it cannot see the stage list, and a leaf that guessed at the caller's reuse
    decision is how the two came to disagree in the first place.
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


def resolve_glob(workdir, pattern: str) -> Optional[list]:
    """The workdir-relative paths a declared `subject_glob` matches, sorted, at most
    `MAX_GLOB_MATCHES` + 1 of them — or None when the pattern could not be walked at all.

    ONE match is a subject; anything else is a refusal, so the caller only needs to tell "0", "1" and
    "more than 1" apart. The extra entry past the cap is what lets it say "more than 1" without
    claiming a total it never counted.

    Resolution is a plain filesystem walk and reads NOTHING the candidate wrote about itself. That is
    the property that keeps a pattern exactly as trustworthy as the literal path it replaces: the
    operator declares the shape, the engine establishes what is there.

    `Path.glob` and not `fnmatch` over a `os.walk`: the pattern is matched SEGMENT by segment against
    real directory entries, so `experiments/*/final/model.safetensors` never descends into
    `checkpoint-4410/`, and a pattern naming a directory that does not exist costs one failed
    `scandir` rather than a walk of everything that does.
    """
    root = Path(workdir)
    out: list = []
    try:
        it = root.glob(str(pattern))
        for p in it:
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:                       # pragma: no cover — glob yields under root
                continue
            out.append(rel)
            if len(out) > MAX_GLOB_MATCHES:
                break
    except (OSError, ValueError, IndexError, NotImplementedError, RuntimeError):
        # Total over a malformed pattern for `_confined`'s reason: a bad declaration must fail the
        # NODE's binding, never take down the run. `ValueError` is what pathlib raises for an
        # absolute or empty pattern — both already refused at submit, but a `_grandfathered`
        # snapshot reload never re-validates, so this path is reachable from a resumed run.
        return None
    return sorted(out)


def bind_glob(workdir, pattern: str, *, since: Optional[float] = None,
              producers: Optional[dict] = None, confine=None) -> dict:
    """The identity record for ONE declared `subject_glob`, or `{"bound": False, "reason": …}`.

    THE UNIQUENESS RULE IS HERE and it is the whole safety argument for patterns (see the module
    docstring). Exactly one match is bound, THROUGH `bind_one`, so a resolved pattern and a literal
    path get the SAME containment, identity, digest, producer and freshness treatment — a pattern
    changes only HOW the path is named, never what is recorded about it or which predicates it must
    pass. That is also why nothing here pre-filters the match set: `Path.glob` follows a symlinked
    directory, so a match CAN land outside the workdir, and dropping those before counting would let
    a candidate de-ambiguate its own subject by making the other matches escape. A match that is not
    confined is a REFUSAL (`bind_one` returns `escapes`), never a quiet exclusion — the same shape as
    the mtime rule one predicate over: what the candidate controls may only ever refuse a binding,
    and may never elect one.
    """
    matches = resolve_glob(workdir, pattern)
    if matches is None:
        return {"glob": pattern, "path": "", "bound": False, "reason": "unreadable", "matched": []}
    if not matches:
        return {"glob": pattern, "path": "", "bound": False, "reason": "missing", "matched": []}
    if len(matches) > 1:
        return {"glob": pattern, "path": "", "bound": False, "reason": "ambiguous",
                "matched": matches[:MAX_GLOB_MATCHES],
                "matched_truncated": len(matches) > MAX_GLOB_MATCHES}
    row = bind_one(workdir, matches[0], since=since, producer=(producers or {}).get(matches[0]),
                   confine=confine)
    row["glob"] = pattern
    return row


def _fallback_confine(workdir, rel):
    try:
        path = (Path(workdir) / rel).resolve()
        root = Path(workdir).resolve()
        return path if (path == root or root in path.parents) else None
    except (OSError, ValueError, RuntimeError):
        return None


def bind(subject, workdir, *, since: Optional[float] = None,
         producers: Optional[dict] = None, confine=None, stage: str = "",
         globs=None) -> dict:
    """The `metric_provenance` SUBJECT record for one eval — always a dict, never None.

    Shape (additive to whatever else `metric_provenance` carries; every key is optional to a reader
    and old logs have none of it, which is invariant #5's requirement):

        {"subject_bound": bool,
         "subjects": [ {path, bound, identity, size, mtime_ns, kind, digest, digest_mode,
                        producer?, reason?, glob?, matched?, matched_truncated?} … ],
         "unbound_reason": <UNBOUND_REASONS slug>   # only when subject_bound is False
         "subject_stage": "score"}                  # which stage the identity was captured at

    `subject_bound` is True only when EVERY declared subject bound. A metric that is a claim about
    two artifacts is not half-true when one of them is missing, and `unbound_reason` reports the
    first failure so the record names one fix rather than a set.

    `globs` is `EvalSpec.metric["subject_glob"]` — the operator's PATTERN declaration, for the task
    family whose output path the agent names (the module docstring measures it: 17 of 17 nodes with a
    declared output, 10 distinct agent-chosen directory names, so no literal could be written at
    submit). Each pattern is resolved by `bind_glob` against the real workdir and binds only when it
    matches exactly one artifact. A resolved match then goes through the SAME `bind_one` as a literal
    — identity, digest, producer, freshness — so the two declaration shapes differ in how the path is
    NAMED and in nothing else. Literals are bound first so a record listing both reads in the order
    the task file writes them.

    `producers` is `command_eval.stage_output_producers(...)` — {declared output path -> the stage
    that promised it}. Recording it is what closes the loop doc 35 §1 describes: given identity,
    `expect` and `needs` become two projections of one relation ("this stage produced/consumed THIS
    artifact") and the write-only asymmetry disappears. It is DESCRIPTIVE and never a tie-breaker:
    that map comes from the agent's own manifest, and `bind_glob` resolving an ambiguity through it
    would let the candidate choose which of its checkpoints the number is bound to.
    """
    rels = [r for r in (subject or []) if isinstance(r, str) and r.strip()][:MAX_SUBJECTS]
    pats = [g for g in (globs or []) if isinstance(g, str) and g.strip()][:MAX_SUBJECTS]
    if not rels and not pats:
        return {"subject_bound": False, "unbound_reason": "not_declared", "subjects": []}
    rows = [bind_one(workdir, rel, since=since, producer=(producers or {}).get(rel), confine=confine)
            for rel in rels]
    rows += [bind_glob(workdir, pat, since=since, producers=producers, confine=confine)
             for pat in pats[:max(0, MAX_SUBJECTS - len(rows))]]
    bad = next((r for r in rows if not r.get("bound")), None)
    out: dict = {"subject_bound": bad is None, "subjects": rows}
    if bad is not None:
        out["unbound_reason"] = bad.get("reason") or "unreadable"
    if stage:
        out["subject_stage"] = stage
    return out


def absent_declaration() -> dict:
    """The provenance record for a metric whose task declared NO subject at all.

    A named function rather than a dict literal at the engine's call site, because it is the rule
    that decides the universal case: `not_declared` is the state 82 of 83 corpus metrics are in, so a
    `require` rung that recorded only MIS-declared subjects would fire on the exception and never on
    the rule. A rule nobody can state is a rule nobody reviews (CLAUDE.md's guard-test ladder, tier
    2), and this one is otherwise reachable only through a whole simulated eval.

    It is deliberately identical in shape to what `bind([])` returns — the engine and the runtime
    must not have two spellings of "no subject" for a reader to have to recognise. "No subject" now
    means neither declaration shape: a task with only a `subject_glob` is DECLARED, and reaching this
    record for it would report the operator's own pattern as an absent declaration.
    """
    return bind([], "", since=None, globs=[])


# What an operator is told when a metric could not be bound. Per-reason, because the fixes are
# genuinely different and a message naming the wrong one sends them hunting the wrong failure — the
# same rule `command_eval._PATHLESS_COST` records for the reader slots.
UNBOUND_MESSAGES = {
    "not_declared": ("this metric names no SUBJECT: `eval.metric.subject` is empty, so the number "
                     "has no recorded referent and nothing can check what it is about. Declare the "
                     "workdir-relative artifact the metric is a claim about, e.g. "
                     "\"subject\": [\"outputs/final/model.safetensors\"] — or, when the pipeline "
                     "names its own output directory, the SHAPE of it with "
                     "\"subject_glob\": [\"outputs/*/final/model.safetensors\"]."),
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
    # The ONE message that must name what it matched. "Ambiguous" alone sends the operator to look at
    # a workdir that no longer exists on a finished run; the paths are the whole content of the fix,
    # and they are also the evidence for the refusal — an operator who sees `final/` beside three
    # `checkpoint-*/` of identical size can tell at a glance that binding either would have been a
    # coin toss dressed up as provenance.
    "ambiguous": ("the declared subject pattern {path!r} matched MORE THAN ONE artifact ({matched}), "
                  "so nothing records which of them the number is about — the pattern was NOT bound "
                  "to any of them, because picking one would record a referent nobody chose. Narrow "
                  "it (e.g. name the `final/` directory rather than every `checkpoint-*/`), or "
                  "declare a literal `subject` if the pipeline's output path is fixed."),
}


def unbound_message(prov: Optional[dict]) -> str:
    """One sentence naming the fix for `prov`'s failure, or "" when it bound."""
    if not isinstance(prov, dict) or prov.get("subject_bound"):
        return ""
    reason = str(prov.get("unbound_reason") or "unreadable")
    template = UNBOUND_MESSAGES.get(reason, UNBOUND_MESSAGES["unreadable"])
    row = next((r for r in (prov.get("subjects") or []) if not r.get("bound")), {})
    # The DECLARATION is what the operator has to edit, so a failed pattern names the pattern and not
    # the empty `path` slot a glob row carries — the two shapes share one message table and a `{path}`
    # that rendered `''` would name nothing at all.
    path = row.get("glob") or row.get("path") or ""
    matched = ", ".join(str(m) for m in (row.get("matched") or []))
    if row.get("matched_truncated"):
        matched += ", …"
    return template.format(path=path, matched=matched) if "{path" in template else template
