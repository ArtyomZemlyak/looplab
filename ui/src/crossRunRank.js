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
// THE OTHER TWO OVERLAYS THAT WERE ON THE TABLE — one refused for good, one built (2026-09-06, doc 52
// row 26) once both of its objections were answered rather than waived.
//   - A NORMALIZED cross-group score (percentile / z-score) so all 5 groups could share one axis. It
//     dies on this corpus's own numbers: 3 of the 5 groups have n <= 3, and two of them have ZERO
//     variance (3x 1.0; 2x 0.925), so the z denominator is 0 and the percentile is 100th for everyone.
//     It would have printed a confident position for a group that contains no ordering information at
//     all, and — the deciding objection — the operator cannot check it. A rank of "1 of 3, values
//     0.8077 / 0.7280 / 0.2250" is verifiable by looking; "z = +1.15" is not.
//   - A per-run metric TRAJECTORY overlay (experiment index vs running best), comparable in shape if
//     not in scale. REFUSED on 2026-08-14 for two reasons, and BUILT once each was answered. The
//     cost objection — the run row carried `nodes` as a COUNT and no series, so the overlay needed one
//     `/api/runs/{id}/state` per run: 45 folds on the request thread for a panel that opens on a
//     click, against a server whose live runs re-fold on every poll — is answered on the server:
//     `events/trajectory.py::running_best` puts the running best on the summary row as CHANGE POINTS
//     (`[experiment, best, node_id]`), derived once from the same fold and cached with it, so a poll
//     pays nothing and `runTrajectory` below only validates. The scale objection — drawing 5
//     objectives on one axis needs a per-run rescale, the normalized-score objection wearing a line
//     chart — is answered by never doing it: `trajectoryOverlay` draws ONE comparable group per chart
//     (one task, one direction, one evaluation), so the axis is the group's own objective, and a
//     prefix-folded run is not drawn for the same reason it holds no rank.
import {
  COMPARABILITY_REFUSAL_TEXT, COMPARABILITY_UNKNOWN, bestMetricCaveatNotice, bestMetricCaveats,
  comparabilityRecord, metricIncomparability, metricIncomparabilityText, runTaskId,
  sourceIncomplete,
} from './runIndex.js'

// THE COMPARABILITY PARTITION, added 2026-08-20, and it is refusal 1 below finally becoming a rule
// instead of a caveat. A (task_id, direction) bucket is SUB-PARTITIONED by the comparability key its
// rows carry (`looplab/engine/comparability.py`), so a ranking is published only over runs whose
// numbers were measured against the same evaluation — and every subset that falls out is still on
// screen, named, with its values shown, which is this module's own rule for a subset it may not rank.
//
// UNKNOWN IS ITS OWN PARTITION AND NOT A WILDCARD. Every run on this box today has no key, so they
// all land in one `unknown` partition together and rank exactly as they did before this shipped —
// the corpus does not move by one row. The moment ONE run declares `eval.inputs` it LEAVES that
// partition rather than joining it: a keyed run and an unkeyed run have not been shown to measure the
// same thing, and this is the surface on which that must be visible rather than assumed. The split
// IS the finding, and it is the one the four recall@100 values on this box needed.
const partitionKey = (run) => {
  const record = comparabilityRecord(run)
  if (!record) return ''
  const authority = String(record.authority || '')
  const key = record.keys?.[authority]
  return key ? `${authority}:${key}` : ''
}

