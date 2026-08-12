// What an experiment TAUGHT — a pure model, no React and no I/O, so `node --test` can drive it.
//
// ─── why this is not a list of lessons ────────────────────────────────────────────────────────────
//
// "Show the lessons derived from this experiment" sounds like a join. It is not one, because the two
// things being joined have different physics and the code says so:
//
//   A. The RUN'S OWN EVENT LOG is immutable. `state.lessons_distilled` is folded from an append-only
//      `events.jsonl` and each entry carries `lessons[].evidence_refs = [{node_id, generation}]` plus
//      a content-addressed `lesson_id` (`core/advisory_payloads.py::research_lesson_ref`). It answers
//      "what did this experiment produce, at the time" EXACTLY, and it can never go stale — history
//      does not change.
//
//   B. The CROSS-RUN STORE is mutable and consolidating. The survivor carries bounded
//      `evidence_refs[{run_uid,run_id,node_id,generation}]` plus traceable/total counts, while its
//      statement and verdict still come from one current base row. `_agentic_merge_lessons` can replace
//      the statement with LLM-written text. Superseded rows are then physically deleted by
//      `replace_jsonl_rows_atomic_preserving_quarantine`; the refs preserve evidence lineage, not a
//      redirect between old and synthesized statements. `compact_lessons`
//      additionally drops the oldest prefix past 4000 lines with no record at all, and
//      `lessons_reconcile.py` RETIRES a run's lessons outright when a re-eval flips their outcome.
//
// So the three tempting designs are all wrong, and each is wrong in a different way:
//
//   ✗ Snapshot the lesson text at finalize time and show that.  Shows text that no longer exists the
//     moment anything merges. This is precisely what the owner ruled out.
//   ✗ Look the lesson up in the live store and show whatever came back.  After a merge that took its
//     base from another run, nothing comes back — and rendering an empty section says "this
//     experiment taught us nothing", which the event log disproves.
//   ✗ Follow the merge to its descendant.  Not possible. There is no redirect to follow, and the
//     descendant's statement may be a sentence an LLM wrote that no run ever produced.
//
// What IS honest is to show the immutable record and STATE its current standing in memory. That is
// what `nodeLessons` returns: history as the spine, the live store as a per-row status, and
// `absorbed` for the case we genuinely cannot distinguish (merged into another row / compacted away /
// retired by reconciliation). Naming the ambiguity is the finding, not a placeholder for a better id.
//
// ─── fetch-on-open vs fold-time projection ────────────────────────────────────────────────────────
//
// Half A needs NO fetch: `lessons_distilled` is already on `GET /api/runs/{id}/state` (verified
// against runs/live-cards-0804), so the exact half costs zero requests and updates with the run poll.
// Half B MUST be fetched at read time — a fold-time projection of a mutable file that lives OUTSIDE
// the event log would be a snapshot, and `fold` may not do I/O anyway (engine invariant 5). The cost
// is one `GET /api/memory?run_id=…` per run, which the caller fetches lazily and caches; the
// `run_id` filter exists so this is not the unfiltered whole-store scan the Memory panel pays.
//
// ─── what CANNOT be done today, honestly ──────────────────────────────────────────────────────────
//
//   • Run-end REFLECT lessons (the whole-run distillation) ride on `EV_REFLECTION_NOTE`, which is a
//     DIAGNOSTIC event — not folded, not on the wire, and its projection carries neither `lesson_id`
//     nor `evidence`. For a typical run those are the majority of lessons. They are reachable at
//     RUN level only, which is why `runMemory` below is a separate, weaker answer.
//   • Cases carry no node id at all — only `task_id` and (for new rows) `run_id`.
//   • Meta-notes carry no node id, and legacy/off-finalize-path notes carry no `run_id` either.
//   • Modern Knowledge notes written by `remember` carry actor/surface/time metadata, but no run/node
//     source ref. Legacy Markdown has no provenance at all. Neither is joined to an experiment here.

const record = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const list = value => Array.isArray(value) ? value : []
const text = value => typeof value === 'string' && value.trim() ? value.trim() : null

// `engine/lesson_hygiene.py:51-53` — `" ".join(str(s).split()).lower()[:160]`. This IS the store's own
// grouping identity (`lesson_hygiene.py:111` keys on it), so matching history against the live store
// on the same normalization is matching on the store's terms rather than inventing a second rule.
// A merged row whose statement an LLM rewrote will not match, and that is the correct outcome: it is
// no longer the sentence this experiment produced.
export const normalizeStatement = value =>
  String(value ?? '').split(/\s+/).filter(Boolean).join(' ').toLowerCase().slice(0, 160)

