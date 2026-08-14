// Pure model for the CROSS-RUN metric overlay — the `Same-task run observations` panel's decisions.
// No React, no I/O; unit-tested with `node --test` (`ui/test/crossRunRank.test.js`).
//
// WHY THIS EXISTS, AND WHY THE PANEL WAS RIGHT TO REFUSE FIRST.
// `panels.jsx::CrossRunPanel` printed a flat list of same-task metric observations under the sentence
// "Cross-run ranking unavailable … values below remain per-run observations". That refusal was CORRECT
// and is NOT deleted here: an operator with 45 runs on one box cannot see which configuration won, but
// the cure for that is not a leaderboard over everything — it is a ranking over the exact subsets where
// a ranking is a fact, with every subset that fails the test still on screen, named, and unranked.
//
// A statement about which run WON is a RECORD-side claim in the sense of
// `docs/36-agent-driven-decisions-2026-08-13.md`: the operator acts on it (they go and reuse that
// config), and a wrong answer is not recoverable because it becomes the result. So every rung below is
// deterministic over fields the run row already carries, and every one of them is visible beside the
// number it produced. Nothing here is smoothed, weighted, imputed or normalized into a score.
//
// MEASURED ON THIS BOX (2026-08-14, 45 run directories under `runs/`, folded through
// `events/replay.py::fold` — the same fold `/api/runs` serves):
//   - 36 of 45 runs carry any metric at all; 9 have none (setup-only, dependency probes, the live
//     `rubertlite-dr-unified-v7`).
//   - Those 36 fall into 20 distinct (task_id, direction) groups. FIFTEEN of them hold ONE run. Only
//     5 groups hold two or more: toy_quadratic/min (9), dataset_task/min (4), repo_task/max (3),
//     dataset_example/max (3), blob_classification/max (2) — 21 runs, 46.7 % of the corpus.
//   - `dataset_task` really does appear with BOTH directions (4 runs minimize, `mnist-experiments`
//     maximizes). Grouping on task_id alone would have put a minimize and a maximize objective in one
//     ordered column. That is why the group key is the PAIR, and why `metricComparable` — which tests
//     exactly "one task, one direction" — is the gate rather than a fresh predicate written here.
//   - TIES ARE THE MAJORITY CASE, not an edge case: 10 of those 21 runs share first place with at
//     least one other run. `dataset_example` is 3 runs at exactly 1.0, `blob_classification` 2 at
//     exactly 0.925, `toy_quadratic` 5 at exactly 0.0. A plain sort would have printed a 1st, a 2nd
//     and a 3rd over three identical numbers — a winner invented by array order. Ranks here are
//     COMPETITION ranks (1, 1, 1, 4) and tied rows are ordered for display by run id, so nothing about
//     the row order can be read as a margin.
//   - `best_confirmed` is null on all 36. Every value in this corpus is a single best observation, not
//     a repeated measurement — so the panel says which kind each number is instead of implying both
//     are the same evidence.
//
// WHAT THE INTEGRITY RUNG IS FOR, and it is not hypothetical. The only group of real GPU work,
// `repo_task/max`, would be won by `rubertlite-dense-retrieval` at 0.8077 — a number folded from 20 of
// 1,624 records, because that log stops being readable at line 21 (`source_integrity.complete: false`;
// see `runIndex.js::sourceIncomplete`). Ranking it would crown a run on 1.2 % of its own evidence. A
// run whose source is incomplete therefore stays IN its group and IN the table, with its value shown,
// and holds NO rank. On this corpus that is exactly 1 run, and it changes who leads `repo_task`.
//
// WHAT THIS MODEL STILL CANNOT SEE, which is why the panel keeps a refusal.
//   1. `/api/runs` carries no metric NAME, unit, dataset identity or evaluation protocol — the row is
//      `{task_id, direction, best_metric, best_confirmed, nodes, phase, …}` and nothing more
//      (`serve/run_projections.py::run_summaries`). A shared task_id is an operational lookup key. Two
//      runs of `repo_task` may have optimized recall@100 against different corpora.
//   2. Nothing in the row says the number is ABOUT that run's own artifact.
//      `docs/35-metric-provenance-core-options-2026-08-13.md` measured this on the same three runs
//      this panel ranks: `rubertlite-dr-unified-v2` node 4 trained to 0.737488 and RECORDED 0.224975 —
//      a July checkpoint of a human's, reached through an absolute path — and v6 node 4 recorded the
//      identical 0.224975 three weeks later. 82 of 83 metrics in the corpus carry no provenance at all.
//      So this ranking orders RECORDED objective values. It does not certify what they measured, and
//      `groupClaim` says so in those words rather than in a hedge.
//
// WHY NOT THE OTHER TWO OVERLAYS THAT WERE ON THE TABLE.
//   - A NORMALIZED cross-group score (percentile / z-score) so all 5 groups could share one axis. It
//     dies on this corpus's own numbers: 3 of the 5 groups have n <= 3, and two of them have ZERO
//     variance (3x 1.0; 2x 0.925), so the z denominator is 0 and the percentile is 100th for everyone.
//     It would have printed a confident position for a group that contains no ordering information at
//     all, and — the deciding objection — the operator cannot check it. A rank of "1 of 3, values
//     0.8077 / 0.7280 / 0.2250" is verifiable by looking; "z = +1.15" is not.
//   - A per-run metric TRAJECTORY overlay (node index vs metric), comparable in shape if not in scale.
//     The run row carries `nodes` as a COUNT and no series, so this needs one `/api/runs/{id}/state`
//     per run — 45 folds on the request thread for a panel that opens on a click, against a server
//     whose live runs re-fold on every poll. And drawing 5 objectives on one axis needs a per-run
//     rescale, which is the normalized-score objection again wearing a line chart. Worth building when
//     the summary row carries the series; the shape of `groupTrajectoryGap` below is left as the note.
import { metricComparable, sourceIncomplete } from './runIndex.js'