// THE REFUSE-ONLY DISCRIMINATORS beside the key, in the order `runIndex.js::pairRefusal` asks them.
// A partition shares one authority key, but a pair in it can still be provably DIFFERENT on one of
// these — a source tree promoted mid-way, a smoke-scored champion beside a full-scored one. Such a
// partition used to go WHOLE to the unranked count and render no row at all, so the panel said "No
// per-run metric observations for this task ID yet" over three runs with values (critic 2026-09-26,
// rendered in jsdom), and the two smoke runs that WERE comparable lost their ranking too.
//
// It is split instead, by exactly the discriminators on which two of its runs RECORDED different
// values — the only ones that can refuse a pair, since `pairRefusal` asks a facet only when both
// sides carry it. A discriminator that varies by ABSENCE alone (one recorded value beside runs
// that recorded none) refuses nothing, and splitting on it would separate runs nothing separates.
// For the discriminators that do split, absence is a value of its own: a run that recorded no
// profile cannot be placed on either side of two profiles that disagree. That part is stricter
// than the evidence (a run that recorded none has not been shown to differ), so each part carries
// which of its separation is PROVEN and which is only UNRECORDED (`splitProven` /
// `splitUnrecorded`), and says so in those words (critic 2026-09-26: the "(none recorded)" part
// printed "provably differ from these", and the coverage line counted it among the runs that
// provably differ). The split is applied only to a partition already refused whole, so it can only
// ADD rankings to what the screen showed before, never take one away. It is the browser's own and
// is never persisted — the reason the durable `group_token` leaves these out
// (`engine/comparability.py`) does not reach a view.
//
// A partition that varies on NONE of them is not split at all: it stays one group, refused,
// worded from its own refusal (`metricIncomparability`). Inside one task, direction and key the
// refusal left is a pair of KEYS at a stronger authority than the one the partition is grouped
// by. Splitting it by nothing produced a part with `split = {}` — truthy — whose header printed an
// empty label, whose line said "provably differ from these on , so", and whose claim blamed a
// source tree or protocol that never varied (critic 2026-09-26, rendered in jsdom).
const DISCRIMINATORS = ['substrate', 'profile', 'scorer', 'fingerprint']
const discriminatorValues = (run) => {
  const record = comparabilityRecord(run)
  const protocol = isRecord(record?.protocol) ? record.protocol : {}
  const text = value => (typeof value === 'string' ? value : '')
  return {
    substrate: text(record?.substrate),
    profile: text(protocol.profile),
    scorer: text(protocol.scorer),
    fingerprint: text(protocol.fingerprint),
  }
}

function splitRefusedBucket(bucket) {
  const values = bucket.members.map(entry => discriminatorValues(entry.run))
  const varying = DISCRIMINATORS.filter(
    name => new Set(values.map(value => value[name]).filter(Boolean)).size > 1)
  if (!varying.length) return [bucket]
  const unrecordedSomewhere = varying.filter(name => values.some(value => !value[name]))
  const subs = new Map()
  bucket.members.forEach((entry, index) => {
    const split = Object.fromEntries(varying.map(name => [name, values[index][name]]))
    const subKey = varying.map(name => `${name}=${split[name]}`).join('\u0000')
    if (!subs.has(subKey)) {
      subs.set(subKey, {
        ...bucket, key: `${bucket.key}\u0000${subKey}`, split, members: [],
        // PROVEN: this part recorded a value, and — every discriminator here holding two or more
        // recorded values — some other part recorded a different one.
        splitProven: varying.filter(name => split[name]),
        // UNRECORDED: this part recorded none, so nothing proves it differs from anyone.
        splitUnrecorded: varying.filter(name => !split[name]),
        // Recorded here, but some run of the partition recorded none: apart from THOSE unproven.
        splitApart: unrecordedSomewhere.filter(name => split[name]),
      })
    }
    subs.get(subKey).members.push(entry)
  })
  return [...subs.values()]
}

// Render backstop. `run_summaries` folds every run directory on the box, and the panel is a table.
const MAX_GROUP_ROWS = 100

const isRecord = value => !!value && typeof value === 'object' && !Array.isArray(value)
const finiteOrNull = value => (typeof value === 'number' && Number.isFinite(value) ? value : null)

// The most runs one overlay draws. `charts.jsx::MultiTrajectory` separates runs by hue AND dash
// pattern, and both palettes hold eight entries, so a ninth line would repeat the first's pair
// exactly — an operator could not tell them apart. The lines beyond it are COUNTED, in rank order.
const MAX_OVERLAY_RUNS = 8

