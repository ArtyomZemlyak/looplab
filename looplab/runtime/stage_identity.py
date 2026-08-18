"""STAGE IDENTITY — what a stage's inputs WERE and what its outputs ARE, derived by the engine.

WHY THIS EXISTS, AND WHY IT IS NOT A CACHE
------------------------------------------
The question that produced this module was "why does `mine` (hard-negative mining) recompute per node
when a sibling already ran a byte-identical configuration?", and the proposed key was the node's
DECLARED mining parameters (`idea.params`, e.g. `{mining_type: 1, n_negatives: 2}`). Re-measured
2026-08-17 over every `stage_finished` row and every preserved node workdir in `runs/`:

    34    `mine` stage runs, 64,908.5 s = 18.03 h        (of 246.1 h of stage time corpus-wide, 7.3 %)
    17    succeeded, mean 46.7 min;  7 (20.6 %) `reused`, ALL of them within one node's own retry
    13    of those runs sit on a node whose manifest declares a `mine` stage and whose workdir survives

Grouping those 13 by the node's DECLARED mining params and letting each group's first node feed the
rest — the proposed key — and then checking the answer against the sha256 of the artifact each node
ACTUALLY produced:

    7 hits, **4 of them WRONG** (57 %), 6.15 h "saved"

`rubertlite-dr-unified-v8` nodes 3, 4 and 8 all declare `{mining_type: 1, n_negatives: 2}` and their
mined parquets are `13db4477…`, `34e9ca5e…` and `c665f57b…` — three different negative sets. Nodes 5,
6, 9 and 14 declare NO mining params at all and DO mine; nodes 1, 7 and 12 declare the full mining
block and run no `mine` stage. **The declaration is decoration relative to what the stage computes**,
which is `engine/repair_verify.py::declared_param_overrides` one file over and docs/36's rule in
general: what the candidate asserts about itself may REFUSE a binding and may never ELECT one.

So the key had to be derived from bytes. `stage_input_key` below is that derivation. Replayed over
the same 13 runs it makes ONE hit, it is correct, and it saves 2,288 s = 0.64 h — 3.5 % of the `mine`
corpus and 0.26 % of all stage time. **The cache was therefore NOT built**; see `docs/BACKLOG.md`
§0.12 for the alternatives and the arithmetic. The reason a sound key cannot pay here is not the key:
it is that the agent edits at NODE granularity. Nodes 3, 9, 10, 11 and 14 produced a byte-identical
79,586,058-byte parquet while their mine-stage import closures differ in `vectorsearch/config.py` and
their workdirs differ in `vectorsearch/configs/config.yaml` — files the mining script really does
read. Only the OUTPUT is identical, and an output is not something a key may consult.

WHAT IS BUILT IS THE INSTRUMENT
-------------------------------
Both halves of that measurement took a 20-minute sha256 sweep over 20 GB of preserved workdirs, which
is why nobody had it and why the wrong key looked obviously right. The engine now derives and records
both facts per stage, so the next person asking this question reads it off the event log:

  * `stage_input_key`  — the reuse key a cache WOULD consult, or None when it cannot be bounded.
  * `stage_output_identity` — the content identity of what the stage declared and produced, bound at
    the instant the artifact contract passed.

Together they make the two numbers that decide whether a cache may ever ship computable from a live
run: equal keys are candidate hits, and equal keys with UNEQUAL outputs are WRONG hits. `looplab
stage-dups <run_dir>` reports both. This records; it decides nothing. No metric, champion,
selectability or violation can move on it.

AND IT HAD TO BE A WRITER RATHER THAN A SCRIPT OVER `runs/`, which is the other half of why this
module exists. A key describes the workdir as it stood BEFORE its stage ran; every preserved workdir
is in its FINAL post-run state, holding artifacts that did not exist at that instant. Replayed
literally against the final state the derivation below makes ZERO hits on this corpus, and that zero
is an artefact of the replay — the 1/0 row above is a reconstruction. The only place a key's real
yield is observable is a live run that records it while it is still true.

THE KEY, AND WHY EACH PART IS IN OR OUT
---------------------------------------
IN — every one of these is something the ENGINE reads for itself, never something a candidate says:

  * the stage's own validated manifest ENTRY: `name`, `command` (the resolved argv), `expect`,
    `needs`, `env`. `expect` is in the key including its `assert` sentence, because a reuse skips the
    success contract as well as the command: a node declaring a DIFFERENT success condition must not
    inherit a sibling's pass.
  * the CONTENT digest of every file in `_stage_reachable_files(stages[:index+1], workdir)` — the
    stage's entry script plus the transitive closure of its workdir-local imports, the same closure
    `_safe_reuse_start` already trusts for the opposite question.
  * the CONTENT digest of every OTHER file in the workdir that is not a `.py` and not a declared
    output of this stage or a later one. This is the clause that makes the key sound at all. The
    import closure bounds only PYTHON imports, and `needs` is a PRECONDITION and not a bound
    (`verify_stage_inputs` stats what is declared; nothing anywhere checks the converse, and
    `read_fence` never fences the workdir) — so "a config the stage reads without declaring it" is
    exactly the blind spot `_safe_reuse_start` fails closed on. The key closes it by INCLUSION rather
    than by trust: everything the closure cannot see is taken verbatim.
  * `KEY_SCHEMA` — this derivation's own version. A changed derivation revokes every issued key
    rather than silently colliding with one minted under the old rule
    (`search/speculation_quality.py`'s rule for the same reason).
  * a caller-supplied `scope` — the run/workspace identity. A key never matches across runs, because
    everything OUTSIDE the workdir (the dataset mount, the model cache, site-packages, the
    interpreter) is not covered and cannot be; see the residual below.

OUT, each for a stated reason:

  * `idea.params` and every other agent DECLARATION about itself — measured 4 wrong of 7 above.
  * the stage `timeout`. It is a leash on how long the stage may run, not an input to what it
    computes, and a stage that hit it never completes and so can never become a reuse SOURCE.
  * `check`. It selects whether the LLM sanity read runs; it cannot change the bytes.
  * anything outside the workdir. THE RESIDUAL IS STATED, NOT PATCHED: this key is sound only within
    one run's materialization on one box at one moment. A dataset mount that changed under the run,
    an upgraded site-package, a different GPU — none of them move the key. That is why `scope` is
    REQUIRED and why the key must never be carried across runs or across machines.

FAIL-CLOSED, at least as strictly as `_safe_reuse_start`
--------------------------------------------------------
`stage_input_key` returns None — never a key, therefore never a hit — on every condition that
predicate refuses on, and on one more:

  * an OPAQUE earlier stage (`_stage_reachable_files` answered None: a shell wrapper, a bare binary,
    a `python -m` naming installed code). Same clause, same reason.
  * a non-default eval `cwd`: changed-file keys and stage-script paths resolve against different
    bases, so nothing derived from either proves anything about the other.
  * a workdir file that cannot be read. `_safe_reuse_start` never had this clause because it compares
    NAMES; a content key that silently skipped an unreadable file would be a key over a smaller set
    than it claims, which is the shape that makes two different workdirs collide.
  * an entry point that resolves to NO file under the workdir (`unresolved_entry`). Also new here:
    `_stage_reachable_files` credits any argv token ending in `.py` as a script, so
    `sh -c "python mine.py"` answers "bounded" with a closure holding one phantom. Harmless there —
    a phantom only ever ADDS to a set used to REFUSE reuse — and not harmless in a key.

It does NOT need `_safe_reuse_start`'s deletion clause and its non-`.py` clause, and that difference
is the point rather than a loosening. Those two exist because that predicate reasons over a CHANGE
SET of file names and can neither see a vanished module nor bound a non-`.py` read. A key over the
CONTENT of the whole workdir has no change set: a deleted file is absent from the digest map and a
config is in it, so both cases are decided by the key differing, which refuses the reuse.

WHAT A WRONG HIT WOULD COST, and why the artifact check is stricter than `expect`'s
------------------------------------------------------------------------------------
A stale negatives set feeding a 5-hour training silently produces a number at coordinates the run
never occupied — worse than the 47 minutes it saves, and unrecoverable after the fact, because
nothing downstream records what the negatives were. So `reuse_refusal` (the decision half, which the
reporter drives and no cache consumes yet) requires more than the key: every declared output of the
candidate row must STILL have the exact `file_identity` AND the exact digest the row recorded.
`verify_stage_artifacts` proves "written after this stage's start", which is the right rule for a
stage that RAN; it cannot distinguish the artifact this key names from a different one written since.
This is `metric_subject`'s rule applied to reuse: presence and freshness may refuse a binding, only
content may establish one.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from looplab.core.jsonutil import canonical_json_digest
# THE DIGEST RULE IS metric_subject's, IMPORTED AND NOT RE-DERIVED. Two content-identity derivations
# in one runtime that disagreed about "the digest of this file" is how a reuse key and the metric's
# own subject record come to describe the same bytes differently — and the sampled preimage
# (`size || head || tail`, size IN the hash) is exactly the property that must not drift. Above
# `SAMPLE_ABOVE` a file is digested head+tail and the MODE rides in the preimage, so a sampled entry
# can never be mistaken for a full one. Same package, and `metric_subject` reaches `command_eval`
# only through deferred imports, so this closes no cycle.
from looplab.runtime.metric_subject import SAMPLE_ABOVE, SAMPLE_BYTES, _sha256

# THE DERIVATION'S OWN VERSION. Bump it for ANY change to what goes into the preimage or how it is
# canonicalized. A key is an identity: a widened derivation that kept the old version would let a key
# minted under the old rule answer a question the new rule asks differently, which is exactly the
# silent collision the whole module exists to avoid.
KEY_SCHEMA = "stage-input-key/v1"

# The three stage-row keys, named beside `command_eval.EXPECT_SINCE_KEY`/`EXPECT_DECLARED_KEY` and
# additive in the same way: `replay._on_stage_finished` copies only name/status/exit_code/seconds out
# of a stage row, so these ride on `stage_finished` without changing folded state and a reader of an
# older log sees them absent. Nothing in the engine branches on any of them.
STAGE_INPUT_KEY = "stage_input_key"
STAGE_OUTPUTS_KEY = "stage_outputs"
# Why there is no key, when there is none. Recorded rather than left implicit because "this stage has
# no key" and "this stage's key did not match" are the two halves of a cache's yield, and only the
# first one names something anybody could fix.
STAGE_KEY_REASON = "stage_key_reason"

# Files the key deliberately does NOT digest, because the ENGINE writes them and they move for
# reasons that have nothing to do with what a stage computes. Each is a per-attempt sidecar the
# engine itself owns; including them would make every key unique by construction, which reads as "no
# duplication exists" rather than as "the instrument is broken".
ENGINE_SIDECARS = (".looplab-manifest", ".looplab-metrics-attempt.json", ".looplab-run-fence",
                   "looplab_stages.json")
# Directory names never walked. `__pycache__` is a derived artifact of the very `.py` files already
# in the key; `.git` is the seeded repo's history, which no stage reads; the rest are dependency
# trees that a stage may read but that the run does not author and cannot change mid-run.
SKIP_DIRS = frozenset({"__pycache__", ".git", ".hg", ".svn", "node_modules", ".venv", "venv",
                       ".mypy_cache", ".pytest_cache", ".ruff_cache"})
# Suffixes never digested: a stage's own log is written BY the stage, so digesting it would make a
# stage's key depend on its own output.
SKIP_SUFFIXES = (".log", ".pyc", ".pyo")
# The workdir walk's bound. A workdir with more files than this is not keyed at all (None), rather
# than keyed over a truncated set — a key over "the first N files" is a key that two different
# workdirs share. Measured on this box: the repo-task workdirs hold 78-79 keyable files.
MAX_KEYED_FILES = 20_000
# …and the same bound in BYTES, which is the one that actually binds. A key is derived on the LIVE
# eval path, once per stage, and the per-file `SAMPLE_ABOVE` ceiling bounds nothing about a workdir
# holding two hundred 200 MB shards. Measured on this box: the largest real repo-task workdir
# digests 1,017 MB across 144 files (three `checkpoint-NNNN/optimizer.pt` at 183.9 MB plus four
# `model.safetensors` at 92.2 MB), at ~70 MB/s on the geesefs mount workdirs live on — so 4 GiB is
# 4x the largest real workdir and ~60 s at the ceiling, i.e. 2 % of the SHORTEST real stage this
# instrument runs beside (a 2,290 s `mine`) and 0.3 % of a train. Over it there is NO key, not a
# partial one: a key we cannot afford to compute is a key we do not mint, and the running total is
# checked after the `lstat` and BEFORE the digest, so crossing the ceiling costs at most the ceiling.
MAX_KEYED_BYTES = 4 * 1024 * 1024 * 1024
# Why a stage has no key. A CLOSED vocabulary for the same reason `metric_subject.UNBOUND_REASONS` is
# one: this slug rides on the durable stage row and a reader must be able to tell "the entry point is
# opaque" (a modelling limit that a widened `_module_entry_candidates` could fix) from "a file would
# not read" (a filesystem fault) from "no manifest" (this eval has no pipeline at all).
KEY_REASONS = ("no_stage", "opaque_entry", "unresolved_entry", "non_default_cwd",
               "unreadable_workdir", "too_many_files", "too_many_bytes", "unencodable")


def declared_outputs(stages: list, *, at: int = 0) -> set:
    """Every path declared as an `expect.files` output by stage `at` or any LATER stage.

    Excluded from the key's workdir sweep, and the exclusion has to be forward-looking rather than
    "this stage's own outputs": on a REPAIRED node the workdir deliberately persists, so a later
    stage's artifact from a previous attempt is sitting there while THIS stage is about to re-run.
    Digesting it would make the key depend on output the reuse candidate has not produced yet, i.e.
    two runs of the identical pipeline would key differently purely because one of them had failed
    later on. The paths are taken from the manifest the engine VALIDATED, so this is a read of the
    engine's own parse and not of anything the candidate asserts at eval time.
    """
    from looplab.runtime.command_eval import normalize_declared_path
    out: set = set()
    for s in (stages or [])[at:]:
        if not isinstance(s, dict):
            continue
        exp = s.get("expect") if isinstance(s.get("expect"), dict) else {}
        for rel in (exp.get("files") or []):
            out.add(normalize_declared_path(rel))
    return out


def workdir_content(workdir, *, exclude: set) -> tuple:
    """`({workdir-relative path -> "<mode>:<digest>"}, "")` for every keyable file, or `(None, why)`.

    THE REASON IS RETURNED AND NOT SWALLOWED, and the test that added this signature is what found
    that it had to be: with a bare `None` the caller mapped every refusal onto `unreadable_workdir`,
    so `too_many_files` sat in `KEY_REASONS` unreachable and an operator reading a row could not tell
    a filesystem fault from a workdir the instrument declined to afford. Three different facts, three
    different remedies.

    None on ANY file that will not read and on a tree above `MAX_KEYED_FILES` or `MAX_KEYED_BYTES`,
    because a key over a partial map is a key two different workdirs can share. The unreadable-file
    clause is the one `_safe_reuse_start` does not have, and it is needed exactly because this
    predicate reasons over CONTENT where that one reasons over names: an unreadable name is still a
    name. The two ceilings are a COST bound rather than a soundness one, and they fail the same way
    for the same reason — a truncated map is not a cheaper key, it is a wrong one.
    """
    total = 0
    from looplab.runtime.command_eval import normalize_declared_path
    root = Path(workdir)
    out: dict = {}
    try:
        walker = os.walk(root)
        for dirpath, dirnames, filenames in walker:
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                full = Path(dirpath) / name
                try:
                    rel = normalize_declared_path(str(full.relative_to(root)))
                except ValueError:                       # outside the root — a walk that escaped
                    return None, "unreadable_workdir"
                if not rel or rel in ENGINE_SIDECARS or rel in exclude:
                    continue
                if rel.endswith(SKIP_SUFFIXES):
                    continue
                if len(out) >= MAX_KEYED_FILES:
                    return None, "too_many_files"
                try:
                    st = os.lstat(full)
                except OSError:
                    return None, "unreadable_workdir"
                # AFTER the stat and BEFORE the digest, so crossing the ceiling costs at most the
                # ceiling rather than the whole tree.
                total += min(int(st.st_size), SAMPLE_ABOVE)
                if total > MAX_KEYED_BYTES:
                    return None, "too_many_bytes"
                # A SYMLINK is keyed by its TARGET TEXT and never followed. Following it would either
                # digest something outside the workdir (which the key does not claim to cover — see
                # the residual in the module docstring) or, on a `mount:true` data source, digest a
                # multi-gigabyte dataset on every stage. The link's own text is what the candidate
                # authored and is exactly what changes when it re-points.
                if os.path.islink(full):
                    try:
                        out[rel] = "symlink:" + os.readlink(full)
                    except OSError:
                        return None, "unreadable_workdir"
                    continue
                if not os.path.isfile(full):
                    continue
                try:
                    mode, digest = _sha256(full, int(st.st_size))
                except OSError:
                    return None, "unreadable_workdir"
                out[rel] = f"{mode}:{digest}"
    except OSError:
        return None, "unreadable_workdir"
    return out, ""


def stage_entry(stage: dict) -> dict:
    """The part of a stage's validated manifest entry that decides what it COMPUTES.

    `timeout` and `check` are deliberately absent — see the module docstring. `expect` is present in
    FULL, `assert` included: a reuse skips the success contract along with the command, so a node
    declaring a different success condition must not inherit a sibling's pass.
    """
    s = stage if isinstance(stage, dict) else {}
    return {"name": str(s.get("name") or ""),
            "command": [str(t) for t in (s.get("command") or [])],
            "expect": s.get("expect") if isinstance(s.get("expect"), dict) else None,
            "needs": list(s.get("needs") or []) or None,
            "env": s.get("env") if isinstance(s.get("env"), dict) else None}


def stage_input_key(stages: list, index: int, workdir, *, scope: str, reachable,
                    cwd=None) -> tuple:
    """`(key, reason)` — the reuse key for `stages[index]`, or `(None, <KEY_REASONS member>)`.

    `scope` binds the key to ONE run's materialization and is required, not defaulted: everything
    outside the workdir is uncovered (the module docstring says which), so a key that could match
    across runs would be asserting a comparability this derivation cannot support.

    `reachable` is `EvalStagesMixin._stage_reachable_files(stages[:index+1], workdir)` — the import
    closure, or None for an opaque entry point — and is INJECTED rather than imported, for the same
    reason `metric_subject.bind_one` takes `confine`: that function lives in `looplab/engine/` and
    `runtime` imports nothing above `core`. One closure rule, computed by the layer that owns it.

    The rest of `looplab_stages.json` is deliberately NOT in the key (`ENGINE_SIDECARS`): a change to
    a LATER stage's entry cannot alter what this one computes, and an EARLIER stage's entry is
    already accounted for by the artifacts it left in the workdir content map, which is what this
    stage actually reads. That is the one narrowing this key makes against `_safe_reuse_start`'s
    "never reuse across a manifest change", and it is narrowing a NAME clause into a CONTENT one:
    the argv of every stage the reuse would skip is the argv this key already carries verbatim.
    """
    if not stages or not isinstance(stages, list) or not (0 <= index < len(stages)):
        return None, "no_stage"
    # Same clause and same words as `_safe_reuse_start`: under a subdir cwd the stage's script paths
    # and the workdir-relative digest map resolve against different bases, so neither proves anything
    # about the other. Fail closed rather than guess the remapping.
    if cwd not in (None, "", "."):
        return None, "non_default_cwd"
    if reachable is None:
        return None, "opaque_entry"
    # REVIEW 2026-08-18 (efficiency): the full-tree digest runs BEFORE the `unresolved_entry` check
    # below, which depends only on `reachable` and a handful of `exists()` probes — so the wrapper
    # shape that check exists for (`sh -c "python mine.py"`, 10 of the 39 corpus rows per the comment
    # below) pays the multi-second `workdir_content` walk on every stage of every attempt only to be
    # refused unconditionally afterwards. Hoisting the reachable-only existence probe above this call
    # skips the digest for the always-refused shape and costs a normal stage one short-circuiting
    # `exists()`; keep it `exists()`-based rather than content-map membership, since the map excludes
    # declared outputs/sidecars and would mis-answer for an entry the map deliberately omits.
    content, why = workdir_content(workdir, exclude=declared_outputs(stages, at=index))
    if content is None:
        return None, why
    # A SECOND opacity clause, and it is here rather than in `_stage_reachable_files` because that
    # function is the LIVE reuse decision and its reach must not move for an instrument's sake.
    # It credits any argv token ENDING in `.py` as a script, so `sh -c "python mine.py"` — the
    # wrapper shape 10 of the 39 corpus rows have — yields a closure holding the phantom entry
    # `python mine.py`, i.e. it answers "bounded" about a shell string it never parsed. Harmless
    # there (a phantom only ever ADDS to a set that is used to REFUSE reuse) and not harmless here,
    # where a key must never claim to have bounded an entry point it did not find. So: at least one
    # member of the closure has to be a file that really exists under this workdir.
    if not any((Path(workdir) / str(f)).exists() for f in reachable):
        return None, "unresolved_entry"
    # The closure is named EXPLICITLY beside the whole-workdir map rather than folded into it. Both
    # cover the same files, and that is the point: the map is what makes the key SOUND (it sees the
    # config the closure cannot), the closure is what makes the key READABLE — `looplab stage-dups`
    # can say WHICH file separated two nearly-identical stages, and on this corpus the answer is
    # `vectorsearch/config.py`, which is a fact about the search and not about the cache.
    # TWO MAPS, and the split IS the trust model. `closure` carries the CONTENT of every `.py` the
    # entry point can reach by import — the same bound `_safe_reuse_start` already trusts, used in
    # the same direction — and a reachable path that is not on disk is recorded as `absent` rather
    # than dropped, so a vanished module changes the key instead of shrinking it. `content` carries
    # every NON-`.py` file, verbatim, because the closure is a bound on IMPORTS and nothing else:
    # `needs` is a precondition and not a bound, `read_fence` never fences the workdir, so a config
    # the stage reads without declaring it is exactly what `_safe_reuse_start` fails closed on and
    # exactly what this includes instead.
    #
    # A `.py` OUTSIDE the closure is therefore NOT in the key, and that is the one place this key
    # rests on the closure's own reach: a dynamic `importlib`/`exec`/`runpy` load escapes it, the
    # same exposure `_safe_reuse_start` has and with the same consequence. The alternative was
    # driven, not assumed — digesting every `.py` in the workdir takes the corpus yield from ONE
    # correct hit to ZERO (v8 nodes 3 and 10 mined a byte-identical parquet and differ only in
    # `train.py` / `loss.py` / `collator.py` / `samplers.py`, none of which a `mine` stage imports),
    # so the strictly-stricter version is not free: it costs the instrument its ability to tell
    # "no duplication" from "the key is too tight". That margin is the measurement, and it is the
    # reason `docs/BACKLOG.md` §0.12 concludes what it concludes.
    closure = {str(f): content.get(str(f), "absent") for f in sorted(str(x) for x in reachable)}
    non_py = {k: v for k, v in content.items() if not k.endswith(".py")}
    preimage = {"schema": KEY_SCHEMA, "scope": str(scope), "stage": stage_entry(stages[index]),
                "closure": closure, "content": non_py}
    key = canonical_json_digest(preimage, prefix="stage1:")
    if key is None:
        return None, "unencodable"
    return key, ""


def stage_output_identity(expect, workdir, since: Optional[float], *, stage: str = "") -> Optional[list]:
    """The content identity of every artifact this stage DECLARED, or None when it declared none.

    Bound through `metric_subject.bind_one`, so a stage's outputs and the metric's subject are
    described by ONE derivation — same digest rule, same `file_identity` tuple, same freshness
    semantics — rather than by two that could come to disagree about the same bytes.

    `since` is the stage's OWN wall-clock start, the identical floor `verify_stage_artifacts` was
    just held to (`EXPECT_SINCE_KEY`). Called only after that check PASSED, so every entry here is
    already known present, non-empty and fresh; what this adds is WHICH BYTES, which is the only
    thing that can later prove a reuse candidate is still the artifact its key names.
    """
    files = list((expect or {}).get("files") or [])
    if not files:
        return None
    from looplab.runtime.command_eval import _confined
    from looplab.runtime.metric_subject import bind_one
    return [bind_one(str(workdir), rel, since=since, confine=_confined) for rel in files]


# What `reuse_refusal` answers when a candidate may NOT be reused. Closed, and every member is a
# DIFFERENT fact about the candidate — a reader deciding whether the cache is worth having has to be
# able to tell "no sibling ran this" from "one did and its artifact has been overwritten since".
REUSE_REFUSALS = ("no_key", "no_candidate", "key_differs", "no_outputs", "output_missing",
                  "output_identity_changed", "output_digest_changed", "output_unbound")


def reuse_refusal(key: Optional[str], candidate: Optional[dict], workdir) -> Optional[str]:
    """None when `candidate`'s completed stage may be reused under `key`, else a `REUSE_REFUSALS`
    member. THE DECISION a cross-node cache would make, stated so it has a truth table.

    `candidate` is a recorded stage row: `{stage_input_key, stage_outputs}` as written by
    `_run_stages`. `workdir` is where those outputs are expected to still be.

    STRICTER THAN THE ARTIFACT CONTRACT, ON PURPOSE. `verify_stage_artifacts` proves an artifact
    exists, is non-empty and was written after the stage began — the right rule for a stage that
    actually RAN, and the rule that caught v8 node 5's "skip if the file exists". It cannot prove the
    file is the one a KEY names: any later write satisfies it. So this re-derives the full identity
    and requires BOTH the `file_identity` stat tuple (which catches a replacement: `os.replace` gives
    the name a new inode) AND the content digest (which catches a same-size, mtime-restored rewrite
    that the stat tuple's ctime field would catch on POSIX but not on Windows) to be byte-equal to
    what the row recorded. Freshness is not consulted at all here and that is deliberate: the reused
    artifact predates this attempt BY DEFINITION, which is precisely why
    `command_eval.attempt_freshness_floor` already passes no floor down a stage-scoped re-run.

    A candidate row whose own binding FAILED (`bound: False`, i.e. the stage completed but its
    declared artifact was stale/missing/unreadable at the instant the contract passed) is refused
    rather than treated as "no outputs to check": a stage nobody could name the output of is not a
    stage anything may inherit.

    THE ONE ARTIFACT SHAPE THIS IS WEAKER ON, stated rather than left to be discovered: a DIRECTORY.
    `bind_one` records a directory as `(entries, total_bytes)` plus its own stat tuple and NO digest,
    because a recursive walk of a thousand-shard checkpoint is not affordable on the eval path — so
    an in-place rewrite of one shard that preserves the file count and the byte total moves neither.
    That is conservative for FRESHNESS (the coarse reading can only ever report an untouched
    directory as stale) and genuinely weaker for IDENTITY, which is what this function needs. A
    caller that must be sure declares the specific FILE, which is what `expect.files` already
    accepts and what every declared artifact in this corpus is.
    """
    if not key:
        return "no_key"
    if not isinstance(candidate, dict):
        return "no_candidate"
    if candidate.get(STAGE_INPUT_KEY) != key:
        return "key_differs"
    rows = candidate.get(STAGE_OUTPUTS_KEY)
    if not isinstance(rows, list) or not rows:
        return "no_outputs"
    from looplab.runtime.command_eval import _confined
    from looplab.runtime.metric_subject import bind_one
    for row in rows:
        if not isinstance(row, dict) or not row.get("bound"):
            return "output_unbound"
        now = bind_one(str(workdir), str(row.get("path") or ""), since=None, confine=_confined)
        if not now.get("bound"):
            return "output_missing"
        if list(now.get("identity") or []) != list(row.get("identity") or []):
            return "output_identity_changed"
        if (now.get("digest"), now.get("digest_mode")) != (row.get("digest"), row.get("digest_mode")):
            return "output_digest_changed"
    return None