// Render backstop. `run_summaries` folds every run directory on the box, and the panel is a table.
const MAX_GROUP_ROWS = 100

const isRecord = value => !!value && typeof value === 'object' && !Array.isArray(value)
const finiteOrNull = value => (typeof value === 'number' && Number.isFinite(value) ? value : null)

// The ONE place the panel reads a run's metric. `best_confirmed ?? best_metric` mirrors
// `runIndex.js::sortRuns` and `RegistryPanel` so the three surfaces cannot drift onto two different
// numbers for the same row; `confirmed` rides along because a repeated-measurement mean and a single
// best are different evidence and the table has to say which one it is showing.
export function runMetric(run) {
  if (!isRecord(run)) return null
  const confirmed = finiteOrNull(run.best_confirmed)
  if (confirmed != null) return { value: confirmed, confirmed: true }
  const best = finiteOrNull(run.best_metric)
  return best == null ? null : { value: best, confirmed: false }
}

const better = (a, b, direction) => (direction === 'min' ? a < b : a > b)

// GROUP THE CORPUS. Returns every (task_id, direction) partition that `metricComparable` accepts,
// plus the runs no partition could take, plus the totals that let a caller state its own coverage.
//
// The partition is deliberately built and then RE-TESTED with `metricComparable` instead of being
// trusted because it was keyed on the same two fields. That keeps one definition of "these runs may be
// compared" in the tree: the day the server ships a metric name and `metricComparable` starts
// requiring it, this panel tightens on the same commit, with no second predicate to remember.
export function crossRunGroups(runs = [], { limit = MAX_GROUP_ROWS } = {}) {
  const rows = Array.isArray(runs) ? runs.filter(isRecord) : []
  const buckets = new Map()
  const noMetric = []
  const unidentified = []          // a metric, but no task id or no min/max direction to read it with
  for (const run of rows) {
    const metric = runMetric(run)
    if (!metric) { noMetric.push(run); continue }
    const taskId = typeof run.task_id === 'string' ? run.task_id.trim() : ''
    const direction = run.direction
    // An ABSENT identity is not a shared identity — the same rule the panel already applied to its
    // own run. Legacy rows that all encode "missing" as '' are not a group of like objectives.
    if (!taskId || !['min', 'max'].includes(direction)) { unidentified.push(run); continue }
    // NUL-separated: a task id is an opaque operator string and may contain spaces, and a key two
    // different (task, direction) pairs could both spell is a key that merges two objectives.
    const key = `${taskId} ${direction}`
    if (!buckets.has(key)) buckets.set(key, { key, taskId, direction, members: [] })
    buckets.get(key).members.push({ run, ...metric })
  }

  const groups = []
  for (const bucket of buckets.values()) {
    if (!metricComparable(bucket.members.map(entry => entry.run))) {
      // Unreachable by construction today; kept because the guard above is the POINT — if the shared
      // predicate ever rejects this partition, the rows go to the unranked bucket rather than through.
      unidentified.push(...bucket.members.map(entry => entry.run))
      continue
    }
    groups.push(buildGroup(bucket, limit))
  }
  // Deterministic order, largest first: a caller iterating this must not get a different page because
  // the server listed its directories in a different order.
  groups.sort((a, b) => b.size - a.size || a.taskId.localeCompare(b.taskId)
    || a.direction.localeCompare(b.direction))

  const comparable = groups.filter(group => group.size > 1)
  return {
    groups,
    comparable,
    singletons: groups.filter(group => group.size === 1),
    noMetric,
    unidentified,
    totals: {
      runs: rows.length,
      withMetric: rows.length - noMetric.length,
      noMetric: noMetric.length,
      unidentified: unidentified.length,
      groups: groups.length,
      comparableGroups: comparable.length,
      // Runs that actually sit in a group where a ranking is possible. This is the number the panel
      // reports as coverage, and on this corpus it is 21 of 45.
      comparableRuns: comparable.reduce((sum, group) => sum + group.size, 0),
      rankedRuns: comparable.reduce((sum, group) => sum + group.ranked, 0),
      integrityExcluded: groups.reduce((sum, group) => sum + group.integrityExcluded, 0),
    },
  }
}