// The run row's trajectory, validated at the boundary. `events/trajectory.py::running_best` writes
// it: change points `[experiment, best, node_id]` with strictly increasing experiment indices, the
// size of the x population in `evaluated`, and `complete: false` when the row was subsampled. A
// malformed or absent series is "no series", never a drawn line — the overlay names how many rows
// carry none, and an additive field a legacy server does not write is exactly that case.
export function runTrajectory(run) {
  const series = isRecord(run) ? run.trajectory : null
  if (!isRecord(series) || !Array.isArray(series.points) || !series.points.length) return null
  let previous = -1
  for (const point of series.points) {
    if (!Array.isArray(point) || point.length < 3) return null
    const [experiment, value, node] = point
    if (!Number.isSafeInteger(experiment) || experiment <= previous
        || finiteOrNull(value) == null || !Number.isSafeInteger(node)) return null
    previous = experiment
  }
  if (!Number.isSafeInteger(series.evaluated) || series.evaluated <= previous) return null
  return { points: series.points, evaluated: series.evaluated, complete: series.complete !== false }
}

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
// The partition is deliberately built and then RE-TESTED with `metricComparable` — through its own
// reading `metricIncomparability`, which also says WHY — instead of being
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
    // `runTaskId` and not a local trim: `metricIncomparability` reads the SAME id, so a bucket can
    // never hold two ids the predicate then refuses as two tasks.
    const taskId = runTaskId(run)
    const direction = run.direction
    // An ABSENT identity is not a shared identity — the same rule the panel already applied to its
    // own run. Legacy rows that all encode "missing" as '' are not a group of like objectives.
    if (!taskId || !['min', 'max'].includes(direction)) { unidentified.push(run); continue }
    // NUL-separated: a task id is an opaque operator string and may contain spaces, and a key two
    // different (task, direction) pairs could both spell is a key that merges two objectives.
    // The partition rides in the SAME NUL-separated key for the same reason the pair does: it is
    // an opaque digest string, and a separator two different partitions could both spell is a
    // key that merges two evaluations — which is the exact merge this partition exists to undo.
    const partition = partitionKey(run)
    const key = `${taskId}\u0000${direction}\u0000${partition}`
    if (!buckets.has(key)) {
      buckets.set(key, { key, taskId, direction, partition, members: [] })
    }
    buckets.get(key).members.push({ run, ...metric })
  }

  const groups = []
  for (const bucket of buckets.values()) {
    // REACHABLE, and the test is the POINT: a partition shares one authority key, but the
    // refuse-only discriminators beside the key — the `substrate`, and since 2026-09-26 the
    // `protocol` (a smoke-scored champion beside a full-scored one) — can still make a pair in it
    // provably DIFFERENT. It is never ranked as one: it is split by what differs
    // (`splitRefusedBucket`), each part re-tested with the same predicate, and a part that STILL
    // fails keeps every row on screen with no rank at all (`refused`, and `refusal` says WHY) —
    // failing closed costs a ranking, ranking across two rulers would cost a false one, and hiding
    // the rows would cost the observations themselves.
    const refusal = metricIncomparability(bucket.members.map(entry => entry.run))
    if (!refusal) {
      groups.push(buildGroup(bucket, limit))
      continue
    }
    for (const part of splitRefusedBucket(bucket)) {
      groups.push(buildGroup(part, limit, {
        refusal: part === bucket ? refusal
          : metricIncomparability(part.members.map(entry => entry.run)) }))
    }
  }
  // Deterministic order, largest first: a caller iterating this must not get a different page because
  // the server listed its directories in a different order. The KEY is the last tiebreak, compared
  // by code unit: two parts of one split share size, task and direction, so without it they came
  // out in the server's listing order (critic 2026-09-26: reversing the input reversed them), and
  // the key is NUL-separated — which a locale collation may treat as ignorable, so it is not asked.
  groups.sort((a, b) => b.size - a.size || a.taskId.localeCompare(b.taskId)
    || a.direction.localeCompare(b.direction) || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0))

  const tally = groupTally(groups)
  return {
    groups,
    comparable: groups.filter(comparableGroup),
    singletons: groups.filter(group => group.size === 1),
    refused: groups.filter(group => group.outcome === 'refused'),
    noMetric,
    unidentified,
    totals: {
      runs: rows.length,
      withMetric: rows.length - noMetric.length,
      noMetric: noMetric.length,
      unidentified: unidentified.length,
      ...tally,
    },
  }
}

