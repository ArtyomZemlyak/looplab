#!/usr/bin/env bash
# WHAT EVERY RUN ON THIS BOX HAS DONE SO FAR -- one table, by CONTENT.
#
# NOT to be confused with `bench_trees.sh`, which is a LIBRARY: it is sourced by `snapshot.sh` and
# `snapshot_timer.sh` to answer "which directories must be archived/watched". This is a REPORT, run
# by a human during a sweep to answer "how far along is each run, and what has it spent". Different
# questions, and the second one is why this file exists at all.
#
# Written 2026-08-31, after the third time in one sweep that hand-rolled discovery lied:
#   * `find / -xdev -name events.jsonl` returned NOTHING while two runs were live -- /var/tmp is a
#     separate overlayfs and -xdev refuses to cross it.
#   * `find <probe> -maxdepth 3` missed every run: the events live at depth 4 (runs/<task>/run/).
#   * guessing the archive path worked only because a snapshot had already copied one.
# Each of those read as "no measurements exist". None of them meant it.
#
# So: no name globs, no depth guesses, no filesystem assumptions. A tree is a measurement if it
# CONTAINS an events.jsonl or spans.jsonl. Roots come from the same variables the rest of the
# bench uses, so this cannot drift from where things are actually written.
set -u

ROOTS=()
add () { [ -d "$1" ] && ROOTS+=("$1"); }
add "${BENCH_ROOT:-/var/tmp/looplab-bench}"
add "${LOOPLAB_RUNS_DIR:-}"
add "${SNAPSHOT_RUNS_ARCHIVE:-/home/jovyan/data/looplab-bench/runs-archive}"
add "/home/jovyan/data/looplab-bench/snapshots"
for extra in "$@"; do add "$extra"; done

if [ ${#ROOTS[@]} -eq 0 ]; then
  echo "no bench roots exist on this box" >&2
  exit 1
fi

NOW=$(date +%s)
printf '%-52s %6s %6s %9s %8s %5s  %s\n' TREE LINES NODES COST AGE_S SPANS ROOT
FOUND=0
for R in "${ROOTS[@]}"; do
  # One row per RUN DIRECTORY, not per file: events.jsonl and spans.jsonl live side by side and
  # the spans row was pure noise (0 nodes, $0). No -xdev, deliberately -- see the header.
  while IFS= read -r D; do
    E="$D/events.jsonl"
    [ -s "$E" ] || continue
    FOUND=$((FOUND + 1))
    read -r LINES NODES COST <<< "$(python3 - "$E" <<'PY'
import json, sys
n = c = 0; cost = 0.0
for line in open(sys.argv[1], errors="replace"):
    n += 1
    try: r = json.loads(line)
    except Exception: continue
    t = r.get("type")
    if t == "node_evaluated": c += 1
    elif t == "llm_usage": cost += r.get("data", {}).get("cost", 0.0)
print(n, c, f"{cost:.4f}")
PY
)"
    AGE=$(( NOW - $(stat -c %Y "$E") ))
    [ -s "$D/spans.jsonl" ] && SP=$(wc -l < "$D/spans.jsonl") || SP="-"
    REL="${D#$R/}"
    [ ${#REL} -gt 52 ] && REL="…${REL: -51}"
    printf '%-52s %6s %6s %9s %8s %5s  %s\n' "$REL" "$LINES" "$NODES" "$COST" "$AGE" "$SP" "$R"
  done < <(find -L "$R" \( -name events.jsonl -o -name spans.jsonl \) -printf '%h\n' 2>/dev/null | sort -u)
done

[ "$FOUND" -eq 0 ] && { echo "(no events.jsonl under any root -- this box holds no measurements)"; exit 0; }
echo
echo "$FOUND run(s). AGE_S is staleness of events.jsonl; STALL_TIMEOUT is ${STALL_TIMEOUT:-2400}s."
