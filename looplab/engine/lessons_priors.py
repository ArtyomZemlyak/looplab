"""Cross-run prior loading (E4 + M2/M3 read side) for the lessons cluster — extracted from
engine/lessons.py as a MIXIN (the Engine's own convention, see engine/novelty.py):
`class LessonMemory(LessonPriorsMixin, …)` inherits these methods unchanged, so there is ZERO
call-site churn and `self` here IS the LessonMemory — the bodies are verbatim moves, reading the
engine through `self._e` and sibling cluster methods through the Engine's thin delegators,
exactly as they did inside the class.

The READ side of cross-run memory: ONE store scan (`_scan_prior_context`) feeds both per-role
prior renders (`_render_role_prior`), with a per-build memoized embedder (`_memoized_embed`) so
a shared/untagged lesson embeds once, not once per role. The role constants live here with the
renderer that filters on them; lessons.py re-exports them for back-compat.

Layering: like lessons.py, no runtime import of the orchestrator (or lessons.py — the mixin is
consumed there) and never serve — only engine.memory, events, core and stdlib (the retrieval/
ranking deps stay lazy, method-local imports)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from looplab.core.memory_window import read_memory_jsonl_window
from looplab.trust.cross_run import LessonScope, cross_run_text, scope_terms

# Which ROLE a cross-run lesson is for, so the two contexts stay separate (the Researcher gets only
# R&D / "what technique to try" lessons, the Developer only its own "what code change fixed a crash"
# lessons). Stamped on the record at distillation; `load_reflection_priors(role=...)` filters on it.
# An UNTAGGED (legacy) lesson is SHARED — both roles see it — so old stores keep working unchanged.
LESSON_ROLE_RESEARCHER = "researcher"
LESSON_ROLE_DEVELOPER = "developer"

#: How much of a case's PARAMS dict may enter a prompt. It is the one payload a case holds that no
#: neighbouring kind does, so it is bounded generously and clipped from the head with its own
#: receipt (`cross_run_text`) rather than dropped: an incomplete recipe a reader knows is incomplete
#: is usable, one it believes is whole is not. Measured on the only real case in the shared store,
#: the params render is 490 chars over 15 keys.
CASE_PARAMS_CHARS = 1_200

#: VISIBLE characters one lesson statement may occupy in a role prior. Sized from the shared store
#: rather than chosen: statements run 66..382 chars, median 211, so 400 lets every one of them
#: arrive WHOLE and anything longer still truncates honestly with its receipt.
#:
#: THE BUG THIS NUMBER REPLACES, measured 2026-08-23. The render asked for `max_chars=200` — but a
#: cap is a budget for the visible text AND for the truncation receipt, and that receipt is 111
#: characters of sha256. Eighty-nine characters survived. Measured THROUGH THE REAL RENDER against
#: the live store: 19 of 33 statements (58%) reached BOTH roles cut mid-sentence, and 0 do after
#: this change. (A first estimate said 27/82% by comparing RAW statement lengths to the visible
#: budget; that over-counts, because `single_line=True` collapses whitespace before the cap and
#: several statements fit once collapsed. The number that counts is the one the renderer produces.)
#: The one that would have stopped `runs/e5small-dr-unified-v4` nodes 6, 8 and 9 from repeating the
#: same stage failure arrived as "A pipeline stage that short-circuits and skips (re)writing its
#: declared artifact when the [redacted preview: ...]" — the sentence stops exactly before "so every
#: stage must write its declared artifact unconditionally on each run".
#:
#: The whole delivery chain was already correct: role scoping (untagged = shared), task scoping
#: (exact match), the priors block reaching `card_build`/`plan`/`stages`/`inline_repair`, and top-5
#: ranking, which that lesson WON. It was destroyed in the last inch.
LESSON_STATEMENT_CHARS = 400


def _memoized_embed(embed):
    """Wrap an embedder in a per-build content memo. The two role priors (built together at run start
    and each refresh) share every UNTAGGED lesson, so without this each shared lesson is re-embedded
    once per role — the dominant cost when a real semantic embedder is configured. Transparent: the
    same text always maps to the same vector, so per-role retrieval is byte-identical to the
    un-memoized build; it only elides the duplicate embed call."""
    cache: dict[str, object] = {}

    def _memo(text):
        key = cross_run_text(
            text, max_chars=4_000, single_line=True, entropy=True)
        if key not in cache:
            cache[key] = embed(key)
        return cache[key]

    return _memo


class LessonPriorsMixin:
    """The lessons cluster's cross-run prior loader/renderer. See the module docstring for
    the mixin convention (`self` is the LessonMemory)."""

    def load_reflection_priors(self, exclude_run_id: Optional[str] = None,
                               exclude_run_uid: Optional[str] = None,
                               role: Optional[str] = None) -> str:
        """E4 + M2/M3: build the cross-run prior injected into a role's prompt. Two parts:
        (1) exact-task "what won" — the meta-note (meta_notes.jsonl, unchanged E4 warm start) and, since
        2026-08-19, the active CASE beside it (cases.jsonl: the winning run's own parameter dict,
        which the note's prose does not carry), and
        (2) LESSONS retrieved by task-FINGERPRINT similarity (M2), so a *similar but new* task also
        benefits — including NEGATIVE lessons (what was tested/abandoned/failed, M3) so the search
        doesn't re-tread a known dead end. Empty unless enabled + present. `exclude_run_id` drops
        lessons THIS run wrote (M6 mid-run distillation / resume): a run must not read its own
        output back as another run's experience — those results are already in its digest.

        `role` (§role-split): return only the lessons FOR that role — the Researcher gets R&D
        "what technique to try" lessons, the Developer only its own "what code change fixed a crash"
        lessons, so the two contexts stay separate. An UNTAGGED (legacy) lesson is shared. The
        research-flavoured meta-notes (part 1) are skipped for the Developer. role=None -> everything."""
        if not (self._e._reflection_priors and self._e.memory_dir):
            return ""
        return self._render_role_prior(
            self._scan_prior_context(exclude_run_id, exclude_run_uid), role)

    def load_reflection_priors_both(self, exclude_run_id: Optional[str] = None,
                                    exclude_run_uid: Optional[str] = None) -> tuple[str, str]:
        """Build BOTH role priors off ONE scan of the store — the run-start load and every refresh
        need the Researcher AND the Developer prior, and calling `load_reflection_priors` twice would
        re-read + re-fingerprint + re-embed the whole lessons store a second time. Returns
        `(researcher_text, developer_text)`; each is byte-identical to the standalone call."""
        if not (self._e._reflection_priors and self._e.memory_dir):
            return "", ""
        ctx = self._scan_prior_context(exclude_run_id, exclude_run_uid)
        return (self._render_role_prior(ctx, LESSON_ROLE_RESEARCHER),
                self._render_role_prior(ctx, LESSON_ROLE_DEVELOPER))

    def _scan_prior_context(self, exclude_run_id: Optional[str],
                            exclude_run_uid: Optional[str] = None):
        """Read the cross-run stores ONCE for a prior build and return everything the per-role render
        needs: the exact-task meta-notes, the parsed lessons (role-agnostic — filtered per role in
        `_render_role_prior`), the current task fingerprint, and a per-build memoized embedder shared
        across the role renders. Nothing here is role-aware, so both role priors reuse this one scan."""
        base = Path(self._e.memory_dir)
        # Passive prompt injection is a reader just like MemoryTools/CrossRunTools.  It used to have
        # a separate, weaker contract (exact task id manufactured similarity=1, notes ignored polarity,
        # and only lessons honored self-run exclusion).  Build the same fail-closed scope passport once
        # and apply it before either ledger can enter a prompt.
        task = self._e.task
        scope = LessonScope(
            bound=True,
            run_uid=str(exclude_run_uid or ""),
            run_id=str(exclude_run_id or ""),
            task_id=str(getattr(task, "id", "") or ""),
            direction=str(getattr(task, "direction", "") or ""),
            goal_terms=scope_terms(getattr(task, "goal", "") or ""),
        )
        # (1) exact-task meta notes (E4) — research-flavoured "what won" config (rendered for the
        # Researcher only; the Developer render drops them below).
        notes: list[str] = []
        npath = base / "meta_notes.jsonl"
        # These `read_jsonl_lenient` reads RAISE OSError on an unreadable shared store (permissions, a
        # transient FS fault). This helper deliberately does NOT swallow it: `maybe_refresh_lessons`
        # needs the exception so it can disclose the skip AND decline to advance its stamp. Both
        # callers guard it — the mid-run refresh has always, and the RUN-START loader
        # (`orchestrator._reentry_repin`) now does too, which is what stopped an unreadable memory_dir
        # from failing the run during deterministic setup on every start and resume.
        note_rows, note_health = read_memory_jsonl_window(npath)
        scope_filtered = 0
        for _index, o in note_rows:
            if not isinstance(o, dict):
                note_health["skipped"] += 1
                continue
            if o.get("task_id") != self._e.task.id or not o.get("note"):
                continue
            if not scope.allows(o):
                # COUNTED, not silent. `scope.allows` is fail-closed on missing polarity and
                # meta-note writers only started persisting `direction` on 2026-08-13, so on an
                # existing shared store every LEGACY note fails here and the exact-task "what won"
                # tier (E4) goes dark until each task re-finalizes. That direction is deliberate —
                # a note whose polarity is unknown must not be read as agreeing with this run — but
                # a filtered row used to increment nothing at all, so the prior read as "N rows,
                # none relevant" rather than "N rows withheld for missing polarity". The two are
                # very different facts for an operator wondering why the tier is empty.
                scope_filtered += 1
                continue
            notes.append(cross_run_text(
                o["note"], max_chars=1_200, single_line=True, entropy=True))
        # (1b) the exact-task CASE — the same "what won" tier as the notes above, in the form prose
        # cannot carry. THIS LOADER IS THE READER `cases.jsonl` NEVER HAD. `store_case` has written
        # one row per finished run since I19, keyed by exactly the `(task_id, direction)` this scan
        # is already scoped by and gated by exactly the `LessonScope` above — and nothing in
        # `looplab/` ever read it back into a decision or a prompt (`JsonlCaseLibrary.search()` and
        # `.all()` have no production call sites; the file's only reader was `KnowledgeTools`, which
        # embeds it into the `kb` index where it must first win a top-3 semantic ranking against
        # every knowledge note).
        #
        # WHY THE NOTE DOES NOT ALREADY COVER IT, which is the whole argument and is a measurement
        # rather than a preference. The shared store's 30 cases are 29 `toy_quadratic` and ONE real
        # row, and on the toy task the meta-note genuinely IS the case's twin — "best metric 4.483
        # via op 'improve' params {'x': 0.885, 'y': -0.9026}" carries both parameters inline, which
        # is why folding cases into notes looks correct from that corpus. On the one real row it is
        # not: `rubertlite-dr-unified-v8`'s note is a causal narrative ("R-Drop … at alpha=0.5
        # stacked onto the DCL nll_cos loss … lifted recall from 0.7384 to 0.762") naming ONE
        # hyperparameter, while its case carries fifteen — `loss.temperature 0.05`, `loss.thr 0.1`,
        # `train.negatives.mining_type 1`, `train.training.batch_size 8192`,
        # `gradient_accumulation_steps 2`, `learning_rate 0.001`, `max_grad_norm 1.0` — beside
        # `metric 0.762048`. Prose is the CAUSE; the case is the CONFIGURATION, and only one of
        # those can be re-run.
        #
        # Bounded to ONE row on purpose: `_add_locked` already keeps a single `active` winner per
        # (task, direction), so "the best configuration for this task" is a store-level fact and not
        # a ranking this reader gets to make. Inactive rows are the store's own history and stay out.
        # The LAST admitted row wins rather than the first, which matters only on a store that has
        # somehow acquired two active rows for one key: the newest append is the one `_add_locked`
        # would have elected, and picking the oldest would pin a prompt to a superseded winner.
        #
        # An unreadable or absent case store DEGRADES here and does not raise, unlike the two reads
        # around it. Those raise because their callers own a recovery contract (leave the source
        # stamp uncommitted, retry next cadence) that predates this tier; adding a THIRD file to
        # that raise would let an unreadable `cases.jsonl` fail deterministic run setup on every
        # start and resume, for a tier that is additive. A missing ledger is already a healthy empty
        # source (`read_memory_jsonl_window`), so a store that never had cases is unaffected; an
        # unreadable one is folded into the health receipt below and the prior says it is partial.
        from looplab.engine.memory import valid_case_record
        case_line = ""
        case_rows, case_health = read_memory_jsonl_window(base / "cases.jsonl")
        for _index, c in case_rows:
            if not isinstance(c, dict):
                case_health["skipped"] += 1
                continue
            # The writer's own validity fence, so a poisoned or future-schema row cannot enter a
            # prompt through a reader that applies a weaker rule than the store does.
            if not valid_case_record(c) or c.get("active") is False:
                continue
            if c.get("task_id") != self._e.task.id or not isinstance(c.get("params"), dict):
                continue
            if not scope.allows(c):
                scope_filtered += 1
                continue
            case_line = (f"metric {c.get('metric')} (run {c.get('run_id') or 'unknown'}) with params "
                         + cross_run_text(c.get("params"), max_chars=CASE_PARAMS_CHARS,
                                          single_line=True, entropy=True))
        # (2) fingerprint-matched lessons (M2/M3), incl. negatives — parsed once; the role filter and
        # similarity scoring happen per role in `_render_role_prior`.
        parsed: list[tuple[int, dict]] = []
        lpath = base / "lessons.jsonl"
        # `idx` is the row's position in THIS captured window (`core/memory_window.py`), NOT an
        # on-disk line number — the window is a bounded tail, so the same lesson gets a different
        # `idx` after any append. It is only a join key within this one scan (`by_idx`/`already`
        # below) and is never persisted or compared across reads.
        lesson_rows, lesson_health = read_memory_jsonl_window(lpath)
        if note_health["unavailable"] or lesson_health["unavailable"]:
            # Run-start/cadence callers already own a durable unavailable event and deliberately leave
            # their source stamp uncommitted so the next cadence retries. Preserve that recovery
            # contract; partial-but-readable windows continue through with an in-prompt receipt.
            raise OSError("cross-run memory source unavailable")
        for idx, o in lesson_rows:
            if not isinstance(o, dict) or not o.get("statement"):
                if not isinstance(o, dict):
                    lesson_health["skipped"] += 1
                continue
            if not scope.allows(o):
                continue                     # one polarity/task/self-run predicate for every reader
            parsed.append((idx, o))
        # Compare WITHOUT param: tokens: the writer stamps the winner's param names, but at read
        # time no winner exists yet, so those tokens only dilute the Jaccard overlap.
        fp = [t for t in self._e._task_fingerprint(self._e._empty_state_for_fp())
              if not t.startswith("param:")]
        health = {
            "complete": not any(
                row["source_window_truncated"] or row["skipped"] or row["unavailable"]
                for row in (note_health, lesson_health)),
            "invalid": int(note_health["skipped"]) + int(lesson_health["skipped"]),
            "source": int(note_health["source_rows"]) + int(lesson_health["source_rows"]),
            "truncated": bool(note_health["source_window_truncated"]
                              or lesson_health["source_window_truncated"]),
            "unavailable": bool(note_health["unavailable"] or lesson_health["unavailable"]),
            "notes_digest": note_health["window_digest"],
            "lessons_digest": lesson_health["window_digest"],
            # Withheld for an unreadable/absent SCOPE, not for being invalid — a different fact
            # from `invalid`, and the one that explains an empty E4 tier on a legacy store.
            "scope_filtered": int(scope_filtered),
        }
        # The case store joins the health receipt on the SAME terms as the other two — an unreadable
        # or bounded case window must not read as "this task has no winning configuration".
        health["invalid"] += int(case_health["skipped"])
        health["source"] += int(case_health["source_rows"])
        health["truncated"] = bool(health["truncated"] or case_health["source_window_truncated"])
        health["complete"] = bool(health["complete"] and not (
            case_health["source_window_truncated"] or case_health["skipped"]
            or case_health["unavailable"]))
        return notes, parsed, fp, _memoized_embed(self._e._embedder), health, case_line

    def _render_role_prior(self, ctx, role: Optional[str]) -> str:
        """Render ONE role's prior text from a shared `_scan_prior_context` scan: filter the parsed
        lessons to that role (untagged = shared), score by fingerprint similarity, splice in Memora
        harmonic recall, apply D2 read-time hygiene + ranking, and pick the top 5 with a role label."""
        from looplab.engine.memory import prompt_slot_key      # both slot budgets below key on it
        notes, parsed, fp, embed, health, case_line = ctx
        out = (f"\n[MEMORY_SOURCE: canonical recent snapshot; rows={health['source']}; "
               f"notes_sha256={health['notes_digest']}; lessons_sha256={health['lessons_digest']}; "
               f"complete={'true' if health['complete'] else 'false'}.]"
               + (("\n[MEMORY_SOURCE_PARTIAL: "
                   f"unreadable={health['invalid']}; truncated={'true' if health['truncated'] else 'false'}; "
                   f"unavailable={'true' if health['unavailable'] else 'false'}; "
                   "retained priors are incomplete.]") if not health["complete"] else "")
               + (f"\n[MEMORY_SCOPE_WITHHELD: {health['scope_filtered']} exact-task note(s) record "
                  "no optimization direction, so their polarity cannot be established and they are "
                  "not shown.]" if health.get("scope_filtered") else ""))
        # (1) meta-notes — research-flavoured, so the Developer never sees them.
        # DE-DUPE FIRST, then take the last 3. These notes are a `write_reflection_note` f-string
        # ("best metric {m} via op '{op}' params {p}; N nodes, M evaluated"), and `meta_notes.jsonl`
        # has no consolidation pass at all (unlike lessons.jsonl, which at least gets
        # `consolidate_lessons_file` + `compact_lessons`) — its only de-dup is the per-(run_id,
        # finish_seq) crash-retry guard in `write_reflection_note`, which by construction cannot see
        # a DIFFERENT run that landed on the same winner. So the tail repeats: measured on the
        # shared store, 140 notes carry 65 distinct texts, and for `toy_quadratic` two of these
        # three slots were BYTE-IDENTICAL. Keyed on `prompt_slot_key`, so re-running the same task
        # to the same optimum with cosmetically different digits also stops eating the budget.
        # LATEST occurrence wins (scan reversed, then restore order) — recency is what the `[-3:]`
        # tail was always selecting for, so a repeat promotes its newest copy rather than resurrect
        # an old one. Nothing is dropped from the store; this only picks which notes fill 3 slots.
        if notes and role != LESSON_ROLE_DEVELOPER:
            _seen_notes: set[str] = set()
            _distinct: list[str] = []
            for _n in reversed(notes):
                _k = prompt_slot_key(_n, cap=200)
                if _k in _seen_notes:
                    continue
                _seen_notes.add(_k)
                _distinct.append(_n)
            _distinct.reverse()
            out += "\nPrior-run insights for this task (meta-learned): " + " | ".join(_distinct[-3:])
        # The CASE rides the same tier and the same role gate as the notes above — it is the same
        # "what won on this exact task" fact, in the form a next run can act on. One line, after the
        # prose, because the prose says WHY it won and this says WHAT to set.
        if case_line and role != LESSON_ROLE_DEVELOPER:
            out += ("\nBest known configuration for this task (the winning run's own parameters, "
                    "not a recommendation): " + case_line)
        if not parsed:
            return cross_run_text(
                out, max_chars=8_000, single_line=False, entropy=True)
        # (2) fingerprint-matched lessons (M2/M3), incl. negatives
        from looplab.engine.memory import fingerprint_similarity
        all_lessons: list[tuple[int, dict]] = []
        scored: list[tuple[float, int, dict]] = []
        for idx, o in parsed:
            lrole = o.get("role")
            if role is not None and lrole is not None and lrole != role:
                continue                     # §role-split: a lesson EXPLICITLY for the OTHER role
                #                              stays out of this role's context (untagged = shared)
            all_lessons.append((idx, o))
            stored_fp = o.get("fingerprint")
            stored_fp = ([t for t in stored_fp if not str(t).startswith("param:")]
                         if isinstance(stored_fp, list) else [])
            exact = o.get("task_id") == self._e.task.id
            sim = 1.0 if exact else fingerprint_similarity(fp, stored_fp)
            if sim >= 0.34:                    # a related task (Jaccard) or the same one
                scored.append((sim, idx, o))
        # Full synergy with Memora: harmonic recall reaches lessons a differently-worded but
        # anchor-linked task shares — the ones token-overlap (Jaccard ≥ 0.34) misses. Splice
        # them into the SAME candidate pool so the D2 hygiene + ranking below apply uniformly.
        # No-op unless a Memora abstractor is wired (memora on); then it uses the T5 embedder.
        if self._e._lesson_abstractor is not None and all_lessons:
            from looplab.engine.memory import retrieve_lessons_harmonic
            from looplab.tools.vectorstore import hash_embed
            by_idx = {i: o for i, o in all_lessons}
            already = {i for _, i, _ in scored}
            query = " ".join(fp) + " " + (getattr(self._e.task, "goal", "") or "")
            # Hash buckets are deterministic offline test machinery, not semantic evidence.  On the
            # shipped no-embed-model default they admitted unrelated harmonic rows at high cosine and
            # saturated the five-slot prior.  Retain lexical/Jaccard retrieval, but require a real
            # configured embedder before harmonic expansion can add a row the lexical gate rejected.
            if self._e._embedder is not hash_embed:
                for hsim, hidx in retrieve_lessons_harmonic(
                        all_lessons, query, self._e._lesson_abstractor, embed):
                    if hidx not in already and hidx in by_idx:
                        scored.append((hsim, hidx, by_idx[hidx]))
                        already.add(hidx)
        # D2 hygiene at read time: quarantine any lesson whose claim a NEWER run reversed
        # (an old "supported" vs a later "tested/abandoned" of the same statement) — the
        # misevolution guard: memory must not keep pushing a refuted correlation.
        from looplab.engine.memory import filter_contradicted, lesson_rank_key
        scored = filter_contradicted(scored)
        # Rank: similarity, then confidence × corroboration (evidence_count), then recency —
        # so a twice-confirmed lesson from a related task beats a one-off at equal similarity.
        scored.sort(key=lambda t: lesson_rank_key(*t))
        seen: set[tuple] = set()
        picked: list[str] = []
        for _, _, o in scored:
            # SLOT identity, not claim identity: `prompt_slot_key` collapses numbers, so N rows of
            # one f-string template ("changing x A->B regressed the metric by D") spend ONE of the
            # five slots instead of five. The old `statement[:80]` key never fired on them — the
            # digits sit well inside 80 chars — so a single template family could fill the whole
            # prior: measured on the shared store, 5/5 slots for `toy_quadratic` and 7/42 (17%)
            # across every task. `scored` is already ranked (similarity, then confidence ×
            # corroboration, then recency), so the row that keeps the slot is the family's BEST
            # one and it is still rendered with its digits intact below. Nothing is dropped from
            # the store — `consolidate_lessons` alone owns what MERGES, and it must not fold these
            # (they are distinct measurements); this only rations prompt space.
            key = (prompt_slot_key(o.get("statement", "")), o.get("outcome"))
            if key in seen:
                continue
            seen.add(key)
            d = o.get("delta")
            dtxt = f" Δ{d:+.3g}" if isinstance(d, (int, float)) else ""
            # Ask for the visible budget PLUS what the receipt costs, derived from the text rather
            # than guessed — the receipt's length tracks the digit count of `original_chars`, so a
            # literal "+111" is correct only until a statement crosses a power of ten. Redaction is
            # untouched: `entropy=True` here and the block-level pass at the end of this method are
            # defence in depth, and a bare high-entropy token is masked by EITHER of them.
            from looplab.core.redact import truncation_receipt_chars
            stmt = cross_run_text(
                o["statement"],
                max_chars=LESSON_STATEMENT_CHARS + truncation_receipt_chars(o["statement"]),
                single_line=True, entropy=True).strip()
            outcome = cross_run_text(
                o.get("outcome", "?"), max_chars=40, single_line=True, entropy=True).strip()
            picked.append(f"{stmt} [{outcome}{dtxt}]")   # store is shared/free-text
            if len(picked) >= 5:
                break
        if picked:
            # The Developer label must NOT over-claim "code fixes": its pool is its own code-fix
            # lessons PLUS any untagged/shared rows (winner records, failure themes) that are not code
            # fixes — so the header names both instead of asserting everything is a fix.
            label = ("Implementation & shared lessons from related runs (code fixes and prior "
                     "findings that did/didn't work)"
                     if role == LESSON_ROLE_DEVELOPER
                     else "Lessons from related runs (what did/didn't work)")
            out += "\n" + label + ": " + "; ".join(picked)
        return cross_run_text(
            out, max_chars=8_000, single_line=False, entropy=True)