// A COMPARABLE GROUP: two or more runs a ranking may order — one task, one direction, one recorded
// evaluation, and no pair of them refused. The ONE predicate behind every count this panel prints
// under that name: the toolbar used to count every group of the task under it (singletons and
// refused parts included) while the coverage line counted only these, so one screen read
// "3 comparable groups" above "0 of them sit in 0 comparable groups" (critic 2026-09-26).
export const comparableGroup = group => isRecord(group) && group.size > 1
  && group.outcome !== 'refused'

// What a GROUP is, said where the counts are — the toolbar printed a count of them and never said.
export const GROUP_DEFINITION = 'a group is the runs of one task and one direction that share a '
  + 'comparability key (or record none), split further by source tree and protocol where those '
  + 'disagree; a comparable group holds two or more runs a ranking may order'

// THE ONE TALLY of a list of groups: `crossRunGroups` takes its totals from it for the whole box
// and the panel's toolbar takes its count from it for one task, so the two numbers are one
// derivation over two scopes and the task's can never exceed the box's.
export function groupTally(groups = []) {
  const list = Array.isArray(groups) ? groups.filter(isRecord) : []
  const runsIn = picked => picked.reduce((sum, group) => sum + group.size, 0)
  const comparable = list.filter(comparableGroup)
  const refused = list.filter(group => group.outcome === 'refused')
  const split = list.filter(group => group.split)
  const proven = group => Array.isArray(group.splitProven) && group.splitProven.length > 0
  return {
    groups: list.length,
    comparableGroups: comparable.length,
    singletonGroups: list.filter(group => group.size === 1).length,
    refusedGroups: refused.length,
    // Runs of one task and key that a source tree or protocol split further — those whose part
    // recorded a value another part provably contradicts, and those set apart only because they
    // recorded none (`splitRefusedBucket`) — and the runs no split could make agree (shown, never
    // ranked).
    splitRuns: runsIn(split),
    splitProvenRuns: runsIn(split.filter(proven)),
    splitUnrecordedRuns: runsIn(split.filter(group => !proven(group))),
    refusedRuns: runsIn(refused),
    // Runs that actually sit in a group where a ranking is possible. This is the number the panel
    // reports as coverage, and on this corpus it is 21 of 45.
    comparableRuns: runsIn(comparable),
    rankedRuns: comparable.reduce((sum, group) => sum + group.ranked, 0),
    integrityExcluded: list.reduce((sum, group) => sum + group.integrityExcluded, 0),
    caveatedRuns: list.reduce((sum, group) => sum + group.caveatedCount, 0),
  }
}

