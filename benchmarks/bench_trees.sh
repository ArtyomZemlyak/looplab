#!/bin/bash
# WHICH DIRECTORIES UNDER BENCH_ROOT HOLD MEASUREMENTS. One answer, for both the archiver
# (`snapshot.sh`) and the change detector (`snapshot_timer.sh`). Source it; it defines functions
# and runs nothing.
#
# It is a file of its own because those two used to answer the question separately, each with its
# own hardcoded glob, and a hardcoded glob has now gone stale twice at a measured cost:
#
#   * 2026-08-23. `snapshot.sh` named ONE campaign directory. The operator's `CAMPAIGN_OUT` had
#     moved to `campaign-paired/`, so the 03:21 snapshot carried a campaign that had FINISHED on
#     2026-08-20 and not one byte of the live one -- whose 17 `.done` markers and 19 `B-*.final.json`
#     scores were the entire arm-B result set. Discovery by `campaign*` fixed that instance.
#   * 2026-08-31. `box-jhub-l40s.sh`'s `CAMPAIGN_RUNS` sets `CAMPAIGN_RUNS="$BENCH_ROOT/camp-runs"` and
#     `campaign.sh`'s `RUNS_ROOT` writes every task-arm's run to `$CAMPAIGN_RUNS/<task>/run/events.jsonl`.
#     `grep -c camp-runs` over `snapshot.sh` = 0 and over `snapshot_timer.sh` = 0: the discovery
#     glob was `runs-* model-probes probes`, which `camp-runs` matches nowhere. So the CAMPAIGN's
#     own per-run evidence -- the same kind of thing whose loss on 2026-08-29 took sixty-nine runs
#     and ~$100 of metered spend -- was archived by nobody and watched by nobody, while
#     `campaign.sh::run_one` does `rm -rf "$TASK_ROOT"` at the head of every attempt. That is the
#     2026-08-29 loss without a container restart: a retry is enough.
#
# So THE NAME IS NOT THE TEST. A run tree is a directory that CONTAINS run logs, and the two names
# only the operator knows -- `$CAMPAIGN_RUNS` and `$CAMPAIGN_OUT` -- are consulted as variables
# rather than guessed as patterns. A pattern over directory names is what went stale twice; what a
# directory holds does not go stale, and a tree renamed tomorrow is found by the same rule.

# Everything under BENCH_ROOT that is NOT a candidate run tree. Each is excluded because it is
# already archived by a rule of its own (the two checkouts, `meter`, `logs`), copied whole as a
# campaign, deliberately never copied (`.venv` 6.3 GB and `.hf_datasets` 872 MB -- see snapshot.sh's
# header for why), or an input rather than a measurement (`looplab_ws` holds generated task specs).
# `runs-archive` is here for a different reason from the rest and it is the load-bearing one: it is
# THIS MACHINERY'S OWN OUTPUT. `snapshot.sh` defaults `RUNS_ARCHIVE` to `$DEST/../runs-archive`, so
# the moment `SNAPSHOT_DEST` points inside `BENCH_ROOT` -- which is exactly the layout
# `snapshot_timer.sh`'s own "TESTING IT AGAINST A SCRATCH TREE" header prescribes -- the archive
# sits under the root this function walks, is full of `events.jsonl`, and is therefore discovered
# as a run tree and copied INTO ITSELF. Driven: the tree nests one level deeper per cycle
# (`runs-archive/runs-archive/runs-archive/...`) and from the second cycle on `cp` refuses with
# "cannot copy a directory into itself", so `archive_tree` returns 1, the snapshot exits 1 for
# ever, the prune never runs and the timer never advances its fingerprint. The old three-name glob
# (`runs-* model-probes probes`) could not reach it only because `DEST` was hardcoded off-root;
# content discovery can, so the exclusion has to be stated. `snapshot.sh` additionally refuses a
# tree that IS its archive by path, for an archive the operator renamed with SNAPSHOT_RUNS_ARCHIVE.
_bench_not_a_run_tree() {
  case "$(basename "$1")" in
    AlgoTune|looplab|looplab_ws|meter|logs|snapshots|runs-archive|campaign*|.*) return 0 ;;
  esac
  return 1
}

