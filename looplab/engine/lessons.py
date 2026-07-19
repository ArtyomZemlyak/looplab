"""Cross-run memory / lessons / reflection for the engine (extracted from orchestrator.py):
the E4 meta-note prior, M2/M3 fingerprint-keyed lessons (incl. negatives), M6 comparative
(credit-assigned pair) lessons with their mid-run distill/refresh cadences, M4 auto-distilled
skills, D2 store hygiene (consolidate/compact), and the I19 case library write.

`LessonMemory` wraps the engine instance (`self._e`) rather than owning copies of its state:
the method bodies are verbatim moves from the Engine, reading the engine's knobs/store/task
through `self._e` and calling sibling cluster methods through the Engine's thin delegators
(so a test monkeypatching e.g. `engine._reflect_client` still intercepts every internal call).
Only the purely lessons-owned mutable state (`seen_stamp`, `prior_note_text`) lives here; the
Engine exposes them back under the original attribute names via properties.

Decomposed the same way the Engine was (see engine/novelty.py's mixin convention): the prior
loading (lessons_priors.py), LLM distillation (lessons_distill.py) and comparative/reconcile
(lessons_reconcile.py) clusters are MIXINS on `LessonMemory` — verbatim method moves, `self`
there IS the LessonMemory, zero call-site churn. This module keeps the constructor/owned state,
the store append + the maybe_* cadence wrappers, and the static file maintenance; the Engine's
CLASS-attribute refs (`LessonMemory.spent_pairs` / `consolidate_lessons_file` /
`compact_lessons`) keep resolving through mixin inheritance.

Layering: this module must not import the orchestrator (TYPE_CHECKING only) and never imports
serve — it touches only engine.memory, events, core and stdlib/orjson."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

import orjson

from looplab.core.atomicio import append_jsonl_bytes_locked
from looplab.core.models import RunState, classifier_verified_node_concepts, latest_lesson_node_count
from looplab.engine.lessons_distill import LessonDistillMixin
# The role constants moved to lessons_priors.py with the prior renderer that filters on them;
# re-imported so `from looplab.engine.lessons import LESSON_ROLE_*` (tests, cross-run tooling)
# keeps resolving.
from looplab.engine.lessons_priors import (  # noqa: F401
    LESSON_ROLE_DEVELOPER, LESSON_ROLE_RESEARCHER, LessonPriorsMixin)
from looplab.engine.lessons_reconcile import LessonReconcileMixin
from looplab.engine.memory import JsonlCaseLibrary
from looplab.events.eventstore import read_jsonl_lenient, write_jsonl_atomic
from looplab.events.replay import fold
from looplab.events.types import EV_LESSONS_DISTILLED, EV_LESSONS_REFRESHED

if TYPE_CHECKING:  # engine type hint only — no runtime import of the orchestrator
    from looplab.engine.orchestrator import Engine


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


class LessonMemory(LessonPriorsMixin, LessonDistillMixin, LessonReconcileMixin):
    """The engine's cross-run memory / lessons / reflection cluster. See the module docstring
    for the `self._e` (engine handle) convention and the mixin decomposition."""

    def __init__(self, engine: "Engine") -> None:
        self._e = engine
        self.seen_stamp = None   # (size, mtime_ns) of the store at the last read
        self.prior_note_text = ""   # E4: cross-run RESEARCHER prior (R&D lessons), loaded at run start
        self.dev_prior_note_text = ""   # §role-split: cross-run DEVELOPER prior (code-fix lessons)
        # Reconcile gate: a hash of {node_id -> outcome-signature} at the last reconcile scan. Recomputed
        # each cadence pass (cheap, no I/O); when it CHANGES (a node reached / left / flipped a terminal —
        # in particular a node_reset re-eval that altered a metric or status), we re-read the lesson file
        # and re-derive any of THIS run's lessons whose evidence sig moved. None on start → first pass
        # always scans (verifies the store against the folded state after a restart/resume).
        self._reconcile_sig_hash = None

    def empty_state_for_fp(self) -> RunState:
        """Minimal RunState carrying just what `_task_fingerprint` reads at run START (before any
        node), so the prior loader can fingerprint the current task the same way the writer will."""
        return RunState(task_id=self._e.task.id, goal=getattr(self._e.task, "goal", ""),
                        direction=getattr(self._e.task, "direction", "min"))

    def task_fingerprint(self, final: RunState, best=None) -> list[str]:
        """M2: content fingerprint of this task so cross-run transfer reaches SIMILAR tasks, not only
        the exact same task_id. Built from kind/direction/metric/goal keywords + the winner's params."""
        from looplab.engine.memory import task_fingerprint
        # NOTE (CODEX): the winner's param NAMES are outcome-derived, so this fingerprint shifts when a new
        # node wins / the run extends — i.e. it is a fuzzy RETRIEVAL key, not an immutable scope identity.
        # This is the SHIPPED convention (store_case / lesson priors key the same way); the capsule reuses
        # it for consistency. The immutable task/ComparisonContract identity is the CR0 TODO (§21.20.13) —
        # deliberately NOT changed here, since it would re-key every existing lesson/case store.
        pnames = list((best.idea.params or {}).keys()) if best is not None and best.idea else []
        return task_fingerprint(getattr(self._e.task, "kind", ""), final.direction,
                                final.goal or getattr(self._e.task, "goal", ""),
                                metric=str(getattr(self._e.task, "metric", "") or ""),
                                param_names=pnames,
                                universal=bool(getattr(self._e, "_fingerprint_universal", False)))

    def append_lessons(self, lessons: list, *, hygiene: bool = True) -> None:
        """Append lessons to the SHARED cross-run store. Used by run-end reflection AND the M6
        mid-run distillation, so a lesson distilled mid-flight is visible to a concurrent run's
        refresh immediately. Concurrency: the whole append (and the optional hygiene rewrite) runs
        under the same best-effort interprocess lock the event store uses — the D2 consolidate/
        compact pass is a full-file read-modify-write, and without the lock a concurrent run's
        O_APPEND between our read and our rewrite would be silently clobbered (losing exactly the
        cross-run lesson the live share exists to propagate). `hygiene=False` (the mid-run path)
        skips consolidate/compact entirely: the read path already dedups and quarantines, so
        hygiene can wait for run end instead of rewriting a shared file every few nodes."""
        if not (lessons and self._e.memory_dir):
            return
        from looplab.events.eventstore import _interprocess_lock
        base = Path(self._e.memory_dir)
        base.mkdir(parents=True, exist_ok=True)
        path = base / "lessons.jsonl"
        payload = b"\n".join(orjson.dumps(lz) for lz in lessons)
        with _interprocess_lock(Path(str(path) + ".lock")):
            append_jsonl_bytes_locked(path, payload)
            if hygiene:
                # D2 hygiene: consolidate the store after appending — merge duplicate claims into
                # an evidence_count, retire contradicted verdicts (newest wins), THEN bound size. The
                # Researcher client + embedder enable the hybrid+agent paraphrase-merge pass (run end
                # only — hygiene=False mid-run skips it, so the shared file isn't rewritten every node).
                # prompts/parser travel WITH the client so a merge_system.md override and the run's
                # configured structured-output parser reach the merge's adjudication call (I18/ADR-8).
                prompts, parser = self._merge_prompt_opts()
                self._e._consolidate_lessons_file(path, self._e._reflect_client(), self._e._embedder,
                                                  parser=parser, prompts=prompts)
                self._e._compact_lessons(path)

    def maybe_distill_lessons(self, state: RunState) -> RunState:
        """M6 write side (doc 13 §7 items 2+5): every `lessons_every` NEW nodes, distill
        comparative lessons and append them to the SHARED cross-run store IMMEDIATELY — a
        concurrent run's refresh (read side below) can pick them up mid-flight, the AgentRxiv
        live-share pattern. The `lessons_distilled` event is the replay-safe gate (at_node +
        the pair ids already spent); fires only at a creation decision point (no pending evals),
        mirroring deep-research. No-op when the cadence is 0 or reflection memory is off."""
        if (self._e.lessons_every <= 0 or not self._e._comparative_lessons_on
                or not (self._e._reflection_priors and self._e.memory_dir)):
            return state
        if state.pending_nodes():
            return state
        n = len(state.nodes)
        last = latest_lesson_node_count(state.lessons_distilled)
        if not self._e._cadence_due(n, last, self._e.lessons_every):
            return state
        fp = self._e._task_fingerprint(state, state.best())
        lessons, pairs = self._e._comparative_lessons(state, fp, exclude=self._e._spent_pairs(state))
        # Event BEFORE the store write, and always — even with 0 lessons — so the at_node gate
        # advances and the loop doesn't retry this node-count. Event-first ordering: if the
        # process dies between the two writes, a resume sees the gate advanced and skips — the
        # store misses one batch (best-effort memory) instead of re-invoking the LLM and
        # appending the same lessons twice. The statements ride in the event for audit.
        self._e.store.append(EV_LESSONS_DISTILLED, {
            "at_node": n, "trigger": "cadence", "count": len(lessons),
            "pairs": [[pr["a"], pr["b"]] for pr in pairs],
            "lessons": [{"statement": lz["statement"], "outcome": lz["outcome"],
                         "claim_stance": lz.get("claim_stance"),
                         "evidence": lz.get("evidence")} for lz in lessons]})
        # Hygiene deferred to run end: the read path dedups/quarantines already, and a full-file
        # rewrite of the shared store every few nodes would race other runs' appends for nothing.
        self._e._append_lessons(lessons, hygiene=False)
        return fold(self._e.store.read_all())

    def lessons_store_stamp(self):
        """(size, mtime_ns) of the shared lessons store, or None — the cheap change detector the
        refresh gate uses to skip a full re-read/re-score when no run has written since."""
        if not self._e.memory_dir:
            return None
        try:
            st = (Path(self._e.memory_dir) / "lessons.jsonl").stat()
            return (st.st_size, st.st_mtime_ns)
        except OSError:
            return None

    def maybe_refresh_lessons(self, state: RunState) -> RunState:
        """M6 read side (doc 13 §7 item 5): every `lessons_refresh_every` NEW nodes, re-read the
        SHARED cross-run store and rebuild the proposal prior — so lessons a CONCURRENT run
        distilled after this run started reach this run's next proposals (pre-M6, the store was
        read at run start only). No LLM call; this run's own lessons are excluded (they're already
        in the digest). The `lessons_refreshed` event is the replay-safe cadence gate. When the
        store file is UNCHANGED since the last look (stat stamp), the rebuild — a full re-read +
        re-score (+ harmonic re-embed) of every lesson — is skipped; the gate still advances.
        No-op when the cadence is 0 or reflection memory is off."""
        if self._e.lessons_refresh_every <= 0 or not (self._e._reflection_priors and self._e.memory_dir):
            return state
        n = len(state.nodes)
        last = latest_lesson_node_count(state.lessons_refreshed)
        if not self._e._cadence_due(n, last, self._e.lessons_refresh_every):
            return state
        stamp = self._e._lessons_store_stamp()
        if stamp == self.seen_stamp:
            self._e.store.append(EV_LESSONS_REFRESHED, {"at_node": n, "skipped": "unchanged"})
            return fold(self._e.store.read_all())
        self.seen_stamp = stamp
        before = (self.prior_note_text, self.dev_prior_note_text)
        rid = state.run_id or None
        # ONE scan for BOTH role priors (see load_reflection_priors_both). `changed` must reflect
        # EITHER prior moving — a concurrent run distilling only developer-tagged code-fix lessons
        # updates just dev_prior_note_text, and the refresh audit signal must not report that as
        # unchanged. `chars` sums both priors so the size delta is likewise visible for either role.
        self.prior_note_text, self.dev_prior_note_text = \
            self._e._load_reflection_priors_both(exclude_run_id=rid)
        self._e.store.append(EV_LESSONS_REFRESHED, {
            "at_node": n, "chars": len(self.prior_note_text) + len(self.dev_prior_note_text),
            "changed": (self.prior_note_text, self.dev_prior_note_text) != before})
        return fold(self._e.store.read_all())

    def reflect_client(self):
        """The LLM client to use for run-end distillation — the Researcher's (unwrapping any
        surrogate/fallback), else the Developer's. None when no LLM client is wired (toy backends)."""
        r = getattr(self._e, "researcher", None)
        for obj in (r, getattr(r, "inner", None), getattr(r, "fallback", None),
                    getattr(self._e, "developer", None)):
            c = getattr(obj, "client", None)
            if c is not None and hasattr(c, "complete_text"):
                return c
        return None

    @staticmethod
    def consolidate_lessons_file(path: Path, client=None, embed=None,
                                 parser: str = "tool_call", prompts=None) -> None:
        """D2: rewrite lessons.jsonl through `consolidate_lessons` — duplicate claims merge into
        an evidence_count and a contradicted verdict is retired (the newest observation wins). When a
        `client` is wired, a hybrid-retrieval + agent pass ALSO merges paraphrase-level duplicates the
        exact key misses (`parser`/`prompts` configure that pass's structured-output parser and
        merge_system override). Atomic rewrite; best-effort (a hygiene failure must never fail the run)."""
        try:
            from looplab.engine.memory import consolidate_lessons
            rows = read_jsonl_lenient(path)
            merged = consolidate_lessons(rows, client=client, embed=embed,
                                         parser=parser, prompts=prompts)
            if len(merged) < len(rows):
                write_jsonl_atomic(path, merged)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def compact_lessons(path: Path, max_lines: int = 4000, keep: int = 2000) -> None:
        """Bound the shared lessons store: it is re-read and scored at every run start, and grows by
        a few lines per finished run forever. Past `max_lines`, keep the most recent `keep` (recency
        also wins ties at retrieval, so the dropped prefix is the least useful part)."""
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > max_lines:
                from looplab.core.atomicio import atomic_write_text
                atomic_write_text(path, "\n".join(lines[-keep:]) + "\n")
        except Exception:  # noqa: BLE001 — compaction is best-effort; never fail the run for it
            pass

    def store_case(self, final: RunState) -> None:
        """Cross-run memory (I19): persist the best result as a retrievable case."""
        if not self._e.memory_dir:
            return
        best = final.best()
        if best is None:
            return
        lib = JsonlCaseLibrary(Path(self._e.memory_dir) / "cases.jsonl")
        lib.add({
            "task_id": final.task_id,
            "goal": final.goal,
            "direction": final.direction,
            "params": best.idea.params,
            "metric": best.robust_metric,
            "rationale": best.idea.rationale,
        })

    def store_concept_capsule(self, final: RunState) -> None:
        """PART IV cross-run Step 2 (§21.20): persist this run's CONCEPT capsule to the shared
        `memory_dir` so a later SIMILAR run can surface "this was tried before -> outcome". Best-effort
        and self-contained: reuses the shipped per-run `node_concepts` tags (no new tagger) and the
        universal-aware `task_fingerprint`; per-concept outcome = the best robust_metric among nodes
        carrying that concept. Never raises — cross-run memory must never fail a run."""
        if not self._e.memory_dir:
            return
        try:
            node_concepts = getattr(final, "node_concepts", None) or {}
            if not node_concepts:
                return                          # nothing tagged this run -> no capsule (no-op)
            from looplab.engine.memory import build_concept_capsule
            from looplab.events.replay import promotion_eligible_nodes
            direction = final.direction
            if direction not in ("min", "max"):
                return

            def _better(a, b) -> bool:          # is metric a strictly better than b for this direction?
                return a < b if direction == "min" else a > b

            # NOTE (CODEX): raw per-run concept LABELS — no `concept_consolidation` canonicalization / UID /
            # taxonomy version yet (the CR1a concept_uid resolver is the §21.20.13 TODO), so a later reader
            # matches by exact string. Attempt coverage retains every tagged node, while a durable numeric
            # outcome may come only from the same feasible, live, unflagged pool used for promotion.
            concepts, outcomes = set(), {}
            eligible_ids = {node.id for node in promotion_eligible_nodes(final)}
            for nd in final.nodes.values():
                m = getattr(nd, "robust_metric", None)
                # CODEX AGENT: capsules advise future agents; never promote the proposer's own taxonomy
                # claims into seemingly independent cross-run evidence.
                for c in classifier_verified_node_concepts(final, nd.id):
                    concepts.add(str(c))
                    if (nd.id in eligible_ids and m is not None
                            and (outcomes.get(c) is None or _better(m, outcomes[c]))):
                        outcomes[str(c)] = m
            if not concepts:
                return
            best = final.best()
            capsule = build_concept_capsule(
                run_id=final.run_id or final.task_id, task_id=final.task_id, direction=direction,
                concepts=concepts, fingerprint=self.task_fingerprint(final, best),
                best_metric=(best.robust_metric if best is not None else None),
                concept_outcomes=outcomes)
        except Exception:  # noqa: BLE001 — BUILD is best-effort: a projection hiccup must never fail a run
            return
        # The WRITE is NOT swallowed (mirrors store_case): a real persistence failure must reach finalize's
        # retry handshake (which sets complete=False and retries on the next re-entry) rather than being
        # silently lost while `finalization_finished` is committed (CODEX).
        from looplab.engine.memory import ConceptCapsuleStore
        ConceptCapsuleStore(Path(self._e.memory_dir) / "concept_capsules.jsonl").add(capsule)

    def _already_curated(self, log_name: str, curation_key: str) -> bool:
        """Whether semantic work has a terminal outcome; unavailable clients do not consume the key."""
        from looplab.events.eventstore import read_jsonl_lenient

        p = Path(self._e.memory_dir) / log_name
        if not p.exists():
            return False
        return any(
            str(r.get("curation_key") or "") == curation_key
            and str(r.get("outcome") or "") != "unavailable"
            for r in read_jsonl_lenient(p, loads=json.loads, dicts_only=True)
        )

    @staticmethod
    def _curation_finish_seq(final: RunState) -> int | None:
        finish_seq = getattr(final, "last_finish_seq", None)
        return (finish_seq if isinstance(finish_seq, int) and not isinstance(finish_seq, bool)
                and finish_seq >= 0 else None)

    @classmethod
    def _curation_source_key(cls, final: RunState) -> str:
        source = {
            "v": 1,
            "run_id": str(final.run_id or ""),
            "task_id": str(final.task_id or ""),
            "finish_seq": cls._curation_finish_seq(final),
        }
        encoded = json.dumps(
            source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "source:v1:" + hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _portfolio_curation_key(kind: str, input_digest: str) -> str:
        if kind not in {"concept", "claim"} or len(input_digest) != 64:
            raise ValueError("invalid portfolio curation identity")
        # CODEX AGENT: paid portfolio work is identified by the exact frozen model input, never by
        # whichever run happened to trigger finalize.  This is both cross-run dedup and the TOCTOU fence.
        return f"{kind}:v2:{input_digest}"

    @staticmethod
    def _facets_curation_key(task_id: str) -> str:
        tid = str(task_id or "")
        if not tid:
            raise ValueError("task facets require an exact task_id")
        encoded = json.dumps(
            {"v": 2, "kind": "facets", "task_id": tid}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "facets:v2:" + hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _diagnostic_curation_key(cls, kind: str, final: RunState) -> str:
        return f"{kind}:diagnostic:v2:{cls._curation_source_key(final).rsplit(':', 1)[-1]}"

    def _curation_provenance(self, *, input_digest: str, input_schema: str,
                             client) -> dict:
        from looplab.trust.redact import redact_persisted_text

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
        from looplab.events.eventstore import read_jsonl_lenient

        rid, tid = str(final.run_id or ""), str(final.task_id or "")
        path = Path(self._e.memory_dir) / log_name
        if not rid or not path.exists():
            return False
        return any(
            not row.get("curation_key")
            and str(row.get("run_id") or "") == rid
            and str(row.get("task_id") or "") == tid
            and str(row.get("outcome") or "") != "unavailable"
            for row in read_jsonl_lenient(path, loads=json.loads, dicts_only=True)
        )

    def _write_curation_claim(self, path: Path, log_name: str, kind: str,
                              final: RunState, curation_key: str,
                              provenance: dict, incomplete: dict) -> None:
        """Create and strictly sync the one-way claim that gates a paid finalize invocation."""
        from looplab.core.atomicio import strict_fsync, strict_fsync_parent

        auto_requested = incomplete.get("auto_requested")
        if not isinstance(auto_requested, bool):
            raise ValueError("paid curation claim requires boolean auto_requested")
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
            "run_id": str(final.run_id or ""),
            "task_id": str(final.task_id or ""),
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
            "run_id": 1000,
            "task_id": 1000,
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
                    or "\n" in value or "\r" in value):
                # Run ids may be empty in historical state, but a durable claim still binds the exact
                # empty value. Handle those two identity fields separately below.
                if field not in {"run_id", "task_id"} or value != "":
                    raise ValueError(f"invalid curation claim {field}")
        finish_seq = claim.get("finish_seq")
        if (finish_seq is not None
                and (isinstance(finish_seq, bool) or not isinstance(finish_seq, int)
                     or finish_seq < 0)):
            raise ValueError("invalid curation claim finish_seq")
        digest = claim["input_digest"]
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("invalid curation claim input_digest")
        from looplab.trust.redact import redact_persisted_text
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
        # CODEX AGENT: recovery metadata comes exclusively from the durable paid claim. A retrying
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
        from looplab.events.eventstore import read_jsonl_lenient

        class _AlreadyLogged(RuntimeError):
            pass

        path = Path(self._e.memory_dir) / log_name
        path.parent.mkdir(parents=True, exist_ok=True)
        source_key = self._curation_source_key(final)
        outcome = str(rec.get("outcome") or "")

        def _validate_locked() -> None:
            if not path.exists():
                return
            for row in read_jsonl_lenient(path, loads=json.loads, dicts_only=True):
                if str(row.get("curation_key") or "") != curation_key:
                    continue
                prior_outcome = str(row.get("outcome") or "")
                if outcome == "unavailable":
                    # CODEX AGENT: a late no-client observer is an audit only. Once another process
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
                path, payload, validate=_validate_locked, require_durable=require_durable)
            return True
        except _AlreadyLogged:
            return False

    def store_concept_curation(self, final: RunState) -> None:
        """PART IV §22.4 — the AGENTIC taxonomy steward at finalize: when `cross_run_curation` is on and an
        LLM client is available (`reflect_client`), let the LLM review the freshly-updated portfolio concept
        graph and PROPOSE a curation (merge/split/purge). Every outcome, including an empty proposal or an
        unavailable client, is durably LOGGED to `concept_curation_log.jsonl` for operator ratification.
        Finalize never applies an agent proposal: mutation requires an explicit operator CLI/API action.
        Portfolio-scoped and fully decoupled from the run's terminal state — best-effort, never raises."""
        if not (self._e.memory_dir and getattr(self._e, "_cross_run_curation", False)):
            return
        log_name, steward_kind = "concept_curation_log.jsonl", "concept"
        auto_requested = bool(getattr(self._e, "_cross_run_curation_auto", False))
        diagnostic_key = self._diagnostic_curation_key(steward_kind, final)
        diagnostic_provenance = self._curation_provenance(
            input_digest="", input_schema="finalize-concept-curation/input-unavailable", client=None)
        try:
            from looplab.engine.concept_steward import (
                CONCEPT_CURATION_INPUT_SCHEMA,
                concept_curation_has_input,
                concept_curation_snapshot,
                curation_is_empty,
                propose_concept_curation,
            )

            overview, input_digest = concept_curation_snapshot(self._e.memory_dir)
            curation_key = self._portfolio_curation_key(steward_kind, input_digest)
            incomplete = {
                "outcome": "prior_attempt_incomplete_not_replayed",
                "ambiguity": "provider_outcome_unknown",
                "auto": False, "auto_requested": auto_requested,
                "proposals": {"merges": [], "splits": [], "purges": []}, "receipt": None,
            }
            with self._curation_decision_lock(log_name, final, curation_key):
                if self._curation_attempt_already_resolved_locked(
                        log_name, steward_kind, final, curation_key, incomplete):
                    return
            if not concept_curation_has_input(overview):
                provenance = self._curation_provenance(
                    input_digest=input_digest, input_schema=CONCEPT_CURATION_INPUT_SCHEMA,
                    client=None)
                self._append_curation_once(log_name, final, curation_key, provenance, {
                    "outcome": "empty", "auto": False, "auto_requested": auto_requested,
                    "proposals": {"merges": [], "splits": [], "purges": []}, "receipt": None})
                return
            client = self.reflect_client()
            provenance = self._curation_provenance(
                input_digest=input_digest, input_schema=CONCEPT_CURATION_INPUT_SCHEMA,
                client=client)
            if client is None:
                self._append_curation_once(log_name, final, curation_key, provenance, {
                    "outcome": "unavailable", "auto": False, "auto_requested": auto_requested,
                    "proposals": {"merges": [], "splits": [], "purges": []}, "receipt": None})
                return
            # Finalize is an untrusted-agent proposal boundary. Even the legacy `auto` flag cannot mutate
            # taxonomy before a durable receipt; only an explicit operator command may apply.
            with self._paid_curation_attempt(
                    log_name, steward_kind, final, curation_key, provenance, incomplete) as invoke:
                if not invoke:
                    return
                try:
                    proposals = propose_concept_curation(
                        overview, client, parser=_FINALIZE_STEWARD_PARSER,
                        raise_on_failure=True)
                    self._append_curation_once(log_name, final, curation_key, provenance, {
                        "outcome": "empty" if curation_is_empty(proposals) else "proposed",
                        "auto": False, "auto_requested": auto_requested,
                        "proposals": proposals, "receipt": None}, require_durable=True)
                except Exception as exc:  # noqa: BLE001 - close the paid attempt while lock is held
                    self._append_curation_once(log_name, final, curation_key, provenance, {
                        "outcome": "error", "error_type": type(exc).__name__, "auto": False,
                        "auto_requested": auto_requested,
                        "proposals": {"merges": [], "splits": [], "purges": []},
                        "receipt": None}, require_durable=True)
        except Exception as exc:  # noqa: BLE001 — agentic curation must never fail a run
            try:
                self._append_curation_once(
                    log_name, final, diagnostic_key, diagnostic_provenance, {
                    "outcome": "error", "error_type": type(exc).__name__, "auto": False,
                    "auto_requested": auto_requested,
                    "proposals": {"merges": [], "splits": [], "purges": []}, "receipt": None},
                    require_durable=True)
            except Exception:  # noqa: BLE001 — logging remains best-effort relative to run finalization
                pass

    def store_claim_curation(self, final: RunState) -> None:
        """PART IV §22.4 — the AGENTIC CLAIM steward at finalize (companion to `store_concept_curation`):
        the LLM reviews the evidence-grounded claim assessments and PROPOSES operator decisions
        (ratify/reject/pin). All outcomes are locked/durably logged to `claim_curation_log.jsonl`; finalize
        never applies them. Same gate/decoupling/best-effort contract as the concept steward."""
        if not (self._e.memory_dir and getattr(self._e, "_cross_run_curation", False)):
            return
        log_name, steward_kind = "claim_curation_log.jsonl", "claim"
        auto_requested = bool(getattr(self._e, "_cross_run_curation_auto", False))
        diagnostic_key = self._diagnostic_curation_key(steward_kind, final)
        diagnostic_provenance = self._curation_provenance(
            input_digest="", input_schema="finalize-claim-curation/input-unavailable", client=None)
        try:
            from looplab.engine.claim_steward import (
                CLAIM_CURATION_INPUT_SCHEMA,
                claim_curation_has_input,
                claim_curation_snapshot,
                curation_is_empty,
                propose_claim_curation,
            )

            claims, input_digest = claim_curation_snapshot(self._e.memory_dir, structured=True)
            curation_key = self._portfolio_curation_key(steward_kind, input_digest)
            incomplete = {
                "outcome": "prior_attempt_incomplete_not_replayed",
                "ambiguity": "provider_outcome_unknown",
                "auto": False, "auto_requested": auto_requested,
                "proposals": {"decisions": []}, "receipt": None,
            }
            with self._curation_decision_lock(log_name, final, curation_key):
                if self._curation_attempt_already_resolved_locked(
                        log_name, steward_kind, final, curation_key, incomplete):
                    return
            if not claim_curation_has_input(claims):
                provenance = self._curation_provenance(
                    input_digest=input_digest, input_schema=CLAIM_CURATION_INPUT_SCHEMA,
                    client=None)
                self._append_curation_once(log_name, final, curation_key, provenance, {
                    "outcome": "empty", "auto": False, "auto_requested": auto_requested,
                    "proposals": {"decisions": []}, "receipt": None})
                return
            client = self.reflect_client()
            provenance = self._curation_provenance(
                input_digest=input_digest, input_schema=CLAIM_CURATION_INPUT_SCHEMA,
                client=client)
            if client is None:
                self._append_curation_once(log_name, final, curation_key, provenance, {
                    "outcome": "unavailable", "auto": False, "auto_requested": auto_requested,
                    "proposals": {"decisions": []}, "receipt": None})
                return
            with self._paid_curation_attempt(
                    log_name, steward_kind, final, curation_key, provenance, incomplete) as invoke:
                if not invoke:
                    return
                try:
                    proposals = propose_claim_curation(
                        claims, client, parser=_FINALIZE_STEWARD_PARSER,
                        raise_on_failure=True)
                    self._append_curation_once(log_name, final, curation_key, provenance, {
                        "outcome": "empty" if curation_is_empty(proposals) else "proposed",
                        "auto": False, "auto_requested": auto_requested,
                        "proposals": proposals, "receipt": None}, require_durable=True)
                except Exception as exc:  # noqa: BLE001 - close the paid attempt while lock is held
                    self._append_curation_once(log_name, final, curation_key, provenance, {
                        "outcome": "error", "error_type": type(exc).__name__, "auto": False,
                        "auto_requested": auto_requested, "proposals": {"decisions": []},
                        "receipt": None}, require_durable=True)
        except Exception as exc:  # noqa: BLE001 — agentic curation must never fail a run
            try:
                self._append_curation_once(
                    log_name, final, diagnostic_key, diagnostic_provenance, {
                    "outcome": "error", "error_type": type(exc).__name__, "auto": False,
                    "auto_requested": auto_requested, "proposals": {"decisions": []}, "receipt": None},
                    require_durable=True)
            except Exception:  # noqa: BLE001
                pass

    def store_task_facets(self, final: RunState) -> None:
        """PART IV §21.20.2 — propose task facets and queue them for operator ratification.

        Facets can widen retrieval scope, so agent output is never silently promoted into policy at finalize.
        Outcomes are written once/task to `task_facets_curation_log.jsonl`, including empty/unavailable ones.
        """
        if not (self._e.memory_dir and getattr(self._e, "_cross_run_curation", False)):
            return
        log_name, steward_kind = "task_facets_curation_log.jsonl", "facets"
        auto_requested = bool(getattr(self._e, "_cross_run_curation_auto", False))
        diagnostic_key = self._diagnostic_curation_key(steward_kind, final)
        diagnostic_provenance = self._curation_provenance(
            input_digest="", input_schema="finalize-task-facets/input-unavailable", client=None)
        try:
            tid = str(getattr(final, "task_id", "") or "")
            if not tid:
                return
            from looplab.engine.task_facets import (
                TASK_FACETS_INPUT_SCHEMA,
                load_task_facets,
                propose_task_facets,
                task_facets_input_digest,
            )

            goal = str(getattr(final, "goal", "") or "")
            kind = str(getattr(getattr(self._e, "task", None), "kind", "") or "")
            input_digest = task_facets_input_digest(goal, kind)
            curation_key = self._facets_curation_key(tid)
            incomplete = {
                "outcome": "prior_attempt_incomplete_not_replayed",
                "ambiguity": "provider_outcome_unknown",
                "auto": False, "auto_requested": auto_requested,
                "proposals": {"task_id": tid, "facets": {}}, "receipt": None,
            }
            # CODEX AGENT: facets are once/task, so differently worded runs share this decision lock.
            # Fast empty/governed decisions must not race a paid attempt and discard its result.
            with self._curation_decision_lock(log_name, final, curation_key):
                if self._curation_attempt_already_resolved_locked(
                        log_name, steward_kind, final, curation_key, incomplete):
                    return
                current = load_task_facets(self._e.memory_dir).get(tid)
                if current is not None:
                    provenance = self._curation_provenance(
                        input_digest=input_digest, input_schema=TASK_FACETS_INPUT_SCHEMA,
                        client=None)
                    self._append_curation_once(log_name, final, curation_key, provenance, {
                        "outcome": "already-governed", "auto": False,
                        "auto_requested": auto_requested,
                        "proposals": {"task_id": tid, "facets": current}, "receipt": None})
                    return
                if not goal[:4000].strip():
                    provenance = self._curation_provenance(
                        input_digest=input_digest, input_schema=TASK_FACETS_INPUT_SCHEMA,
                        client=None)
                    self._append_curation_once(log_name, final, curation_key, provenance, {
                        "outcome": "empty", "auto": False, "auto_requested": auto_requested,
                        "proposals": {"task_id": tid, "facets": {}}, "receipt": None})
                    return
                client = self.reflect_client()
                provenance = self._curation_provenance(
                    input_digest=input_digest, input_schema=TASK_FACETS_INPUT_SCHEMA,
                    client=client)
                if client is None:
                    self._append_curation_once(log_name, final, curation_key, provenance, {
                        "outcome": "unavailable", "auto": False,
                        "auto_requested": auto_requested,
                        "proposals": {"task_id": tid, "facets": {}}, "receipt": None})
                    return
                with self._paid_curation_attempt_locked(
                        log_name, steward_kind, final, curation_key,
                        provenance, incomplete) as invoke:
                    if not invoke:
                        return
                    try:
                        facets = propose_task_facets(
                            goal, kind, client, parser=_FINALIZE_STEWARD_PARSER,
                            raise_on_failure=True)
                        self._append_curation_once(log_name, final, curation_key, provenance, {
                            "outcome": "proposed" if facets else "empty", "auto": False,
                            "auto_requested": auto_requested,
                            "proposals": {"task_id": tid, "facets": facets}, "receipt": None},
                            require_durable=True)
                    except Exception as exc:  # noqa: BLE001 - close while decision lock is held
                        self._append_curation_once(log_name, final, curation_key, provenance, {
                            "outcome": "error", "error_type": type(exc).__name__, "auto": False,
                            "auto_requested": auto_requested,
                            "proposals": {"task_id": tid, "facets": {}}, "receipt": None},
                            require_durable=True)
        except Exception as exc:  # noqa: BLE001 — agentic faceting must never fail a run
            try:
                self._append_curation_once(
                    log_name, final, diagnostic_key, diagnostic_provenance, {
                    "outcome": "error", "error_type": type(exc).__name__, "auto": False,
                    "auto_requested": auto_requested,
                    "proposals": {"task_id": str(final.task_id or ""), "facets": {}}, "receipt": None},
                    require_durable=True)
            except Exception:  # noqa: BLE001
                pass

    def store_research_claims(self, final: RunState) -> None:
        """PART IV/§21.20 — persist this run's D8 deep-research claims (from the memo ledger) to the
        cross-run `research_claims.jsonl`, so evidence-backed research findings survive their run and can
        support/contest lesson verdicts. Best-effort BUILD; the WRITE reaches finalize's retry handshake."""
        if not self._e.memory_dir:
            return
        try:
            claims = []
            raw_research = getattr(final, "research", None)
            if raw_research is None:
                return
            # CODEX AGENT: a malformed outer memo collection is one UNKNOWN producer slot, not an
            # iterable of trusted memos (a dict/string used to be walked key/character by character).
            memos = raw_research if type(raw_research) in (list, tuple) else (None,)
            # An explicitly observed empty research collection is a complete zero-row D8 snapshot. It must
            # reach the upsert writer so a finalize retry can clear stale understood rows for the same run.
            # By contrast, `None` above and a non-empty collection of pre-D8 memos with no `claims` field do
            # not assert anything about the D8 source and deliberately leave an existing store untouched.
            d8_source_observed = not memos
            for memo in memos:
                if type(memo) is not dict:
                    d8_source_observed = True
                    claims.append(None)
                    continue
                # Old pre-D8 memos legitimately have no `claims` key. Once the field is present, however,
                # only the declared list/tuple shape can prove its cardinality. Any scalar/container mismatch
                # contributes one opaque omitted slot so finalize cannot silently publish a complete receipt.
                if "claims" not in memo:
                    continue
                d8_source_observed = True
                raw_claims = memo.get("claims")
                if type(raw_claims) not in (list, tuple):
                    claims.append(None)
                    continue
                verification = memo.get("verification")
                verification = verification if type(verification) is dict else {}
                verdicts = verification.get("verdicts")
                verdicts = verdicts if type(verdicts) in (list, tuple) else ()
                method_value = verification.get("method")
                method = method_value[:80] if isinstance(method_value, str) else ""
                for i, c in enumerate(raw_claims):
                    # Preserve one slot per producer item. The D8 writer deliberately counts opaque None
                    # markers but never indexes/persists their contents, so an all-invalid memo still emits a
                    # durable incomplete-source sentinel and a malformed prefix cannot shrink the receipt.
                    if type(c) is not dict:
                        claims.append(None)
                        continue
                    try:
                        statement = c.get("statement")
                        if not isinstance(statement, str) or not statement.strip():
                            claims.append(None)
                            continue
                        # `verify_memo` promises an index-aligned verdict list. Fail closed if a malformed
                        # event breaks that alignment or names a different statement: the citation remains
                        # drillable, but it is never upgraded into positive support.
                        v = verdicts[i] if i < len(verdicts) and type(verdicts[i]) is dict else {}
                        verified_statement = v.get("statement")
                        same = (isinstance(verified_statement, str)
                                and verified_statement.strip() == statement.strip())
                        verdict_value = v.get("verdict")
                        verdict = (verdict_value if same and isinstance(verdict_value, str)
                                   else "unverified")
                        note_value = v.get("note")
                        note = (note_value[:400] if same and isinstance(note_value, str)
                                else "verification alignment mismatch")
                        # Forward only the fields the D8 writer understands. Unknown model output remains
                        # untrusted run-local data and cannot hitch a ride into durable cross-run memory.
                        prepared = {
                            "statement": statement,
                            "node_ids": c.get("node_ids"),
                            "urls": c.get("urls"),
                            "verification": {"verdict": verdict, "method": method, "note": note},
                        }
                        for key in ("metric_name", "metric_key", "objective_metric", "metric", "fingerprint"):
                            if key in c:
                                prepared[key] = c.get(key)
                        claims.append(prepared)
                    except Exception:  # noqa: BLE001 - one hostile legacy item is one omitted source slot
                        claims.append(None)
            if not d8_source_observed:
                return
        except Exception:  # noqa: BLE001 — extraction is best-effort, never fails a run
            return
        from looplab.engine.claims import record_research_claims
        record_research_claims(self._e.memory_dir, run_id=final.run_id or final.task_id,
                               task_id=final.task_id, claims=claims,
                               direction=final.direction)