function buildGroup(bucket, limit, { refusal = '' } = {}) {
  const refused = !!refusal
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
    // WHAT KIND OF NUMBER this row's value is, from the server's `best_metric_caveats` receipt
    // (`looplab/engine/champion_caveats.py`). This closes refusal 2 below by exactly the width of
    // what the row now carries and no more: the caveat says the run RECORDED something about its
    // champion's number and selected on it anyway — a salvage the operator admitted with
    // `metric_salvage: "select"`, or a hard reward-hack/leakage signal its `trust_gate` did not
    // enforce.
    //
    // CAVEAT, AND DELIBERATELY NOT UNRANK — the same decision `panels.jsx::ParetoPanel` settled on
    // 2026-08-15, and NOT the `sourceIncomplete` rung one line up, which is why the two are separate
    // fields rather than one "eligible" flag. An incomplete log means the value shown is the best of
    // a PREFIX: it is not this run's best and no ordering can use it. A caveated value IS this run's
    // best — the run's own selector crowned it under a rung the operator configured — so dropping it
    // from the ordering would publish a leaderboard that disagrees with the runs it ranks, and would
    // overrule a recorded operator decision to boot. What it must never be is SILENT, because this
    // table is the surface an operator reads to decide which configuration to reuse.
    caveats: bestMetricCaveats(entry.run),
    caveatNotice: bestMetricCaveatNotice(entry.run),
    // THE RUN'S TRAJECTORY, when its row carries one (`events/trajectory.py::running_best`): the
    // change points of its running best, validated by `runTrajectory`. Read here, beside the rank,
    // so the overlay draws the SAME rows the table ranks and nothing the table does not show.
    trajectory: runTrajectory(entry.run),
  }))
  // A REFUSED part holds no rank at all: every row is shown, with its value, and none is ordered.
  const eligible = refused ? [] : entries.filter(entry => !entry.sourceIncomplete)
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
  const unranked = entries.filter(entry => refused || entry.sourceIncomplete)
    .sort((a, b) => a.runId.localeCompare(b.runId))
  for (const entry of unranked) { entry.rank = null; entry.tied = 0 }

  // What this group IS, in one word the render can switch on. `tied` is not a degenerate 'ranked': a
  // group whose eligible runs all recorded the same number contains no ordering information, and
  // saying "3-way tie at 1.0" is the whole finding.
  const outcome = refused ? 'refused' : eligible.length === 0 ? 'none'
    : eligible.length === 1 ? 'single'
      : values.length === 1 ? 'tied' : 'ranked'
  const shown = ordered.slice(0, limit)
  return {
    key: bucket.key,
    taskId: bucket.taskId,
    direction,
    // WHICH evaluation this group's numbers were measured against — `''` when no run in it recorded
    // one. Carried so the render can NAME the partition: showing an operator two groups of one task
    // with no account of why they are two would be a worse silence than the one this replaced.
    partition: bucket.partition || '',
    comparability: bucket.partition ? String(bucket.partition).split(':')[0] : COMPARABILITY_UNKNOWN,
    // What further split this group from the rest of its partition — `{discriminator: value}`, a
    // value of '' meaning "recorded none" — or null for a partition that was never split (never
    // refused, or refused with nothing varying to split it by). Which of it is PROVEN, which only
    // UNRECORDED here, and which only unrecorded elsewhere: see `splitRefusedBucket`.
    split: isRecord(bucket.split) ? bucket.split : null,
    splitProven: Array.isArray(bucket.splitProven) ? bucket.splitProven : [],
    splitUnrecorded: Array.isArray(bucket.splitUnrecorded) ? bucket.splitUnrecorded : [],
    splitApart: Array.isArray(bucket.splitApart) ? bucket.splitApart : [],
    // WHY no row of this group holds a rank (`runIndex.js::metricIncomparability`: a pair refusal,
    // inside one task and direction), or '' for a group that was not refused. The ONE reading the
    // claim, the header and every unranked row's title are worded from.
    refusal,
    size: entries.length,
    ranked: ordered.length,
    // The PREFIX-folded rows, whatever else holds a row unranked (a refused part's rows are
    // unranked for another reason, and are not counted here).
    integrityExcluded: entries.filter(entry => entry.sourceIncomplete).length,
    // Counted over EVERY entry, ranked or not: a caveat is a fact about the number, and an unranked
    // row still shows its value.
    caveatedCount: entries.filter(entry => entry.caveats.length).length,
    // Whether the row that WON is one of them, which is the sharper question and the one the group's
    // refusal names — a caveated also-ran changes nothing about who leads.
    caveatedLeader: ordered.some(entry => entry.rank === 1 && entry.caveats.length),
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
  // A REFUSED group is worded from its own refusal: the sentence it replaced blamed "a split by
  // source tree or protocol" on a group nothing of the kind had split — its runs refused by keys,
  // or by task ids a trim made one (critic 2026-09-26).
  const claim = group.outcome === 'refused'
    ? `None of these ${group.size} runs of ${group.taskId} holds a rank: ${refusalClause(group)}. `
      + 'Each value is shown and is true of its own measurement; no ordering between them is.'
    : group.outcome === 'none'
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
    // NARROWED 2026-08-15, by exactly the width of what the row now carries. `best_metric_caveats`
    // records whether the champion's number was SALVAGED or TRUST-FLAGGED; it still records nothing
    // about the metric SUBJECT — which artifact the number is a claim about — and that is the half
    // docs 31/35 measured, so the refusal keeps its example and loses only the sentence that is no
    // longer true.
    'It is not a claim that each number is about the artifact its own run produced — the metric '
    + 'SUBJECT is not recorded on this row (docs 31/35: two nodes here recorded 0.224975 for a '
    + 'checkpoint neither of them trained).',
  ]
  // THE COMPARABILITY STATE OF THE PARTITION, added 2026-08-20. It is stated for BOTH answers and
  // never omitted, because the whole defect this closes is that silence read as agreement: an
  // operator looking at four recall@100 values in one `repo_task` group had nothing on the screen
  // telling them that some were measured on one test set and some on another. Refusal 1 above stays
  // verbatim and stays true either way — the metric NAME and UNIT are still not on this row; what
  // changed is that the DATA and the PROTOCOL can now be decidable, and the group says which it is.
  //
  // A REFUSED group gets its own sentence for both answers (`refusedPartitionLine`): a shared key
  // there is a GROUPING and not an agreement, and "unknown whether they were measured against the
  // same test set" is false of runs the refusal has just shown differ.
  refusals.push(group.outcome === 'refused' ? refusedPartitionLine(group) : group.partition
    ? `These ${group.size} run(s) share a recorded comparability key (${group.partition}), so their `
      + 'numbers were measured against the same declared evaluation inputs. Runs of this same task '
      + 'that recorded a DIFFERENT key, or none at all, are in their own group above or below — they '
      + 'are deliberately not ranked against these.'
    : 'No run in this group records a comparability key, so it is UNKNOWN whether they were measured '
      + 'against the same test set, the same corpus or the same protocol — and unknown is not the '
      + 'same as yes. On this box a `repo_task` group has held recall@100 values measured on more '
      + 'than one test set. Declare `eval.inputs` on the task to make this decidable.')
  // WHY this group is a part of its partition and not the whole of it (`splitRefusedBucket`).
  refusals.push(...splitClaims(group))
  if (group.caveatedCount > 0) {
    // The COUNT and the LEADERSHIP are two different facts and the sentence says both: a caveated
    // also-ran is a footnote, a caveated leader is the answer to "which configuration should I
    // reuse". Neither is unranked — see the `caveats` comment in `buildGroup`.
    refusals.push(`${group.caveatedCount} run(s) in this group publish a best metric their own run `
      + 'recorded a caveat about (salvaged, or from a trust-flagged node) and selected on anyway. '
      + (group.caveatedLeader
        ? 'One of them leads this group. Open it before reusing its configuration.'
        : 'None of them leads this group.'))
  }
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