# DEDUPLICATED BY BASENAME, NOT BY PATH, and that is the whole of what this function can promise.
#
# Both consumers key the DESTINATION on `basename`: `snapshot.sh::copy` does `cp -r "$1" "$OUT/"`
# and `archive_tree` does `B="$(basename "$S")"` under one `$RUNS_ARCHIVE`. So two emitted trees
# that share a basename do not become two archives -- they MERGE into one directory, the second
# copy overwriting the first's same-named files, with `FOUND_CAMPAIGN` reporting 2 and the snapshot
# holding 1. Path-keyed dedup could not see that, and the operator-variable branches added above
# are exactly what makes it reachable: their whole point is naming a tree OFF `$BENCH_ROOT`, so
# `CAMPAIGN_OUT=/home/jovyan/data/campaign` beside a stale `$BENCH_ROOT/campaign` -- the 2026-08-23
# shape, one live campaign and one dead one -- merges the live markers with the dead ones under a
# single name and PROVENANCE.txt records neither source.
#
# FIRST WINS, and the order is chosen so that means the OPERATOR'S OWN VARIABLE: it is printed
# before the glob, it is the authoritative answer by this file's own "THE NAME IS NOT THE TEST"
# rule, and a glob hit that collides with it is the stale one. The dropped path is named on stderr
# rather than swallowed -- a tree this machinery declines to archive is exactly the thing the
# operator has to hear about, and stdout is the data channel.
_bench_first_per_basename() {
  # THE EXACT-DUPLICATE DEDUP COMES FIRST AND IS SILENT. `$CAMPAIGN_OUT` and the `campaign*` glob
  # name the SAME directory on the box default (`CAMPAIGN_OUT=$BENCH_ROOT/campaign`), so warning on
  # every repeated line printed "NOT archiving X -- its basename collides with X" on every snapshot
  # and every timer tick -- an alarm about a directory colliding with itself, in the ordinary
  # configuration. Only a basename shared by two DIFFERENT paths is a real collision, and that one
  # is worth saying out loud because both would land at `$ARCHIVE/<basename>` and merge.
  awk 'NF && !dup[$0]++ {
            n = $0; sub(/.*\//, "", n)
            if (n in seen) { print "bench_trees: NOT archiving " $0 " -- its basename collides" \
                                   " with " seen[n] ", and both would merge into one archive" \
                                   > "/dev/stderr"; next }
            seen[n] = $0; print }'
}

bench_campaign_trees() {  # $1 = BENCH_ROOT. One absolute path per line, deduplicated.
  local root="$1" d
  {
    [ -n "${CAMPAIGN_OUT:-}" ] && [ -d "${CAMPAIGN_OUT:-}" ] && printf '%s\n' "${CAMPAIGN_OUT%/}"
    for d in "$root"/campaign*; do
      [ -d "$d" ] && printf '%s\n' "${d%/}"
    done
  } 2>/dev/null | _bench_first_per_basename
}

bench_run_trees() {  # $1 = BENCH_ROOT. One absolute path per line, deduplicated.
  local root="$1" d
  {
    # THE OPERATOR'S OWN VARIABLE FIRST, and by name: `campaign.sh` empties `$CAMPAIGN_RUNS/<task>`
    # at the head of every attempt, so this tree is legitimately EMPTY for the first minutes of a
    # run -- exactly the window a container restart is insurance against -- and content discovery
    # cannot see an empty directory. Naming it is also the only way to reach one set outside
    # BENCH_ROOT.
    [ -n "${CAMPAIGN_RUNS:-}" ] && [ -d "${CAMPAIGN_RUNS:-}" ] && printf '%s\n' "${CAMPAIGN_RUNS%/}"
    for d in "$root"/*; do
      [ -d "$d" ] || continue
      _bench_not_a_run_tree "$d" && continue
      # `-quit` stops at the first hit, so this is one stat-storm-free probe per candidate, not a
      # walk. `events.jsonl` OR `spans.jsonl`: a run that has opened its trace but not yet its
      # event log is still a run, and is still the thing worth archiving.
      [ -n "$(find "$d" \( -name events.jsonl -o -name spans.jsonl \) -print -quit 2>/dev/null)" ] \
        && printf '%s\n' "${d%/}"
    done
  } 2>/dev/null | _bench_first_per_basename
}