function buildGroup(bucket, limit) {
  const direction = bucket.direction
  // Eligibility to HOLD A RANK is one rung and it is deterministic: a fold that did not see the whole
  // log describes a prefix, and a prefix's best is not this run's best. It keeps its row and its
  // value — hiding it would be a different lie — and it is counted where the group states its size.
  const entries = bucket.members.map(entry => ({
    run: entry.run,
    runId: String(entry.run.run_id),
    label: typeof entry.run.label === 'string' && entry.run.label ? entry.run.label
      : String(entry.run.run_id),
    value: entry.value,
    confirmed: entry.confirmed,
    nodes: Number.isSafeInteger(entry.run.nodes) ? entry.run.nodes : null,
    phase: typeof entry.run.phase === 'string' ? entry.run.phase : '',
    // A run still searching has a best-SO-FAR. That is complete evidence about a shorter search, not a
    // truncated view of a longer one, so it ranks — but it is marked, because it is the one number on
    // the table that can still move.
    provisional: entry.run.finished !== true,
    sourceIncomplete: sourceIncomplete(entry.run),
  }))
  const eligible = entries.filter(entry => !entry.sourceIncomplete)
  const values = [...new Set(eligible.map(entry => entry.value))]
  const bestValue = values.length
    ? values.reduce((best, value) => (better(value, best, direction) ? value : best))
    : null

  // COMPETITION RANK over eligible rows. Equal values share a rank and the next rank skips — the only
  // rank arithmetic that cannot manufacture an order between two identical numbers.
  const ordered = [...eligible].sort((a, b) => (a.value === b.value
    ? a.runId.localeCompare(b.runId)                      // display order only; the ranks stay equal
    : (better(a.value, b.value, direction) ? -1 : 1)))
  let rank = 0
  let seen = 0
  let previous = null
  for (const entry of ordered) {
    seen += 1
    if (previous == null || entry.value !== previous) { rank = seen; previous = entry.value }
    entry.rank = rank
  }
  // How many OTHER runs share this row's rank. The render prints it as "(tie)" and the count in a
  // tooltip: a rank number alone still reads as a placing, and on this corpus 10 of the 21 rankable
  // runs are in a shared first place.
  const shared = new Map()
  for (const entry of ordered) shared.set(entry.rank, (shared.get(entry.rank) || 0) + 1)
  for (const entry of ordered) entry.tied = shared.get(entry.rank) - 1
  const leaders = ordered.filter(entry => entry.rank === 1)
  const unranked = entries.filter(entry => entry.sourceIncomplete)
    .sort((a, b) => a.runId.localeCompare(b.runId))
  for (const entry of unranked) { entry.rank = null; entry.tied = 0 }

  // What this group IS, in one word the render can switch on. `tied` is not a degenerate 'ranked': a
  // group whose eligible runs all recorded the same number contains no ordering information, and
  // saying "3-way tie at 1.0" is the whole finding.
  const outcome = eligible.length === 0 ? 'none'
    : eligible.length === 1 ? 'single'
      : values.length === 1 ? 'tied' : 'ranked'
  const shown = ordered.slice(0, limit)
  return {
    key: bucket.key,
    taskId: bucket.taskId,
    direction,
    size: entries.length,
    ranked: ordered.length,
    integrityExcluded: unranked.length,
    provisionalCount: entries.filter(entry => entry.provisional).length,
    confirmedCount: entries.filter(entry => entry.confirmed).length,
    distinctValues: values.length,
    bestValue,
    leaders: leaders.map(entry => entry.runId),
    outcome,
    rows: shown,
    unranked,
    omitted: Math.max(0, ordered.length - shown.length),
  }
}

