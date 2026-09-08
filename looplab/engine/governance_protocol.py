"""The two halves of the operator-governance transaction that no subsystem owns (doc 25 EM-05/EM-08).

Governance rows decide canonical concept identity, which claims are allowed into live cross-run
projections, and what a paid steward is allowed to have concluded.  Two shapes recur across every
subsystem that touches them, and both were homed inside a subsystem that happened to need them
first:

* the WRITE half — one record appended to an append-only ledger under a required interprocess lock,
  with idempotency resolved BEFORE the revision CAS, validation inside the same critical section, and
  a durable fsync.  `append_governance` lived in `concept_registry.py`, so a primitive four
  subsystems import as generic was maintained beside the concept ledgers, and its private spelling
  had to be imported through a module those subsystems otherwise have nothing to do with.
* the READ half — a projection that takes `_governance=None`, and, when it is None, re-enters itself
  under a resolved snapshot.  Six projections copy-pasted that skeleton along with the derivation of
  WHICH stores the resolved snapshot must govern.

Neither half belongs to concepts, to claims or to the stewards; they belong to the ledger protocol,
which is what this module is.  Everything here is protocol only: it knows no ledger filename, no
schema and no subsystem — the caller passes its strict reader, its identity rule and its conflict
types, because those are the parts that are genuinely per-ledger (doc 25 EM-05's own finding was
that a filename branch inside the primitive is exactly this leak).
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Optional

from looplab.engine.governance_health import (
    confirm_governance_durable,
    raise_governance_storage_unavailable,
)

# ------------------------------------------------------------------------------------------------
# The READ half: one governed projection, re-entered under a resolved snapshot.


def governed_projection(memory_dir, reenter: Callable[[dict], Any], *,
                        include_concepts: bool = False, source_names=(),
                        unsupplied: Optional[Mapping[str, Any]] = None):
    """Resolve the governance snapshot ONCE, then call `reenter` with it (doc 25 EM-08).

    Six projections share this skeleton — `claims.atlas_for_memory`,
    `claims_retrieval.cross_run_retrieve`, `claim_steward.claim_curation_snapshot`,
    `concept_steward.concept_curation_snapshot`, and the Strategist/Researcher live context builders
    through `cross_run_context.enter_governed`.  Each takes a private `_governance=None` parameter
    and, when it is None, calls back into ITSELF with the resolved snapshot; the body below the guard
    then reads the stores under one linearizable era.

    The copied part that actually bites is not the recursion, it is `source_names`.  A projection
    that omits a store it then READS is governed by a ledger that never saw that store: no error, no
    exception, just quietly weaker guarantees — which is the same failure
    `cross_run_context.CROSS_RUN_SOURCE_NAMES` exists to prevent for the live builders.  `unsupplied`
    states that derivation once: it maps each store's FILENAME to the value the caller passed for it,
    and every entry the caller left as None — i.e. every store this projection is about to load
    itself — joins the governed set.  A store the caller supplied is already frozen by whoever loaded
    it, so locking it again would be a claim about bytes this call never reads.

    Ordering is irrelevant here and deliberately not preserved: `project_governed_sources` sorts the
    source locks by absolute path, because the lock ORDER is the deadlock-avoidance rule and it may
    not depend on the order a caller happened to list its stores in.

    `claim_locked` is deliberately NOT exposed. It says "this caller already owns the claim-decision
    lock", which is true only of `claims.record_claim_decision`'s persist chain — a WRITER wrapping
    its own append, not a projection re-entering itself — so offering it here would invite a reader
    to declare a lock it does not hold, and the projection would then run outside the fence it
    reports as governing it.
    """
    names = list(source_names or ())
    for name, supplied in (unsupplied or {}).items():
        if supplied is None and name not in names:
            names.append(name)
    # DEFERRED: `governance_health` reaches back into `concept_registry` for the concept-global
    # transaction, and `concept_registry` imports the write half of this module at module scope.
    from looplab.engine.governance_health import project_governed_sources

    return project_governed_sources(
        memory_dir, reenter, include_concepts=include_concepts, source_names=tuple(names))


# ------------------------------------------------------------------------------------------------
# The WRITE half: one durable, idempotent, CAS-guarded governance append.
#
# The three exception types keep their `Concept…` spellings even though the protocol is generic.
# That is not inertia: `tools/concept_tools.py::ConceptGovernanceTools.execute` classifies them by
# `type(exc).__name__` against a literal set, so the NAME is an agent-facing contract, and
# `serve/routers/cross_run.py` and `concept_tidy.py` catch them by identity.  `concept_registry`
# re-exports all three, so every existing importer keeps working.


class ConceptGovernanceConflict(ValueError):
    """Optimistic-concurrency failure for an alias/split ledger mutation."""

    def __init__(self, path: Path, expected: int, actual: int):
        self.path = path
        self.expected = expected
        self.actual = actual
        super().__init__(f"stale governance revision for {path.name}: expected {expected}, current {actual}")


class ConceptGovernanceGlobalConflict(ValueError):
    """Optimistic-concurrency failure across the combined alias/split policy."""

    def __init__(self, expected: int, actual: int):
        self.expected = expected
        self.actual = actual
        super().__init__(
            "stale concept governance revision: "
            f"expected {expected}, current {actual}"
        )


class ConceptGovernanceIdempotencyConflict(ValueError):
    """An action id was already committed with a different semantic payload in this ledger."""

    def __init__(self, path: Path, action_id: str):
        self.path = path
        self.action_id = action_id
        super().__init__(f"action_id {action_id!r} already exists with a different payload in {path.name}")


def validate_expected_revision(value: Optional[int], field: str = "expected_revision") -> None:
    """The CAS input rule, in one place.

    `True` is an `int` subclass, so a JSON `true` reaching a revision comparison would compare equal
    to 1 and silently accept a stale write against revision one; a negative revision is not a
    position in an append-only ledger at all.  Both are operator input errors, refused rather than
    coerced.
    """
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError(f"{field} must be a non-negative integer")


def idempotency_payload(rec: dict) -> str:
    """Canonical semantic payload; actor/timestamp/revision are receipt metadata, not mutation identity."""
    semantic = {k: rec.get(k) for k in ("v", "action", "from", "to", "rules", "default") if k in rec}
    return json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def governance_lock(path: Path):
    """The one spelling of a governance ledger's critical section.

    Lenient JSONL replay can tolerate a torn tail, but it cannot recover an interleaved or lost
    policy decision, so this lock is `required=True` everywhere: governance fails CLOSED when the
    locking guarantee is unavailable rather than proceeding unserialized.
    """
    from looplab.events.eventstore import interprocess_lock

    return interprocess_lock(Path(str(path) + ".lock"), required=True)


def _row_action_id(row: dict) -> str:
    return str(row.get("action_id") or "")


def action_replay(rows, rec: dict, action_id: str, *,
                  payload: Callable[[dict], Any] = idempotency_payload,
                  action_id_of: Callable[[dict], str] = _row_action_id) -> tuple[Optional[dict], bool]:
    """Resolve an `action_id` against already-committed rows: `(existing_row, is_exact_replay)`.

    Called BEFORE the revision CAS by every governance writer, and the order is the load-bearing
    part: a transport retry carrying the original — now stale — revision must return its first
    durable receipt rather than appending again or failing with a conflict it cannot act on.  The
    first matching row wins, because an append-only ledger's first commit of an action id IS the
    action.

    `payload` is the ledger's own identity rule (which fields make two rows the SAME action) and
    `action_id_of` is how that ledger READS an id off a persisted row, so this function never has to
    know a schema.  Both matter: the claim ledger sanitizes and bounds a stored id through
    `_identity_text` before comparing it, because a persisted row is untrusted text, and comparing
    the raw field instead would let a control-character-padded id miss its own replay and append a
    second decision.  The caller raises its own conflict type on `(row, False)`: what a mismatched
    replay means to an operator differs per ledger, and a shared exception would flatten "you reused
    an action id" into one message for all of them.
    """
    for existing in rows:
        if action_id_of(existing) != action_id:
            continue
        return existing, payload(existing) == payload(rec)
    return None, False


def durable_governance_append(path: Path, line: str, *, created: bool,
                              require_durable: bool = True) -> None:
    """Append one already-serialized line and publish it, or refuse the write.

    `created` (the ledger did not exist before this call) additionally fsyncs the PARENT directory:
    without it the file's bytes are durable while the directory entry naming them is not, so a crash
    can lose an entire ledger that reported success.  Storage faults become
    `GovernanceLedgerUnavailable` rather than a raw `OSError`, because every caller of this protocol
    crosses an API/CLI/tool boundary where a filesystem path or errno text must not be reflected.
    """
    from looplab.core.atomicio import best_effort_fsync, strict_fsync, strict_fsync_parent

    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            (strict_fsync if require_durable else best_effort_fsync)(f.fileno())
        if require_durable and created:
            strict_fsync_parent(path)
    except (OSError, TimeoutError, RuntimeError) as exc:
        if require_durable:
            raise_governance_storage_unavailable(path, exc)
        raise


def append_governance(path: Path, rec: dict, *, validate: Optional[Callable[[], None]] = None,
                      guard: Optional[Callable[[], None]] = None,
                      read_rows: Optional[Callable[[Path], list[dict]]] = None,
                      expected_revision: Optional[int] = None,
                      global_revision: Optional[Callable[[], int]] = None,
                      expected_global_revision: Optional[int] = None,
                      require_durable: bool = False) -> dict:
    """Append one governance record under a required cross-platform interprocess lock.

    Lenient JSONL replay can tolerate a torn tail, but it cannot recover an interleaved or lost policy
    decision.  Governance therefore fails closed when the locking guarantee is unavailable.  `validate`,
    when supplied, runs in the same critical section as the append.

    `global_revision`, when supplied, is a CALLABLE returning the revision of a policy that spans more
    than this one physical ledger; the caller closes over whatever that means for it.  It used to be a
    memory directory this module resolved through `concept_governance_global_revision`, which is the
    same leak as the ledger-filename branch doc 25 EM-05 removed: a primitive four subsystems import
    as generic knew one subsystem's cross-ledger policy by name.
    """
    if validate is not None and guard is not None:
        raise ValueError("pass either validate or guard, not both")
    # Retain the shipped `guard` spelling while executing both forms inside the required lock.
    validator = validate or guard

    with governance_lock(path):
        strict_rows = read_rows(path) if read_rows is not None else None
        if global_revision is not None:
            # The caller holds the memory-wide lock, so this health preflight is stable through
            # idempotency lookup and append. A retry may return its old receipt only when the full
            # alias+split policy remains projectable; an unhealthy sibling ledger is not an exact state.
            global_revision()
        # Idempotency keys are namespaced by the physical governance ledger. Alias and split endpoints may
        # use the same caller-generated key; changing that boundary requires a durable cross-ledger action
        # index rather than an unlocked scan of the sibling file.
        action_id = str(rec.get("action_id") or "")
        if action_id and path.exists():
            if strict_rows is not None:
                existing_rows = strict_rows
            else:
                # `read_rows` is now the ONLY way a caller selects a strict reader (doc 25 EM-05).
                # This used to branch on `concept_aliases.jsonl`/`concept_splits.jsonl` by name, so a
                # primitive that four subsystems import as generic secretly knew the concept ledgers.
                # Those two call sites pass their reader explicitly; what is left here is the lenient
                # projection that non-policy RECEIPT logs have always used.
                from looplab.events.eventstore import read_jsonl_lenient
                existing_rows = read_jsonl_lenient(path, loads=json.loads, dicts_only=True)
            # Resolve idempotency before CAS/validation. A transport retry carrying the original stale
            # revision must return its first durable receipt, never append again or fail with a conflict.
            existing, exact = action_replay(existing_rows, rec, action_id)
            if existing is not None:
                if not exact:
                    raise ConceptGovernanceIdempotencyConflict(path, action_id)
                if require_durable:
                    # The first response may have failed during fsync after bytes reached the page
                    # cache. A retry must re-confirm both contents and publication before returning 200.
                    confirm_governance_durable(path)
                return dict(existing)
        resolved_global = None
        if global_revision is not None:
            # Public concept writes hold the memory-wide governance lock before entering this per-ledger
            # lock. Resolve action-id replay first so a lost-response retry can return its original receipt
            # even though both revision tokens are now stale.
            resolved_global = global_revision()
            validate_expected_revision(
                expected_global_revision, "expected_governance_revision"
            )
            if (expected_global_revision is not None
                    and expected_global_revision != resolved_global):
                raise ConceptGovernanceGlobalConflict(
                    expected_global_revision, resolved_global
                )
        if strict_rows is None:
            current = _lenient_ledger_revision(path)
        else:
            explicit = [row.get("revision") for row in strict_rows
                        if isinstance(row.get("revision"), int)
                        and not isinstance(row.get("revision"), bool)]
            current = max([len(strict_rows), *explicit], default=0)
        created = not path.exists()
        validate_expected_revision(expected_revision)
        if expected_revision is not None and expected_revision != current:
            raise ConceptGovernanceConflict(path, expected_revision, current)
        if validator is not None:
            validator()
        if resolved_global is not None:
            rec["governance_revision"] = resolved_global + 1
        # Allocate the CAS revision inside the same required lock as validation and append.
        rec["revision"] = current + 1
        separator = ""
        # `read_rows is None` now carries the whole distinction (doc 25 EM-05). It always did for
        # these two ledgers — they reach this line with a reader, so the filename clause that used to
        # sit here could only ever agree with it. A strict ledger is one whose caller supplied a
        # strict reader, which is the same statement the name check was making indirectly.
        if read_rows is None and path.exists() and path.stat().st_size:
            with open(path, "rb") as existing_bytes:
                existing_bytes.seek(-1, 2)
                if existing_bytes.read(1) not in (b"\n", b"\r"):
                    # Non-policy receipt logs retain their historical torn-tail separation. Strict
                    # operator ledgers were rejected by _ledger_revision before reaching this branch.
                    separator = "\n"
        durable_governance_append(path, separator + json.dumps(rec) + "\n",
                                  created=created, require_durable=require_durable)
    return rec


def _lenient_ledger_revision(path: Path) -> int:
    """The revision of a ledger read WITHOUT a strict reader: its lenient row count.

    `read_rows is None` means the caller declared this ledger a non-policy RECEIPT log, exactly as it
    does for the torn-tail separator above, so the same lenient projection applies to its revision.

    What this does NOT do is second-guess that declaration by name. `concept_registry._ledger_revision`
    still dispatches on `concept_aliases.jsonl`/`concept_splits.jsonl` and so used to fail a policy
    ledger closed even when a call site forgot its reader — a backstop doc 25 EM-05 recorded as the
    reason the structural guarantee survived the filename branches being deleted. Moving the
    primitive out of the concept module ends that accident: the backstop is now the call-site
    convention alone, pinned by `tests/test_concept_registry.py::
    test_every_policy_ledger_append_passes_its_strict_reader`, which fails when any concept append
    drops `read_rows`. Re-importing a subsystem's filename dispatch here would put the leak this
    module exists to close back into it.
    """
    from looplab.events.eventstore import read_jsonl_lenient

    if not path.exists():
        return 0
    rows = read_jsonl_lenient(path, loads=json.loads, dicts_only=True)
    explicit = [row.get("revision") for row in rows
                if isinstance(row.get("revision"), int) and not isinstance(row.get("revision"), bool)]
    return max([len(rows), *explicit], default=0)