/**
 * Every lesson this run's event log credits to `nodeId` at `attempt`, with its live-store standing.
 *
 * The generation fence is not optional. `evidence_refs` carries `{node_id, generation}` because a
 * `node_reset` re-runs the same numeric id as a NEW attempt, and a lesson drawn from attempt 0 is not
 * a lesson about attempt 1 — `engine/lessons_reconcile.py` retires exactly those rows for that reason.
 * Matching on node id alone would let a re-run node inherit its previous life's conclusions.
 *
 * `storeStatus` per row:
 *   • `present`  — a live row in this run's memory carries the same normalized statement.
 *   • `absorbed` — it does not. Merged into another row, compacted away, or retired by reconciliation;
 *                  the store keeps no record that distinguishes those, so neither do we.
 *   • `unknown`  — the live store was not loaded, or its recent-tail window was truncated so absence
 *                  is not evidence of absence. NEVER collapse this into `absorbed`.
 */
export function nodeLessons(state, nodeId, attempt, store = null) {
  const id = Number(nodeId)
  if (!Number.isSafeInteger(id)) return []
  const live = storeIndex(store)
  const seen = new Set()
  const out = []
  for (const batch of list(state?.lessons_distilled)) {
    if (!record(batch)) continue
    for (const lesson of list(batch.lessons)) {
      if (!record(lesson)) continue
      const refs = list(lesson.evidence_refs).filter(record)
      // A row with no `evidence_refs` at all is pre-fence history: `research_lesson_receipt` omits
      // them (and the id) whenever any credited node was tombstoned/aborted/missing at write time.
      // It cannot be attributed to a node, so it is left to the run-level view rather than guessed at.
      const credited = refs.some(ref => Number(ref.node_id) === id
        && (attempt == null || !Number.isSafeInteger(ref.generation) || ref.generation === attempt))
      if (!credited) continue
      const statement = text(lesson.statement)
      if (!statement) continue
      const key = `${text(lesson.lesson_id) || ''}|${normalizeStatement(statement)}`
      if (seen.has(key)) continue     // the same lesson can be re-emitted by a later cadence batch
      seen.add(key)
      out.push({
        statement,
        outcome: text(lesson.outcome),
        claimStance: text(lesson.claim_stance),
        lessonId: text(lesson.lesson_id),
        atNode: Number.isSafeInteger(batch.at_node) ? batch.at_node : null,
        trigger: text(batch.trigger),
        // Everything else this lesson was drawn from. The operator's real question after "what did
        // this teach us" is "compared to what", and a comparative lesson's evidence IS the pair.
        alsoFrom: refs.map(ref => Number(ref.node_id))
          .filter(value => Number.isSafeInteger(value) && value !== id),
        storeStatus: live === null ? 'unknown'
          : live.rows.has(normalizeStatement(statement)) ? 'present'
            : live.truncated ? 'unknown' : 'absorbed',
      })
    }
  }
  return out
}

/**
 * The other direction: LIVE cross-run lessons that still credit this experiment.
 *
 * This is not a duplicate of `nodeLessons` and the difference is the whole reason both exist.
 * `nodeLessons` walks history forward and asks "is what this experiment produced still there".
 * This walks the live store backward and asks "what does memory still say, citing this node" — and
 * after a consolidation those are different sets. Measured across the 46 runs in `runs/` on
 * 2026-08-06: of 16 node-attributed lessons in the event logs, 15 were no longer present as written,
 * while the surviving CONSOLIDATED rows still carried `evidence` naming those same nodes. So without
 * this lane the section is almost entirely "no longer in memory", which is true and useless; with it,
 * the operator sees the sentence memory actually holds about their experiment.
 *
 * `attemptMatch` is reported, never enforced. `evidence_generations` is absent on rows written before
 * `evidence_sig`, and — more often — on a consolidated survivor whose base came from a run that
 * recorded no signature for this id. Dropping those would hide real lessons; silently showing them as
 * attempt-matched would repeat the bug the fence exists to stop. So: `'exact' | 'other' | 'unrecorded'`,
 * with `'other'` filtered out (it is provably a different lifecycle) and `'unrecorded'` kept and marked.
 */