// THE SENTENCE THE PANEL PRINTS. It lives here, not in JSX, for the same reason
// `runIndex.js::sourceIntegrityNotice` and `conceptForest.js::conceptScopeClaim` do: the wording IS the
// claim, and a claim a test cannot drive is a claim nobody is holding to its evidence.
//
// `claim` is what the panel asserts; `refusals` are what it explicitly does not, and they are returned
// as a list rather than folded into prose so the render cannot quietly drop one to save a line.
export function groupClaim(group) {
  if (!group) return null
  const objective = group.direction === 'min' ? 'lowest' : 'highest'
  const scope = `${group.size} run${group.size === 1 ? '' : 's'} of task ${group.taskId}`
  const claim = group.outcome === 'none'
    ? `No run of ${group.taskId} can hold a rank: every one of these ${group.size} has an incomplete `
      + 'event log, so each value describes a readable prefix.'
    : group.outcome === 'single'
      ? `One ranked run of ${group.taskId}. A single observation is not a comparison — there is `
        + 'nothing here that another run of this objective lost to.'
      : group.outcome === 'tied'
        ? `All ${group.ranked} ranked runs of ${group.taskId} recorded exactly the same value `
          + `(${group.bestValue}). This group has no winner: it is a ${group.ranked}-way tie, not an `
          + 'ordering.'
        : group.leaders.length > 1
          ? `${group.leaders.length} of ${group.ranked} ranked runs share the ${objective} recorded `
            + `value (${group.bestValue}) for ${group.taskId}. They tie for first; none of them beat `
            + 'the others.'
          : `Of ${group.ranked} ranked runs of ${group.taskId}, ${group.leaders[0]} recorded the `
            + `${objective} value (${group.bestValue}).`
  const refusals = [
    'This orders the values these runs RECORDED. `/api/runs` carries no metric name, unit, dataset or '
    + 'evaluation protocol, so a shared task ID does not prove the runs measured the same thing.',
    'It is not a claim that each number is about the artifact its own run produced — metric '
    + 'provenance is not recorded (docs 31/35: two nodes here recorded 0.224975 for a checkpoint '
    + 'neither of them trained).',
  ]
  if (group.integrityExcluded > 0) {
    refusals.push(`${group.integrityExcluded} run(s) in this group hold no rank: their event log `
      + 'stops being readable, so the value shown is the best of a PREFIX, not of the run.')
  }
  if (group.provisionalCount > 0) {
    refusals.push(`${group.provisionalCount} of these runs have not finished. Their best is a `
      + 'best-so-far and can still improve.')
  }
  if (group.confirmedCount > 0 && group.confirmedCount < group.size) {
    refusals.push(`${group.confirmedCount} of ${group.size} values are confirmed means and the rest `
      + 'are single best observations — repeated and unrepeated measurements in one column.')
  }
  return { scope, claim, refusals }
}

// The coverage sentence ABOVE the groups, in the spirit of `conceptForest.js::forestCoverage`: a panel
// ranking 21 of 45 runs is telling the truth about 21 runs and NOTHING about the other 24, and it may
// not be read as "the box has been ranked".
export function rankCoverage(index) {
  const totals = index?.totals
  if (!totals || !Number.isSafeInteger(totals.runs) || totals.runs <= 0) return null
  return {
    runs: totals.runs,
    comparableRuns: totals.comparableRuns,
    comparableGroups: totals.comparableGroups,
    outOfScope: totals.runs - totals.comparableRuns,
    singletonTasks: totals.groups - totals.comparableGroups,
    noMetric: totals.noMetric,
    unidentified: totals.unidentified,
    integrityExcluded: totals.integrityExcluded,
    // The honest ceiling: `true` only when every run on the box sits in a group that can be ranked.
    complete: totals.comparableRuns === totals.runs && totals.runs > 0,
    empty: totals.comparableGroups === 0,
  }
}

// Left deliberately unbuilt, and named so the next reader finds the reasoning rather than re-deriving
// it: the TRAJECTORY overlay (node index vs metric per run) needs a per-node series the run summary
// does not carry. This states the gap in the one place a future change would start.
export const TRAJECTORY_GAP = 'A per-run metric trajectory needs the node series; /api/runs carries '
  + 'only `nodes` as a count, so drawing it would cost one state fold per run on the request thread.'
