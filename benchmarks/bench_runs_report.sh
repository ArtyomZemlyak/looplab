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
# THE ONLY ROOT HERE THAT COULD NOT BE MOVED, and every other line above says why it should be.
# `snapshot.sh` already reads `SNAPSHOT_DEST` (its line 65) and `snapshot_timer.sh` documents
# setting BOTH it and `BENCH_ROOT` to test against a scratch tree. This line ignored it, so a
# test of this script scanned the operator's live snapshots whatever it set -- inert today
# only because measurements land in `runs-archive`, which IS overridable, and that default is
# `$DEST/../runs-archive`: one different DEST puts them inside the snapshots tree and every
# such test starts silently reading live data. Two tests reddened this afternoon for exactly
# this shape (probe fixtures writing into the live corpus, the profiler guard reading the live
# dataset dir), which is why a latent third is worth one line.
add "${SNAPSHOT_DEST:-/home/jovyan/data/looplab-bench/snapshots}"
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
# ONE PASS OVER EVERY ROOT, DEDUPED. The first cut walked each root separately and printed a line
# per score.log -- and a finished run exists TWICE, once in the live tree and once in the archive,
# so `remPde4 node_0` was reported as two zeros within an hour of this section shipping. The reader
# of a sweep needs to know how many zeros there are, not how many copies of the evidence exist.
# The path LIST goes in a file, not down stdin: `python3 -` reads its PROGRAM from stdin, so
# piping find into a heredoc'd script gives the interpreter two things on one channel and the
# script sees an empty stdin. It printed "no zero-scoring nodes" over a box that had one.
_ZL="$(mktemp)"
find -L "${ROOTS[@]}" -name score.log 2>/dev/null | sort > "$_ZL"
python3 - "$_ZL" <<'PY'
import json, os, sys

seen = {}
unreadable = []
for line in open(sys.argv[1]):
    path = line.strip()
    if not path:
        continue
    # THE FILE IS NOT ALWAYS PURE JSON. `looplab_eval` prints its build result first --
    # `looplab_eval: build_ext ok`, or `build_ext failed rc=1: ...` -- and the JSON follows. Parsing
    # the whole file therefore raises, and the `except: continue` above silently DROPPED the node.
    #
    # Measured 2026-09-01: 31 of 76 score.log files on this box carry such a prefix, and one of the
    # corpus's three zero-scoring nodes was invisible because of it -- remEE6's node_3, whose Cython
    # kernel failed to compile ('cpython/long/PyLong_AS_LONG.pxd' not found). A section built to stop
    # a zero going unreported was itself losing one.
    # SCAN for the first offset that actually decodes, and keep everything before it.
    #
    # `raw.find("{")` plus a whole-tail `json.loads` was the previous rule and it failed two ways,
    # both reproduced 2026-09-01: a brace INSIDE the build prefix (C and Cython print them freely --
    # `error: expected '}' before` is an ordinary compiler message) aimed the decoder at the wrong
    # offset, and any text AFTER the JSON made the tail undecodable. Both ended at the same
    # `except: continue`, dropping the node in silence -- the very failure this section exists to
    # prevent, in the repair for the first version of it.
    raw = open(path, errors="replace").read().strip()
    dec = json.JSONDecoder()
    j = None
    prefix_raw = ""
    k = raw.find("{")
    while k >= 0:
        try:
            j, _ = dec.raw_decode(raw[k:])
        except ValueError:
            k = raw.find("{", k + 1)
            continue
        prefix_raw = raw[:k]
        break
    if not isinstance(j, dict):
        # NOT silent. A score.log that cannot be read is a node whose outcome is UNKNOWN, which is
        # a different and more alarming fact than "it scored fine".
        unreadable.append((path.split("/runs/", 1)[0].rsplit("/", 1)[-1] if "/runs/" in path else "?",
                           os.path.basename(os.path.dirname(path)), path))
        continue
    plines = [x.strip() for x in prefix_raw.strip().splitlines() if x.strip()]
    prefix = " | ".join(plines)[:200]
    sp = j.get("speedup")
    if isinstance(sp, (int, float)) and sp > 0:
        continue                       # a real score is not a zero
    node = os.path.basename(os.path.dirname(path))
    run = path.split("/runs/", 1)[0].rsplit("/", 1)[-1] if "/runs/" in path else "?"
    key = (run, node)
    seen.setdefault(key, []).append((path, j))

if unreadable:
    print("score.log FILES THAT COULD NOT BE READ (outcome unknown, not 'fine'):")
    for run_u, node_u, path_u in sorted(set(unreadable)):
        print(f"  {run_u:<10} {node_u:<8} {path_u}")
    print()

if not seen:
    print("no zero-scoring nodes on this box")
    sys.exit(0)

print("ZERO-SCORING NODES (reason read from nodes/<id>/score.log):")
for (run, node), copies in sorted(seen.items()):
    _, j = copies[0]
    sp = j.get("speedup")
    # ABSENT IS NOT ZERO. `float(... or 0.0)` turned a missing field into 0.0 and then into
    # "RULER (never reached the solver)" -- a diagnosis invented out of an absence.
    raw_secs = j.get("eval_seconds")
    secs = float(raw_secs) if isinstance(raw_secs, (int, float)) else None
    ns = j.get("no_speedup") or {}
    reason = str(ns.get("reason") or ("speedup is null" if sp is None else "unstated"))
    # THE SWEEP LIST'S OWN RULE: ~0.1 s means the evaluation never reached the solver. A THIRD
    # category sits between the two and is named separately: the submission never COMPILED, which
    # `looplab_eval` reports in the prefix line and which no eval_seconds threshold can distinguish
    # (remEE6's node_3 came back in 8.3 s).
    # Keyed on the harness's OWN reason, not on sniffing the prefix: the build error is multi-line
    # and its last line before the JSON says nothing about building. `compilation_failed` is what
    # `looplab_eval` puts in `no_speedup.reason`, and it is the only thing that says so reliably.
    if reason == "compilation_failed" or "build_ext failed" in prefix:
        flag = "BUILD (the submission never compiled)"
    elif secs is None:
        flag = "unknown (no eval_seconds recorded)"
    elif secs < 1.0:
        flag = "RULER (never reached the solver)"
    else:
        flag = "solver"
    dupes = f"  [{len(copies)} copies]" if len(copies) > 1 else ""
    shown = f"{secs:>7.1f}s" if secs is not None else "      ?s"
    print(f"  {run:<10} {node:<8} {shown}  {flag:<32} {reason}{dupes}")
    verdict = str(ns.get("evaluator_verdict") or "")[:70]
    if verdict:
        print(f"             {verdict}")
    if "build_ext failed" in prefix:
        print("             " + prefix[:110])
    errs = ns.get("is_solution_errors") or []
    if errs:
        first = errs[0] if isinstance(errs[0], dict) else {"message": str(errs[0])}
        print("           " + str(first.get("message") or "")[:90])
PY
rm -f "$_ZL"

# EXPLICIT, because a `[ ... ] && echo` tail returns 1 when the test is false -- so FINDING a zero
# made this report exit non-zero, i.e. "the instrument failed" whenever it had something to say.
# Caught by its own test within the hour; the same shape as `cmd | tail` returning tail's status.
exit 0
