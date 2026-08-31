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
#   * 2026-08-31. `box-jhub-l40s.sh:39` sets `CAMPAIGN_RUNS="$BENCH_ROOT/camp-runs"` and
#     `campaign.sh:52` writes every task-arm's run to `$CAMPAIGN_RUNS/<task>/run/events.jsonl`.
#     `grep -c camp-runs` over `snapshot.sh` = 0 and over `snapshot_timer.sh` = 0: the discovery
#     glob was `runs-* model-probes probes`, which `camp-runs` matches nowhere. So the CAMPAIGN's
#     own per-run evidence -- the same kind of thing whose loss on 2026-08-29 took sixty-nine runs
#     and ~$100 of metered spend -- was archived by nobody and watched by nobody, while
#     `campaign.sh:1078` does `rm -rf "$TASK_ROOT"` at the head of every attempt. That is the
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
_bench_not_a_run_tree() {
  case "$(basename "$1")" in
    AlgoTune|looplab|looplab_ws|meter|logs|snapshots|campaign*|.*) return 0 ;;
  esac
  return 1
}

bench_campaign_trees() {  # $1 = BENCH_ROOT. One absolute path per line, deduplicated.
  local root="$1" d
  {
    [ -n "${CAMPAIGN_OUT:-}" ] && [ -d "${CAMPAIGN_OUT:-}" ] && printf '%s\n' "${CAMPAIGN_OUT%/}"
    for d in "$root"/campaign*; do
      [ -d "$d" ] && printf '%s\n' "${d%/}"
    done
  } 2>/dev/null | awk 'NF && !seen[$0]++'
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
  } 2>/dev/null | awk 'NF && !seen[$0]++'
}