// The words for a group's `split`, one clause per discriminator that varies; '' for no split.
const SPLIT_NAMES = {
  substrate: 'source tree', profile: 'eval profile', scorer: 'host scorer',
  fingerprint: 'eval fingerprint',
}
export function splitLabel(split) {
  if (!isRecord(split)) return ''
  return Object.entries(split)
    .map(([name, value]) => `${SPLIT_NAMES[name] || name} ${value ? String(value).slice(0, 12)
      : '(none recorded)'}`)
    .join(', ')
}

// WHY THIS PART IS A PART, one sentence per kind of separation, and never "provably" about a
// separation nothing proves (critic 2026-09-26: the "(none recorded)" part said other runs
// "provably differ from these on eval profile (none recorded)"). `splitRefusedBucket` decides which
// is which; the header's title and `groupClaim` both print these, so the two cannot disagree.
export function splitClaims(group) {
  if (!isRecord(group) || !isRecord(group.split)) return []
  const list = value => (Array.isArray(value)
    ? value.filter(name => Object.hasOwn(group.split, name)) : [])
  const names = picked => picked.map(name => SPLIT_NAMES[name] || name).join(' or ')
  const these = group.size === 1 ? 'this run' : 'these runs'
  const proven = list(group.splitProven)
  const unrecorded = list(group.splitUnrecorded)
  const apart = list(group.splitApart)
  const out = []
  if (proven.length) {
    out.push(`Runs of this task with the same comparability key that recorded a different `
      + `${names(proven)} provably differ from ${these} (${splitLabel(Object.fromEntries(
        proven.map(name => [name, group.split[name]])))}), so each part is ranked on its own and `
      + 'never against the other.')
  }
  // Scoped to the FACET, not the run: a part can be set apart on one facet by absence and provably
  // differ on another, and "nothing proves a difference" said of the whole run would be false.
  if (unrecorded.length) {
    out.push(`${these[0].toUpperCase()}${these.slice(1)} recorded no ${names(unrecorded)}, while `
      + 'other runs of this task with the same comparability key recorded conflicting ones — not '
      + 'recorded, so not comparable with either side. '
      + `${group.size === 1 ? 'It is' : 'They are'} ranked apart from those runs, though a missing `
      + `${names(unrecorded)} proves no difference.`)
  }
  if (apart.length) {
    out.push(`Runs of this task with the same comparability key that recorded no ${names(apart)} `
      + `are ranked apart from ${these} as well, though a missing ${names(apart)} proves no `
      + 'difference.')
  }
  return out
}

