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

# ZEROS, WITH THE REASON. A node scoring 0.0 is at least six different facts and the durable EVENT
# carries only the number: `node_evaluated` has `metric` and `eval_seconds` and nothing else. The
# diagnosis exists -- `nodes/<id>/score.log` holds the harness's `no_speedup` block, with the
# evaluator's verdict and the actual `is_solution` errors -- and it is in a file this report was not
# reading. On 2026-08-31 that cost four hand-rolled commands to answer the one question the sweep
# list asks every time: is a zero the RULER failing or the SOLVER failing?
#
# The discriminator is stated in the sweep brief and is now applied here: an evaluation that
# returned in about a tenth of a second never ran the solver at all.
echo
ZFOUND=0
for R in "${ROOTS[@]}"; do
  while IFS= read -r SL; do
    [ -s "$SL" ] || continue
    OUT=$(python3 - "$SL" <<'PY'
import json, os, sys
p = sys.argv[1]
try:
    j = json.loads(open(p, errors="replace").read().strip())
except Exception:
    sys.exit(0)
sp = j.get("speedup")
if isinstance(sp, (int, float)) and sp > 0:
    sys.exit(0)                       # a real score is not a zero
secs = float(j.get("eval_seconds") or 0.0)
ns = j.get("no_speedup") or {}
reason = str(ns.get("reason") or ("speedup is null" if sp is None else "unstated"))
verdict = str(ns.get("evaluator_verdict") or "")[:70]
errs = ns.get("is_solution_errors") or []
detail = ""
if errs:
    first = errs[0] if isinstance(errs[0], dict) else {"message": str(errs[0])}
    detail = "  " + str(first.get("message") or "")[:90]
# THE SWEEP LIST'S OWN RULE: ~0.1 s means the evaluation never reached the solver.
flag = "RULER (never reached the solver)" if secs < 1.0 else "solver"
node = os.path.basename(os.path.dirname(p))
run = p.split("/runs/", 1)[0].rsplit("/", 1)[-1] if "/runs/" in p else "?"
print(f"  {run:<10} {node:<8} {secs:>7.1f}s  {flag:<32} {reason}")
if verdict:
    print(f"             {verdict}")
if detail:
    print(f"           {detail}")
PY
)
    if [ -n "$OUT" ]; then
      [ "$ZFOUND" -eq 0 ] && echo "ZERO-SCORING NODES (reason read from nodes/<id>/score.log):"
      ZFOUND=$((ZFOUND + 1))
      echo "$OUT"
    fi
  done < <(find -L "$R" -name score.log 2>/dev/null | sort)
done
[ "$ZFOUND" -eq 0 ] && echo "no zero-scoring nodes on this box"

# EXPLICIT, because the line above is the last statement and `[ ... ] && echo` returns 1 when the
# test is false -- so FINDING a zero made this report exit non-zero, i.e. "the instrument failed"
# whenever it had something to say. Caught by its own test within the hour; the same shape as
# `cmd | tail` returning tail's status.
exit 0
