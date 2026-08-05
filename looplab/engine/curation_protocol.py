"""The FINALIZE at-most-once paid-curation transaction (doc 25 EM-03).

This is governance infrastructure, not lessons. It used to live inside `lessons.py`/`LessonMemory`,
where it was ~645 of that file's 1,301 lines and had nothing to do with the E4/M2/M6 cross-run
lesson machinery around it. Nothing about the protocol changed in the move: `LessonMemory` still
mixes it in, so `LessonMemory._write_curation_claim` / `store_concept_curation` / … resolve exactly
as before and every existing monkeypatch seam through the class keeps working.

**What it protects.** Each of the three finalize stewards (concept, claim, task-facets) makes ONE
paid provider call whose result is durably logged for operator ratification. The provider call
cannot participate in the JSONL transaction, so a crash between "we called the model" and "we wrote
the receipt" must not be replayable — replaying it buys the same answer twice.

**Why there are two of these and not one.** `steward_invocation.py` implements the OTHER
at-most-once paid-steward protocol, for on-demand HTTP/CLI invocations, over the SAME three ledgers.
Converging the two writers was examined and declined (doc 25 EM-03); the crash windows differ in
ways that are load-bearing rather than incidental:

| | finalize (here) | on-demand (`steward_invocation.py`) |
|---|---|---|
| identity | SEMANTIC: the content digest of the exact model-visible snapshot (`concept:v2:<digest>`), or the task id for facets | the caller's `action_id`, bound to a `request_digest` |
| who chooses it | the code, from the input | the operator, per request |
| claim location | a side file, `.curation_invocations/<sha>.json` | a `begun` ROW in the ledger itself |
| lock granularity | one lock per semantic key | one lock per LEDGER |
| what the lock spans | fast paths + paid call + terminal | cache check + client build + paid call + terminal |
| a crash between claim and terminal | the NEXT attempt writes a `prior_attempt_incomplete_not_replayed` terminal from the CLAIM's own identity and closes the key forever | the next attempt with the same id REPLAYS the begun row; the claim stays open and the operator must review it and choose a NEW action id |
| re-running deliberately | impossible while the input is unchanged — that is the point | possible, with a new action id — that is the point |

So the two protocols answer opposite questions. Finalize runs unattended on every finalization and
must never buy the same snapshot twice; the on-demand path runs because a human asked for it and
must let that human ask again after reviewing an ambiguous attempt. A single writer would have to
drop one of those, and the v2 row schema physically cannot carry the on-demand path's fields
anyway (`_validate_v2_curation_row` rejects any row carrying `action_id`, `by`/`at`, or a
non-null `receipt`). What IS shared, and now shared by construction, is the kind -> ledger-file
vocabulary (`governance_health.curation_ledger_file`).

Layering: same as `lessons.py` — never imports the orchestrator or `serve`; the engine handle
arrives as the mixed-in `LessonMemory`'s own `self._e`."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path

from looplab.core.jsonutil import valid_digest_ref
from looplab.core.models import RunState


_CURATION_CLAIM_DIR = ".curation_invocations"
_CURATION_CLAIM_MAX_BYTES = 16 * 1024
_FINALIZE_STEWARD_PARSER = "tool_call_once"
# Soft cap on `.curation_invocations/`. `_interprocess_lock` opens (creates) a `<name>.lock` per paid
# decision and never unlinks it, and the concept/claim curation keys carry the EVOLVING portfolio digest,
# so the scratch dir would otherwise accrete a lock file per finalize forever. Past this cap we best-effort
# prune the oldest ORPHAN lock files (no matching `.json` recovery claim). Claim `.json` markers are durable
# crash-recovery state and are never pruned here.
_CURATION_SCRATCH_MAX_ENTRIES = 512
# Never prune a lock younger than a finalize's worst-case wall-clock, so a GC pass can never unlink a lock
# an in-flight decision on another process still holds (the paid LLM call runs inside the lock).
_CURATION_SCRATCH_MIN_AGE_S = 6 * 3600
_CURATION_THREAD_LOCKS: dict[str, tuple[threading.Lock, int]] = {}
_CURATION_THREAD_LOCKS_GUARD = threading.Lock()


@contextmanager
def _curation_thread_lock(key: str):
    """Serialize one semantic curation claim locally without retaining an unbounded lock registry."""
    with _CURATION_THREAD_LOCKS_GUARD:
        lock, users = _CURATION_THREAD_LOCKS.get(key, (threading.Lock(), 0))
        _CURATION_THREAD_LOCKS[key] = (lock, users + 1)
    try:
        with lock:
            yield
    finally:
        with _CURATION_THREAD_LOCKS_GUARD:
            current = _CURATION_THREAD_LOCKS.get(key)
            if current is not None and current[0] is lock:
                if current[1] <= 1:
                    _CURATION_THREAD_LOCKS.pop(key, None)
                else:
                    _CURATION_THREAD_LOCKS[key] = (lock, current[1] - 1)


@dataclass(frozen=True)
class _StewardPlan:
    """What one finalize steward needs after its snapshot is read, before the decision lock.

    `propose(client)` returns `(outcome, proposals)` — the outcome name is the steward's own, since
    "empty" means different things to a curation (`curation_is_empty`) and to a facet set (falsy).
    `fast_paths` are evaluated INSIDE the lock in order; each returns `(outcome, proposals)` to
    settle, or None to continue.
    """

    input_schema: str
    input_digest: str
    curation_key: str
    has_input: bool
    propose: Callable[[object], tuple[str, dict]]
    fast_paths: tuple = ()


class CurationProtocolMixin:
    """The finalize paid-curation transaction. `self` here IS the `LessonMemory` (see the module
    docstring for why this is a MIXIN rather than a free-function module: every method below is a
    live patch seam reached as `LessonMemory.<name>`, and the protocol reads the engine handle
    `self._e` for `memory_dir`, the two `_cross_run_curation*` gates and `reflect_client`)."""

    def _already_curated(self, log_name: str, curation_key: str) -> bool:
        """Whether semantic work has a terminal outcome; unavailable clients do not consume the key."""
        from looplab.engine.governance_health import read_curation_rows

        p = Path(self._e.memory_dir) / log_name
        if not p.exists():
            return False
        return any(
            r.get("v") == 2 and not isinstance(r.get("v"), bool)
            and r.get("action") is None
            and str(r.get("curation_key") or "") == curation_key
            and str(r.get("outcome") or "") != "unavailable"
            for r in read_curation_rows(p)
        )

    @staticmethod
    def _curation_finish_seq(final: RunState) -> int | None:
        finish_seq = getattr(final, "last_finish_seq", None)
        return (finish_seq if isinstance(finish_seq, int) and not isinstance(finish_seq, bool)
                and finish_seq >= 0 else None)

    @classmethod
    def _curation_source_key(cls, final: RunState) -> str:
        """Derived by `governance_health`, which is also what VALIDATES it on every ledger read.

        The two used to compute it independently. They cannot be allowed to disagree: the validator
        recomputes this key and rejects any row that does not match, so a drift would retroactively
        invalidate every receipt already on disk (doc 25 EM-04).
        """
        from looplab.engine.governance_health import curation_source_key

        return curation_source_key(
            run_id=str(final.run_id or ""), task_id=str(final.task_id or ""),
            finish_seq=cls._curation_finish_seq(final))

    @staticmethod
    def _portfolio_curation_key(kind: str, input_digest: str) -> str:
        if kind not in {"concept", "claim"} or len(input_digest) != 64:
            raise ValueError("invalid portfolio curation identity")
        # paid portfolio work is identified by the exact frozen model input, never by
        # whichever run happened to trigger finalize.  This is both cross-run dedup and the TOCTOU fence.
        return f"{kind}:v2:{input_digest}"

    @staticmethod
    def _facets_curation_key(task_id: str) -> str:
        """Derived by `governance_health` — same reason as `_curation_source_key`."""
        from looplab.engine.governance_health import facets_curation_key

        return facets_curation_key(task_id)

    @classmethod
    def _diagnostic_curation_key(cls, kind: str, final: RunState) -> str:
        return f"{kind}:diagnostic:v2:{cls._curation_source_key(final).rsplit(':', 1)[-1]}"

    def _curation_provenance(self, *, input_digest: str, input_schema: str,
                             client) -> dict:
        from looplab.core.redact import redact_persisted_text

        model = getattr(client, "model", None) if client is not None else None
        if not model:
            model = getattr(getattr(self._e, "settings", None), "llm_model", None)
        model = redact_persisted_text(
            model or "unknown", max_chars=200, entropy=True, single_line=True)
        return {
            "input_digest": input_digest,
            "input_schema": input_schema,
            "model": model or "unknown",
            "parser": _FINALIZE_STEWARD_PARSER,
        }

    def _curation_claim_path(self, log_name: str, curation_key: str) -> Path:
        digest = hashlib.sha256(f"{log_name}\0{curation_key}".encode("utf-8")).hexdigest()
        return Path(self._e.memory_dir) / _CURATION_CLAIM_DIR / f"{digest}.json"

    def _legacy_curation_claim_path(self, log_name: str, final: RunState) -> Path | None:
        """The v1 run-keyed claim path, checked only for an exact non-empty run id."""
        rid = str(final.run_id or "")
        if not rid:
            return None
        digest = hashlib.sha256(f"{log_name}\0{rid}".encode("utf-8")).hexdigest()
        return Path(self._e.memory_dir) / _CURATION_CLAIM_DIR / f"{digest}.json"

    def _legacy_curation_terminal(self, log_name: str, final: RunState) -> bool:
        """Bridge known v1 outcomes without reviving the old polymorphic run/task identity."""
        from looplab.engine.governance_health import read_curation_rows

        rid, tid = str(final.run_id or ""), str(final.task_id or "")
        path = Path(self._e.memory_dir) / log_name
        if not rid or not path.exists():
            return False
        return any(
            not row.get("curation_key")
            and str(row.get("run_id") or "") == rid
            and str(row.get("task_id") or "") == tid
            and str(row.get("outcome") or "") != "unavailable"
            for row in read_curation_rows(path)
        )

    def _write_curation_claim(self, path: Path, log_name: str, kind: str,
                              final: RunState, curation_key: str,
                              provenance: dict, incomplete: dict) -> None:
        """Create and strictly sync the one-way claim that gates a paid finalize invocation."""
        from looplab.core.atomicio import strict_fsync, strict_fsync_parent
        from looplab.engine.governance_health import CURATION_ID_MAX_CHARS

        auto_requested = incomplete.get("auto_requested")
        if not isinstance(auto_requested, bool):
            raise ValueError("paid curation claim requires boolean auto_requested")
        run_id, task_id = str(final.run_id or ""), str(final.task_id or "")
        if (len(run_id) > CURATION_ID_MAX_CHARS or any(ord(ch) < 32 for ch in run_id)
                or len(task_id) > CURATION_ID_MAX_CHARS
                or any(ord(ch) < 32 for ch in task_id)):
            # The terminal ledger uses the same bounds. Validate before the irreversible provider
            # boundary so a claim can never be durable while every matching terminal is unwritable.
            raise ValueError("invalid paid curation source identity")
        claim_dir = path.parent
        created_dir = not claim_dir.exists()
        claim_dir.mkdir(parents=True, exist_ok=True)
        if created_dir:
            strict_fsync_parent(claim_dir)
        payload = {
            "v": 2,
            "action": "finalize-steward-begun",
            "kind": kind,
            "log": log_name,
            "curation_key": curation_key,
            "source_key": self._curation_source_key(final),
            "run_id": run_id,
            "task_id": task_id,
            "finish_seq": self._curation_finish_seq(final),
            "auto": False,
            "auto_requested": auto_requested,
            **provenance,
        }
        encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")) + "\n").encode("utf-8")
        # Exclusive create is a second line of defence behind the semantic invocation lock. Any
        # extant file, including a torn claim from a failed sync, is conservatively non-replayable.
        with path.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            strict_fsync(handle.fileno())
        strict_fsync_parent(path)

    def _read_curation_claim(self, path: Path, log_name: str, kind: str,
                             curation_key: str) -> tuple[RunState, dict, bool]:
        """Read an existing v2 paid claim without borrowing identity from the retrying run."""
        from looplab.engine.governance_health import CURATION_ID_MAX_CHARS

        def _unique_object(pairs):
            obj = {}
            for key, value in pairs:
                if key in obj:
                    raise ValueError("duplicate curation claim field")
                obj[key] = value
            return obj

        def _reject_constant(_value):
            raise ValueError("non-finite curation claim value")

        with path.open("rb") as handle:
            raw = handle.read(_CURATION_CLAIM_MAX_BYTES + 1)
        if not raw or len(raw) > _CURATION_CLAIM_MAX_BYTES:
            raise ValueError("invalid curation claim size")
        if raw.count(b"\n") != 1 or not raw.endswith(b"\n"):
            raise ValueError("curation claim must be one complete record")
        try:
            claim = json.loads(
                raw.decode("utf-8"), object_pairs_hook=_unique_object,
                parse_constant=_reject_constant)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid curation claim encoding") from exc
        expected_fields = {
            "v", "action", "kind", "log", "curation_key", "source_key", "run_id",
            "task_id", "finish_seq", "auto", "auto_requested", "input_digest",
            "input_schema", "model", "parser",
        }
        if not isinstance(claim, dict) or set(claim) != expected_fields:
            raise ValueError("invalid curation claim fields")
        if claim.get("v") != 2 or isinstance(claim.get("v"), bool):
            raise ValueError("unsupported curation claim version")
        if claim.get("action") != "finalize-steward-begun":
            raise ValueError("invalid curation claim action")
        if claim.get("kind") != kind or claim.get("log") != log_name:
            raise ValueError("foreign curation claim scope")
        if claim.get("curation_key") != curation_key:
            raise ValueError("foreign curation claim identity")
        if claim.get("auto") is not False or not isinstance(claim.get("auto_requested"), bool):
            raise ValueError("invalid curation claim invocation mode")

        bounded_strings = {
            "run_id": CURATION_ID_MAX_CHARS,
            "task_id": CURATION_ID_MAX_CHARS,
            "source_key": 80,
            "curation_key": 100,
            "input_digest": 64,
            "input_schema": 200,
            "model": 200,
            "parser": 100,
        }
        for field, maximum in bounded_strings.items():
            value = claim.get(field)
            if (not isinstance(value, str) or not value or len(value) > maximum
                    or any(ord(ch) < 32 for ch in value)):
                # Run ids may be empty in historical state, but a durable claim still binds the exact
                # empty value. Handle those two identity fields separately below.
                if field not in {"run_id", "task_id"} or value != "":
                    raise ValueError(f"invalid curation claim {field}")
        finish_seq = claim.get("finish_seq")
        if (finish_seq is not None
                and (isinstance(finish_seq, bool) or not isinstance(finish_seq, int)
                     or finish_seq < 0)):
            raise ValueError("invalid curation claim finish_seq")
        # The shared predicate also CLOSES a gap here (doc 25 EV-04): this copy tested `len` and
        # membership without an `isinstance`, so a 64-element list of hex characters satisfied both
        # and was accepted as a digest. The binding stays — `_portfolio_curation_key` below rebuilds
        # this claim's identity from it.
        digest = claim["input_digest"]
        if not valid_digest_ref(digest):
            raise ValueError("invalid curation claim input_digest")
        from looplab.core.redact import redact_persisted_text
        for field, maximum in (("input_schema", 200), ("model", 200), ("parser", 100)):
            value = claim[field]
            if redact_persisted_text(
                    value, max_chars=maximum, entropy=True, single_line=True) != value:
                raise ValueError(f"unsafe curation claim {field}")
        if kind in {"concept", "claim"}:
            if self._portfolio_curation_key(kind, digest) != curation_key:
                raise ValueError("curation claim digest does not match its identity")
        elif kind == "facets":
            if self._facets_curation_key(claim["task_id"]) != curation_key:
                raise ValueError("facets claim task does not match its identity")
        else:
            raise ValueError("invalid curation claim kind")

        claim_final = RunState(
            run_id=claim["run_id"], task_id=claim["task_id"],
            last_finish_seq=finish_seq if finish_seq is not None else -1)
        if self._curation_source_key(claim_final) != claim["source_key"]:
            raise ValueError("curation claim source identity mismatch")
        provenance = {
            field: claim[field] for field in ("input_digest", "input_schema", "model", "parser")
        }
        return claim_final, provenance, claim["auto_requested"]

    @contextmanager
    def _curation_decision_lock(self, log_name: str, final: RunState, curation_key: str):
        """Serialize every terminal decision for one semantic key, including no-call fast paths."""
        from looplab.core.atomicio import strict_fsync_parent
        from looplab.events.eventstore import _interprocess_lock

        claim_path = self._curation_claim_path(log_name, curation_key)
        legacy_path = self._legacy_curation_claim_path(log_name, final)
        created_dir = not claim_path.parent.exists()
        claim_path.parent.mkdir(parents=True, exist_ok=True)
        if created_dir:
            strict_fsync_parent(claim_path.parent)
        self._prune_curation_scratch(claim_path.parent)
        key = str(claim_path.absolute())
        with _curation_thread_lock(key):
            # The legacy (v1, run-keyed) claim is NEVER written by this v2 path — it is only READ
            # (`_curation_attempt_already_resolved_locked`). Its interprocess lock therefore only matters
            # when a legacy claim actually exists on disk (a v1-era writer left one). Acquiring it
            # unconditionally would open (create) a `<run_id>.json.lock` — and since the legacy path is
            # keyed by the unique run_id and `_interprocess_lock` never unlinks, that accreted one orphan
            # lock per run in `.curation_invocations/` forever. Serialize against it only when there is a
            # legacy claim to serialize against; the v2 claim lock below always fences the paid decision.
            legacy_guard = (
                _interprocess_lock(Path(str(legacy_path) + ".lock"), required=True)
                if legacy_path is not None and legacy_path.exists() else nullcontext())
            with legacy_guard:
                with _interprocess_lock(Path(str(claim_path) + ".lock"), required=True):
                    yield

    def _prune_curation_scratch(self, scratch: Path) -> None:
        """Best-effort bound on `.curation_invocations/`. Once the dir grows past the soft cap, unlink the
        OLDEST orphan `<digest>.json.lock` files — locks with no matching `<digest>.json` recovery claim,
        i.e. pure interprocess-mutex scratch left behind by empty/unavailable/evolving-digest decisions.
        Skips any lock younger than a finalize's worst-case wall-clock so an in-flight paid decision's lock
        is never pulled out from under it, and never touches the durable `.json` claim markers. Never
        raises — a hiccup in scratch GC must not perturb finalize."""
        try:
            entries = list(scratch.iterdir())
        except OSError:
            return
        if len(entries) <= _CURATION_SCRATCH_MAX_ENTRIES:
            return
        claims = {p.name for p in entries if p.name.endswith(".json")}
        now = time.time()
        prunable: list[tuple[float, Path]] = []
        for p in entries:
            if not p.name.endswith(".json.lock"):
                continue
            if p.name[:-len(".lock")] in claims:
                continue  # keep a lock paired with a live recovery claim
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if now - mtime < _CURATION_SCRATCH_MIN_AGE_S:
                continue  # a lock this fresh may be held by an in-flight decision on another process
            prunable.append((mtime, p))
        prunable.sort()  # oldest first
        for _mtime, p in prunable[: len(entries) - _CURATION_SCRATCH_MAX_ENTRIES]:
            try:
                p.unlink()
            except OSError:
                pass

    @contextmanager
    def _paid_curation_attempt_locked(self, log_name: str, kind: str, final: RunState,
                                      curation_key: str, provenance: dict, incomplete: dict):
        """Paid-attempt protocol; the caller must hold ``_curation_decision_lock``."""
        claim_path = self._curation_claim_path(log_name, curation_key)
        if self._curation_attempt_already_resolved_locked(
                log_name, kind, final, curation_key, incomplete):
            yield False
            return
        self._write_curation_claim(
            claim_path, log_name, kind, final, curation_key, provenance, incomplete)
        yield True

    def _recover_curation_claim_locked(self, log_name: str, kind: str, curation_key: str,
                                       incomplete: dict) -> bool:
        """Close one existing ambiguous paid claim; the semantic decision lock must be held."""
        claim_path = self._curation_claim_path(log_name, curation_key)
        if not claim_path.exists():
            return False
        # recovery metadata comes exclusively from the durable paid claim. A retrying
        # run/model may observe the same semantic key, but it never impersonates the lost attempt.
        claim_final, claim_provenance, claim_auto_requested = self._read_curation_claim(
            claim_path, log_name, kind, curation_key)
        recovered_incomplete = {
            **incomplete,
            "auto": False,
            "auto_requested": claim_auto_requested,
        }
        self._append_curation_once(
            log_name, claim_final, curation_key, claim_provenance, recovered_incomplete,
            require_durable=True)
        return True

    def _curation_attempt_already_resolved_locked(
            self, log_name: str, kind: str, final: RunState,
            curation_key: str, incomplete: dict) -> bool:
        """Resolve/suppress old work before any new v2 terminal; decision lock must be held."""
        if self._already_curated(log_name, curation_key):
            return True
        if self._legacy_curation_terminal(log_name, final):
            return True
        legacy_path = self._legacy_curation_claim_path(log_name, final)
        if legacy_path is not None and legacy_path.exists():
            # A v1 provider may have accepted the call, but its receipt did not bind an exact
            # model-visible snapshot. Suppress only this exact run and never invent a v2 terminal.
            return True
        return self._recover_curation_claim_locked(log_name, kind, curation_key, incomplete)

    @contextmanager
    def _paid_curation_attempt(self, log_name: str, kind: str, final: RunState,
                               curation_key: str, provenance: dict, incomplete: dict):
        """Yield once only after a durable claim; resolve a prior ambiguous claim without replay."""
        with self._curation_decision_lock(log_name, final, curation_key):
            with self._paid_curation_attempt_locked(
                    log_name, kind, final, curation_key, provenance, incomplete) as invoke:
                yield invoke

    def _append_curation_once(self, log_name: str, final: RunState, curation_key: str,
                              provenance: dict, rec: dict, *,
                              require_durable: bool = False) -> bool:
        """Append one semantic steward outcome; unavailable audits remain non-blocking."""
        from looplab.engine.concept_registry import _append_governance
        from looplab.engine.governance_health import read_curation_rows

        class _AlreadyLogged(RuntimeError):
            pass

        path = Path(self._e.memory_dir) / log_name
        path.parent.mkdir(parents=True, exist_ok=True)
        source_key = self._curation_source_key(final)
        outcome = str(rec.get("outcome") or "")
        locked_rows: list[dict] = []

        def _read_locked(current: Path) -> list[dict]:
            # paid history is policy. Capture the complete validated ledger under the
            # physical append lock so dedup and the next revision are derived from the same snapshot.
            rows = read_curation_rows(current)
            locked_rows[:] = rows
            return rows

        def _validate_locked() -> None:
            for row in locked_rows:
                if str(row.get("curation_key") or "") != curation_key:
                    continue
                prior_outcome = str(row.get("outcome") or "")
                if outcome == "unavailable":
                    # a late no-client observer is an audit only. Once another process
                    # commits a terminal result it may never append after or supersede that result.
                    if prior_outcome != "unavailable":
                        raise _AlreadyLogged
                    if prior_outcome == "unavailable" and row.get("source_key") == source_key:
                        raise _AlreadyLogged
                elif prior_outcome != "unavailable":
                    raise _AlreadyLogged

        payload = {
            "v": 2,
            "curation_key": curation_key,
            "source_key": source_key,
            "run_id": str(final.run_id or ""),
            "task_id": str(final.task_id or ""),
            "finish_seq": self._curation_finish_seq(final),
            **provenance,
            **rec,
        }
        try:
            _append_governance(
                path, payload, validate=_validate_locked, read_rows=_read_locked,
                require_durable=require_durable)
            return True
        except _AlreadyLogged:
            return False

    # --- one finalize-steward driver, three configurations (doc 25 EM-02) -----------------------
    #
    # The concept, claim and task-facet stewards share a ~90-line at-most-once protocol: the
    # cross-run gate, the semantic decision lock, the already-resolved check, the fast paths that
    # must not race a paid attempt, the paid attempt itself, and TWO error terminals (one inside the
    # lock, one diagnostic outside it). It was copy-pasted three times, so every protocol fix — a
    # lock-ordering change, a new terminal, a receipt field — had to be applied three times IN STEP
    # or the three ledgers would disagree about what happened during the same finalize.
    #
    # What actually differs between them is data: which log, which snapshot, which propose call, and
    # the empty shape of that steward's proposals. `fast_paths` carries the one STRUCTURAL
    # difference — facets are once-per-TASK, so an already-governed task must short-circuit inside
    # the lock, before any provider call.

    def _run_finalize_steward(self, final: RunState, *, kind: str,
                              unavailable_schema: str, empty_proposals, plan,
                              diagnostic_proposals=None) -> str:
        """Drive one finalize steward through the shared at-most-once protocol.

        `empty_proposals()` returns a FRESH empty proposals dict (never a shared mutable default).
        `plan(final)` runs inside the outer try and returns either a short-circuit outcome string or
        a `_StewardPlan`. `diagnostic_proposals()` defaults to `empty_proposals` and exists for the
        facets steward, whose diagnostic row carries the task id even when planning failed.

        The ledger is DERIVED from `kind` rather than passed beside it: the on-demand protocol
        resolves the same three files through the same table, and a caller free to pair a kind with
        another kind's log would write a receipt the at-most-once gate never looks at (doc 25 EM-03).
        """
        from looplab.engine.governance_health import curation_ledger_file

        if not (self._e.memory_dir and getattr(self._e, "_cross_run_curation", False)):
            return "disabled"
        log_name = curation_ledger_file(kind)
        auto_requested = bool(getattr(self._e, "_cross_run_curation_auto", False))
        diagnostic_key = self._diagnostic_curation_key(kind, final)
        diagnostic_provenance = self._curation_provenance(
            input_digest="", input_schema=unavailable_schema, client=None)

        def row(outcome, proposals, **extra):
            return {"outcome": outcome, "auto": False, "auto_requested": auto_requested,
                    "proposals": proposals, "receipt": None, **extra}

        try:
            planned = plan(final)
            if isinstance(planned, str):          # a pre-lock short-circuit (facets with no task id)
                return planned
            curation_key = planned.curation_key
            incomplete = row("prior_attempt_incomplete_not_replayed", empty_proposals(),
                             ambiguity="provider_outcome_unknown")

            def settle(outcome, proposals, provenance, *, durable=False):
                appended = self._append_curation_once(
                    log_name, final, curation_key, provenance, row(outcome, proposals),
                    **({"require_durable": True} if durable else {}))
                return outcome if appended else "already-resolved"

            # The semantic decision lock covers every fast path AND the paid attempt. Otherwise a
            # stale empty/unavailable observer can commit while another process is paying, then
            # suppress that provider's terminal result at append time.
            with self._curation_decision_lock(log_name, final, curation_key):
                if self._curation_attempt_already_resolved_locked(
                        log_name, kind, final, curation_key, incomplete):
                    return "already-resolved"
                unpaid = self._curation_provenance(
                    input_digest=planned.input_digest, input_schema=planned.input_schema,
                    client=None)
                for fast_path in planned.fast_paths:
                    decided = fast_path()
                    if decided is not None:
                        return settle(decided[0], decided[1], unpaid)
                if not planned.has_input:
                    return settle("empty", empty_proposals(), unpaid)
                client = self.reflect_client()
                provenance = self._curation_provenance(
                    input_digest=planned.input_digest, input_schema=planned.input_schema,
                    client=client)
                if client is None:
                    return settle("unavailable", empty_proposals(), provenance)
                # Finalize is an untrusted-agent proposal boundary. Even the legacy `auto` flag
                # cannot mutate taxonomy before a durable receipt; only an explicit operator command
                # may apply a proposal.
                with self._paid_curation_attempt_locked(
                        log_name, kind, final, curation_key, provenance, incomplete) as invoke:
                    if not invoke:
                        return "already-resolved"
                    try:
                        outcome, proposals = planned.propose(client)
                        return settle(outcome, proposals, provenance, durable=True)
                    except Exception as exc:  # noqa: BLE001 - close while decision lock is held
                        self._append_curation_once(
                            log_name, final, curation_key, provenance,
                            row("error", empty_proposals(), error_type=type(exc).__name__),
                            require_durable=True)
                        return "error"
        except Exception as exc:  # noqa: BLE001 — agentic curation must never fail a run
            try:
                self._append_curation_once(
                    log_name, final, diagnostic_key, diagnostic_provenance,
                    row("error", (diagnostic_proposals or empty_proposals)(),
                        error_type=type(exc).__name__),
                    require_durable=True)
            except Exception:  # noqa: BLE001 — logging stays best-effort relative to finalization
                pass
            return "error"

    def store_concept_curation(self, final: RunState) -> str:
        """PART IV §22.4 — the AGENTIC taxonomy steward at finalize: when `cross_run_curation` is on and an
        LLM client is available (`reflect_client`), let the LLM review the freshly-updated portfolio concept
        graph and PROPOSE a curation (merge/split/purge). Every outcome, including an empty proposal or an
        unavailable client, is durably LOGGED to `concept_curation_log.jsonl` for operator ratification.
        Finalize never applies an agent proposal: mutation requires an explicit operator CLI/API action.
        Portfolio-scoped and fully decoupled from the run's terminal state — best-effort, never raises."""
        def plan(_final):
            from looplab.engine.concept_steward import (
                CONCEPT_CURATION_INPUT_SCHEMA,
                concept_curation_has_input,
                concept_curation_snapshot,
                curation_is_empty,
                propose_concept_curation,
            )

            overview, input_digest = concept_curation_snapshot(self._e.memory_dir)

            def propose(client):
                proposals = propose_concept_curation(
                    overview, client, parser=_FINALIZE_STEWARD_PARSER, raise_on_failure=True)
                return ("empty" if curation_is_empty(proposals) else "proposed"), proposals

            return _StewardPlan(
                input_schema=CONCEPT_CURATION_INPUT_SCHEMA,
                input_digest=input_digest,
                curation_key=self._portfolio_curation_key("concept", input_digest),
                has_input=bool(concept_curation_has_input(overview)),
                propose=propose,
            )

        return self._run_finalize_steward(
            final, kind="concept",
            unavailable_schema="finalize-concept-curation/input-unavailable",
            empty_proposals=lambda: {"merges": [], "splits": [], "purges": []},
            plan=plan)

    def store_claim_curation(self, final: RunState) -> str:
        """PART IV §22.4 — the AGENTIC CLAIM steward at finalize (companion to `store_concept_curation`):
        the LLM reviews the evidence-grounded claim assessments and PROPOSES operator decisions
        (ratify/reject/pin). All outcomes are locked/durably logged to `claim_curation_log.jsonl`; finalize
        never applies them. Same gate/decoupling/best-effort contract as the concept steward."""
        def plan(_final):
            from looplab.engine.claim_steward import (
                CLAIM_CURATION_INPUT_SCHEMA,
                claim_curation_has_input,
                claim_curation_snapshot,
                curation_is_empty,
                propose_claim_curation,
            )

            claims, input_digest = claim_curation_snapshot(self._e.memory_dir, structured=True)

            def propose(client):
                proposals = propose_claim_curation(
                    claims, client, parser=_FINALIZE_STEWARD_PARSER, raise_on_failure=True)
                return ("empty" if curation_is_empty(proposals) else "proposed"), proposals

            return _StewardPlan(
                input_schema=CLAIM_CURATION_INPUT_SCHEMA,
                input_digest=input_digest,
                curation_key=self._portfolio_curation_key("claim", input_digest),
                has_input=bool(claim_curation_has_input(claims)),
                propose=propose,
            )

        return self._run_finalize_steward(
            final, kind="claim",
            unavailable_schema="finalize-claim-curation/input-unavailable",
            empty_proposals=lambda: {"decisions": []},
            plan=plan)

    def store_task_facets(self, final: RunState) -> str:
        """PART IV §21.20.2 — propose task facets and queue them for operator ratification.

        Facets can widen retrieval scope, so agent output is never silently promoted into policy at finalize.
        Outcomes are written once/task to `task_facets_curation_log.jsonl`, including empty/unavailable ones.
        """
        def plan(final_state):
            tid = str(getattr(final_state, "task_id", "") or "")
            if not tid:
                return "empty"
            from looplab.engine.task_facets import (
                TASK_FACETS_INPUT_SCHEMA,
                load_task_facets,
                propose_task_facets,
                task_facets_goal_is_empty,
                task_facets_input_digest,
            )

            goal = str(getattr(final_state, "goal", "") or "")
            kind = str(getattr(getattr(self._e, "task", None), "kind", "") or "")

            def already_governed():
                # Facets are once/TASK, so a task the operator already governs must settle before any
                # provider call — and inside the lock, so it cannot race a paid attempt.
                current = load_task_facets(self._e.memory_dir).get(tid)
                return None if current is None else (
                    "already-governed", {"task_id": tid, "facets": current})

            def empty_goal():
                return ("empty", {"task_id": tid, "facets": {}}) if task_facets_goal_is_empty(
                    goal, kind) else None

            def propose(client):
                facets = propose_task_facets(
                    goal, kind, client, parser=_FINALIZE_STEWARD_PARSER, raise_on_failure=True)
                return ("proposed" if facets else "empty"), {"task_id": tid, "facets": facets}

            return _StewardPlan(
                input_schema=TASK_FACETS_INPUT_SCHEMA,
                input_digest=task_facets_input_digest(goal, kind),
                # Facets are once/task, so differently worded runs share this decision lock.
                curation_key=self._facets_curation_key(tid),
                has_input=True,        # the goal check above is the facets steward's "no input"
                propose=propose,
                fast_paths=(already_governed, empty_goal),
            )

        return self._run_finalize_steward(
            final, kind="facets",
            unavailable_schema="finalize-task-facets/input-unavailable",
            empty_proposals=lambda: {"task_id": str(getattr(final, "task_id", "") or ""),
                                     "facets": {}},
            plan=plan)