// WHY a refused group's runs hold no rank, as a clause. A pair refusal completes "two of them …"
// (`runIndex.js::COMPARABILITY_REFUSAL_TEXT`); anything else is `metricIncomparabilityText`'s whole
// clause. '' for a group that was not refused.
function refusalClause(group, subject = 'two of them') {
  if (!isRecord(group) || group.outcome !== 'refused') return ''
  const reason = typeof group.refusal === 'string' ? group.refusal : ''
  if (reason && Object.hasOwn(COMPARABILITY_REFUSAL_TEXT, reason)) {
    return `${subject} ${COMPARABILITY_REFUSAL_TEXT[reason]}`
  }
  return metricIncomparabilityText(reason) || `${subject} provably disagree on their evaluation`
}

// The sentence a refused group prints where every other group states its comparability key.
function refusedPartitionLine(group) {
  return group.partition
    ? `These ${group.size} runs are grouped by a shared comparability key (${group.partition}), `
      + 'but a pair of them is refused all the same (above), so sharing that key does not make '
      + 'them one evaluation. Runs of this same task that recorded a DIFFERENT key, or none at '
      + 'all, are in their own group above or below.'
    : `These ${group.size} runs record no comparability key they could be grouped by, and a pair `
      + 'of them is refused all the same (above): what their records do carry is enough to show '
      + 'they differ.'
}

// The title of a row with NO RANK — or, with no row, of the "not ranked" mark in a refused group's
// header — naming the refusal that holds it unranked. A refused group's row used to say only that
// "the runs of this group provably disagree on their evaluation", whatever had refused them; a
// prefix-folded row keeps its own sentence, and a row that is both says both.
const PREFIX_ROW_TITLE = "no rank: this run's event log stops being readable, so the value beside "
  + 'it is the best of a PREFIX'
export function unrankedRowTitle(group, row = null) {
  if (!isRecord(group) || group.outcome !== 'refused') return PREFIX_ROW_TITLE
  return `no rank: ${refusalClause(group, 'two runs of this group')} — no ordering between the `
    + 'runs of this group is a fact'
    + (row?.sourceIncomplete ? `; and ${PREFIX_ROW_TITLE.slice('no rank: '.length)}` : '')
}

// The coverage sentence ABOVE the groups, in the spirit of `conceptForest.js::forestCoverage`: a panel
// ranking 21 of 45 runs is telling the truth about 21 runs and NOTHING about the other 24, and it may
// not be read as "the box has been ranked".
export function rankCoverage(index) {
  const totals = index?.totals
  if (!totals || !Number.isSafeInteger(totals.runs) || totals.runs <= 0) return null
  const count = value => (Number.isSafeInteger(value) ? value : 0)
  return {
    runs: totals.runs,
    comparableRuns: totals.comparableRuns,
    comparableGroups: totals.comparableGroups,
    outOfScope: totals.runs - totals.comparableRuns,
    // The ONE-RUN groups, read off `groupTally` — never `groups - comparableGroups`, which also
    // counted every REFUSED group (two or more runs, no rank) as "the only run of its kind".
    singletonTasks: count(totals.singletonGroups),
    noMetric: totals.noMetric,
    unidentified: totals.unidentified,
    splitRuns: count(totals.splitRuns),
    // Only these are said to PROVABLY differ; the unrecorded ones are said to be set apart,
    // unproven.
    splitProvenRuns: count(totals.splitProvenRuns),
    splitUnrecordedRuns: count(totals.splitUnrecordedRuns),
    refusedRuns: count(totals.refusedRuns),
    integrityExcluded: totals.integrityExcluded,
    // The honest ceiling: `true` only when every run on the box sits in a group that can be ranked.
    complete: totals.comparableRuns === totals.runs && totals.runs > 0,
    empty: totals.comparableGroups === 0,
  }
}