export function liveLessonsForNode(store, nodeId, attempt, runId = '') {
  const id = Number(nodeId)
  if (!record(store) || !Number.isSafeInteger(id)) return []
  const out = []
  for (const row of list(store.lessons)) {
    if (!record(row)) continue
    const lineage = list(row.evidence_refs).filter(ref => record(ref)
      && Number.isSafeInteger(ref.node_id)
      && (!runId || ref.run_id === runId || ref.run_uid === runId))
    // The survivor's legacy `evidence` ids are local to its own run.  Never reinterpret them inside
    // another run merely because both runs happened to allocate node 3.
    const localEvidence = !runId || row.run_id === runId
      ? list(row.evidence).filter(value => Number.isSafeInteger(value)) : []
    const evidence = lineage.length
      ? lineage.map(ref => ref.node_id)
      : localEvidence
    if (!evidence.includes(id)) continue
    const lineageRef = lineage.find(ref => ref.node_id === id)
    const recorded = Number.isSafeInteger(lineageRef?.generation) ? lineageRef.generation
      : (!runId || row.run_id === runId) && record(row.evidence_generations)
        ? row.evidence_generations[String(id)] : undefined
    const attemptMatch = !Number.isSafeInteger(recorded) ? 'unrecorded'
      : attempt == null || recorded === attempt ? 'exact' : 'other'
    if (attemptMatch === 'other') continue
    const statement = text(row.statement)
    if (!statement) continue
    out.push({
      statement,
      outcome: text(row.outcome),
      claimStance: text(row.claim_stance),
      attemptMatch,
      // A bounded lineage can still omit legacy or capped sources; show the durable denominator beside
      // the refs rather than turning a retained subset into an exact claim.
      evidenceCount: Number.isSafeInteger(row.evidence_count) ? row.evidence_count : null,
      evidenceTraceableCount: Number.isSafeInteger(row.evidence_traceable_count)
        ? row.evidence_traceable_count : null,
      alsoFrom: evidence.filter(value => value !== id),
      concepts: list(row.concepts).filter(value => typeof value === 'string'),
    })
  }
  return out
}

/**
 * The run-level tiers, which is all that exists for notes and cases — and for the run-end reflect
 * lessons that never reach `lessons_distilled`.
 *
 * `unattributed` is the honest residue and is reported rather than hidden: live lessons this run
 * owns that NO event-log entry credits to any node. They are real (reflect lessons, and any row whose
 * evidence was already stale when the receipt was written), and dropping them would make the
 * per-node view look complete when it is not.
 */
export function runMemory(state, store) {
  const live = storeIndex(store)
  if (live === null) return null
  const attributed = new Set()
  for (const batch of list(state?.lessons_distilled)) {
    for (const lesson of list(batch?.lessons)) {
      if (!record(lesson) || !list(lesson.evidence_refs).length) continue
      const statement = text(lesson.statement)
      if (statement) attributed.add(normalizeStatement(statement))
    }
  }
  const lessons = list(store?.lessons).filter(record)
  return {
    lessons,
    notes: list(store?.notes).filter(record),
    cases: list(store?.cases).filter(record),
    unattributed: lessons.filter(row => !attributed.has(normalizeStatement(row.statement))),
    truncated: live.truncated,
  }
}

/**
 * Whether the live store can be trusted to answer "is this lesson still here".
 *
 * `null` means not loaded. `truncated` folds every reason absence might be uninformative — the source
 * window was cut, rows were dropped as unreadable, or the tier could not be read at all — into the one
 * question a caller actually asks. Note that `filtered` is deliberately NOT one of them: rows excluded
 * by the run filter are rows about other runs, and their exclusion says nothing about this run's.
 */
function storeIndex(store) {
  if (!record(store)) return null
  const rows = new Set()
  for (const row of list(store.lessons)) {
    const statement = record(row) ? text(row.statement) : null
    if (statement) rows.add(normalizeStatement(statement))
  }
  const tier = store.page?.tiers?.lessons
  return {
    rows,
    // Both spellings on purpose: this reads the RAW `/api/memory` body (`source_window_truncated`),
    // and `panels.jsx::memoryPayload` normalizes the same receipt to `sourceWindowTruncated` for the
    // Memory panel. Accepting either is what lets one model serve both without a second validator —
    // and the fail-closed default (`!record(tier)` ⇒ truncated) means a body without a receipt can
    // never be read as an authoritative "gone".
    truncated: !record(tier) || tier.unavailable === true
      || tier.source_window_truncated === true || tier.sourceWindowTruncated === true
      || (tier.skipped || 0) > 0,
  }
}