// WHAT ONE GROUP'S OVERLAY DRAWS, and what it leaves out, each COUNTED. The rows are the group's
// ranked rows in rank order, so the legend reads as the leaderboard; a row is drawn only when it
// carries a validated series. Four exclusions, each for a reason the panel prints:
//   - a prefix-folded run (`group.unranked`) is not drawn: its series is the running best of a PREFIX,
//     and beside complete runs its line would read as a search that stopped early — the same reason
//     it holds no rank;
//   - a REFUSED group's other runs are not drawn either, for the reason none of them holds a rank:
//     one axis over a pair that provably disagrees on its evaluation would order them anyway. They
//     are counted apart from the prefix-folded ones — `prefix` used to count every unranked row,
//     so a refused group of complete runs read "2 prefix-folded runs are not drawn" (critic
//     2026-09-26, the miscount 45d14782 fixed in `integrityExcluded` and not here);
//   - a row without a series is counted, never drawn as a flat line at any value;
//   - the rows beyond `MAX_OVERLAY_RUNS` are counted, because the chart cannot tell a ninth apart.
// `capped` counts drawn runs whose row was subsampled server-side (`complete: false`): the legend
// marks each and the sentence names the count, since a coarser step is still an honest one.
export function trajectoryOverlay(group, { limit = MAX_OVERLAY_RUNS } = {}) {
  const ranked = Array.isArray(group?.rows) ? group.rows : []
  const unranked = Array.isArray(group?.unranked) ? group.unranked : []
  const withSeries = ranked.filter(row => row.trajectory)
  const drawn = withSeries.slice(0, Math.max(0, limit))
  return {
    runs: drawn.map(row => ({
      run_id: row.runId,
      label: row.rank != null ? `#${row.rank} ${row.label}` : row.label,
      points: row.trajectory.points,
      evaluated: row.trajectory.evaluated,
      complete: row.trajectory.complete,
    })),
    drawn: drawn.length,
    noSeries: ranked.length - withSeries.length,
    prefix: unranked.filter(row => row.sourceIncomplete).length,
    refused: group?.outcome === 'refused'
      ? unranked.filter(row => !row.sourceIncomplete).length : 0,
    beyondLimit: withSeries.length - drawn.length,
    capped: drawn.filter(row => !row.trajectory.complete).length,
    limit: Math.max(0, limit),
  }
}

// THE SENTENCE under the chart, in the model for the reason `groupClaim` is: the counts ARE the
// claim, and a render that could drop one to save a line would be publishing a tidier overlay than
// the group has.
export function trajectoryClaim(group, overlay) {
  const size = Number.isSafeInteger(group?.size) ? group.size : 0
  const plural = (n, one, many) => (n === 1 ? one : many)
  const left = []
  if (overlay.noSeries) left.push(`${overlay.noSeries} ${plural(overlay.noSeries, 'carries', 'carry')} no series (no feasible measured node, or a row served before the series existed)`)
  if (overlay.prefix) left.push(`${overlay.prefix} prefix-folded ${plural(overlay.prefix, 'run is', 'runs are')} not drawn, for the reason ${plural(overlay.prefix, 'it holds', 'they hold')} no rank`)
  if (overlay.refused) left.push(`${overlay.refused} ${plural(overlay.refused, 'run is', 'runs are')} not drawn, for the reason ${plural(overlay.refused, 'it holds', 'they hold')} no rank: a pair of this group's runs provably disagrees on its evaluation, and one axis would order them all the same`)
  if (overlay.beyondLimit) left.push(`${overlay.beyondLimit} beyond the ${overlay.limit} lines the chart can tell apart, in rank order`)
  if (overlay.capped) left.push(`${overlay.capped} drawn coarser: more improvements than the row carries`)
  const tail = left.length ? ` ${left.join('; ')}.` : ''
  if (!overlay.drawn) return `No trajectory to draw for this group.${tail}`
  return `Running best per evaluated experiment for ${overlay.drawn} of ${size} ${plural(size, 'run', 'runs')}, `
    + "on this group's own axis — one task, one direction, one evaluation, nothing rescaled; "
    + `a line holds its value until the experiment that beat it.${tail}`
}
