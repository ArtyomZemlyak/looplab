#!/bin/bash
# Copy everything that cannot be regenerated onto the PERSISTENT filesystem.
#
#   source benchmarks/box-jhub-l40s.sh && benchmarks/snapshot.sh [dest]
#
# The runtime lives on the container's local disk (see docs/52) because the home mount is geesefs
# and cannot host a venv or an honest timing. Local disk does not survive a container restart, so
# the split is: anything scripted is rebuilt, anything MEASURED is copied here.
#
# What is deliberately NOT copied: `.venv` (6.3 GB, rebuilt by two uv commands) and `.hf_datasets`
# (872 MB and growing, re-downloaded on first use). Copying either onto an S3-backed FUSE mount
# costs more than recreating it. Everything else here is either a bundle of a git checkout (both
# of them, since 2026-08-30 -- see 1b for what it cost to learn that ours counted too) or a
# measurement that no command can reproduce.
#
# NOTE: the AlgoTune checkout must NOT be shallow. A bundle made from a `--depth 1` clone names
# parents it does not carry, and `git clone <bundle>` fails with "remote did not send all necessary
# objects" -- i.e. the backup looks fine and is not one. Run `git fetch --unshallow origin` once
# (13 MB -> 210 MB of history here) before trusting a snapshot. This script verifies nothing about
# the bundle; verify a restore by hand after the first one on a new box.
#
# The AlgoTune checkout goes as a git BUNDLE -- one file, full history, `git clone <bundle>` restores
# it with our commit intact. That keeps the provenance a published number needs (upstream sha +
# ours) instead of a directory of files nobody can date.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
# WHICH TREES HOLD MEASUREMENTS is answered in one place, shared with `snapshot_timer.sh`. Two
# copies of that question is how `camp-runs/` came to be archived by neither -- see bench_trees.sh.
. "$HERE/bench_trees.sh"

SRC="${BENCH_ROOT:-/var/tmp/looplab-bench}"
# THE DESTINATION IS A VARIABLE, like the source. `$BENCH_ROOT` has always moved this script's
# SOURCE; nothing moved its DESTINATION, which fell back to the hardcoded persistent path whatever
# `$BENCH_ROOT` said. Both callers invoke this script with no argument (`snapshot_timer.sh` in its
# `_loop`, `campaign.sh` after an arm), so on both paths the fallback was the only destination
# reachable, and `grep -rn SNAPSHOT_DEST` over the tree returned nothing at all.
#
# COST, 2026-08-31: an agent started `snapshot_timer.sh` against a synthetic `BENCH_ROOT` to test
# it. The timer honoured `BENCH_ROOT` for what it read and ignored it for where it wrote, so the
# cycle deposited a snapshot of a fake box into the LIVE rotation on the persistent mount, beside
# the real ones, and it had to be identified and deleted by hand. Nothing in the snapshot said
# where it came from -- see PROVENANCE.txt below, which now records the root it was taken from for
# exactly that reason.
#
# Precedence is argument, then environment, then the box default: a caller that knows says so, a
# box profile (`box-jhub-l40s.sh`) declares the machine's persistent path beside its other
# machine-specific facts, and a bare invocation on this box still does what it always did.
DEST="${1:-${SNAPSHOT_DEST:-/home/jovyan/data/looplab-bench/snapshots}}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DEST/$STAMP"
mkdir -p "$OUT" || { echo "cannot create $OUT"; exit 1; }

echo "snapshot $SRC -> $OUT"

SHORT=0

# 1. The patched third-party checkout, as a bundle with history.
if [ -d "$SRC/AlgoTune/.git" ]; then
  ( cd "$SRC/AlgoTune" && git bundle create "$OUT/AlgoTune.bundle" --all 2>/dev/null ) \
    && echo "  AlgoTune.bundle       $(du -h "$OUT/AlgoTune.bundle" | cut -f1)  ($(cd "$SRC/AlgoTune" && git log --oneline -1))" \
    || echo "  AlgoTune.bundle       FAILED (shallow clone?); falling back to a tar of tracked files"
  if [ ! -s "$OUT/AlgoTune.bundle" ]; then
    ( cd "$SRC/AlgoTune" && git ls-files -z | tar --null -T - -czf "$OUT/AlgoTune-tracked.tar.gz" ) \
      && echo "  AlgoTune-tracked.tar.gz  $(du -h "$OUT/AlgoTune-tracked.tar.gz" | cut -f1)"
  fi
  ( cd "$SRC/AlgoTune" && git log --oneline -3 > "$OUT/AlgoTune-HEAD.txt"; git status --porcelain \
      > "$OUT/AlgoTune-dirty.txt" )
fi

# 1b. OUR OWN repo, as a bundle, for exactly the same reason.
#
# Measured 2026-08-30, from the wreck. `PROVENANCE.txt` had faithfully recorded
# `looplab:  af0e4772 ... (0 dirty files)` at 19:11 on 2026-08-29. Four minutes later the container
# restarted; `/var/tmp` -- the container's own writable layer, where BENCH_ROOT lives -- came back
# empty, and that sha named an object no surviving repository contained. Thirty-seven commits made
# between 07:11 and 19:06 that day went with it: five code fixes with their falsifying tests, two of
# them still awaiting acceptance by a probe that was running at the time, and twenty-four sections
# of docs/56. The published branch stopped at 07:11 because pushing is something a person remembers
# to do and a snapshot is something that runs every thirty minutes.
#
# A sha is not a backup. It is a RECEIPT for a backup this script was not taking. The third-party
# checkout above had been bundled since the first version; our own repo -- the one whose loss
# actually costs work -- was only ever named. The uncommitted tree goes along as a patch, because
# "(0 dirty files)" is a claim worth being able to CHECK, and a dirty tree is worth being able to
# restore.
if [ -d "$SRC/looplab/.git" ]; then
  ( cd "$SRC/looplab" && git bundle create "$OUT/looplab.bundle" --all 2>/dev/null ) \
    && echo "  looplab.bundle        $(du -h "$OUT/looplab.bundle" | cut -f1)  ($(cd "$SRC/looplab" && git log --oneline -1))" \
    || { echo "  BUNDLE FAILED        looplab -- OUR COMMITS ARE NOT IN THIS SNAPSHOT"; SHORT=$((SHORT + 1)); }
  ( cd "$SRC/looplab" && git log --oneline -3 > "$OUT/looplab-HEAD.txt"
    git status --porcelain > "$OUT/looplab-dirty.txt"
    git diff HEAD > "$OUT/looplab-uncommitted.patch" ) 2>/dev/null
else
  echo "  MISSING              looplab.bundle -- $SRC/looplab/.git absent, so NO commit of ours is archived"
  SHORT=$((SHORT + 1))
fi

# 2. Measurements. These are the irreplaceable half.
#
# A MISSING SOURCE IS REPORTED, NOT SKIPPED. `copy` used to `return 0` on a path that was not there,
# so a snapshot that copied nothing at all still printed PROVENANCE.txt and exited 0, and
# `campaign.sh`'s `|| echo "(snapshot failed...)"` could never fire. An archive that is silently
# empty is worse than no archive: it is one somebody will restore from.
# A FAILED COPY COUNTS THE SAME AS A MISSING SOURCE, and until 2026-08-25 only the second did. The
# `cp` discarded its stderr and its status: the `&& echo` printed nothing, the counter stayed 0, the
# completeness check below passed and the script exited 0 -- so `campaign.sh`'s
# `|| echo "(snapshot failed...)"` arm could never fire for it and `snapshot_timer.sh` recorded the
# fingerprint as archived. The destination is the geesefs S3 FUSE mount, where a transient error or
# an ENOSPC part-way through a recursive copy is the ORDINARY failure, not an exotic one -- and it
# produces exactly the artifact this header calls the worst outcome. `cp`'s own stderr is kept for
# the same reason: "COPY FAILED" without the errno sends the operator back to the mount to guess.
copy() {  # $1 = path under $SRC (or absolute), $2 = label
  if [ ! -e "$1" ]; then
    echo "  MISSING              $2 -- $1 does not exist, so it is NOT in this snapshot"
    SHORT=$((SHORT + 1)); return 0
  fi
  if cp -r "$1" "$OUT/"; then
    echo "  $2 $(du -sh "$OUT/$(basename "$1")" | cut -f1)"
  else
    echo "  COPY FAILED          $2 -- cp exited non-zero (see its error above); this snapshot is"
    echo "                       SHORT of $1 and whatever it managed to write is PARTIAL"
    SHORT=$((SHORT + 1))
  fi
}
copy "$SRC/AlgoTune/reports"                     "reports              "
copy "$SRC/looplab/benchmarks/algotune/.baseline_times" "baseline_times       "
copy "$SRC/meter"                                "meter                "
copy "$SRC/logs"                                 "logs                 "

# EVERY campaign directory, DISCOVERED, because the name is the operator's (`CAMPAIGN_OUT`) and this
# script used to hardcode one of them. Measured 2026-08-23: the snapshot taken at 03:21 that morning
# held a directory called `campaign` containing arm A's 102 markers and logs from a run that FINISHED
# on 2026-08-20, and held nothing at all from `campaign-paired/` -- the live campaign, whose 17
# `.done` markers and 19 `B-*.final.json` scores were the entire arm-B result set and the one thing
# on the box that cannot be recomputed. So the archive was 84 MB of a bundle that regenerates from
# git, beside a stale copy of a dead campaign, under a header saying "everything that cannot be
# regenerated". The failure needs no bug: a campaign is pointed at a new CAMPAIGN_OUT and the
# hardcoded name goes quietly out of date.
FOUND_CAMPAIGN=0
while IFS= read -r D; do
  [ -n "$D" ] || continue
  FOUND_CAMPAIGN=$((FOUND_CAMPAIGN + 1))
  copy "$D" "$(printf '%-21s' "$(basename "$D")")"
done < <(bench_campaign_trees "$SRC")

# EVERY tree of RUNS -- where a run's `events.jsonl` and `spans.jsonl` actually live. DISCOVERED,
# by what a directory holds and by the operator's own `$CAMPAIGN_RUNS`, never by a pattern over
# names; see bench_trees.sh for the two times a pattern went stale and what each cost.
#
# The second of those is why this paragraph was rewritten. `campaign.sh:52` writes every task-arm's
# run to `$CAMPAIGN_RUNS/<task>/run/events.jsonl` -- `camp-runs/` on this box -- and the glob here
# was `runs-* model-probes probes`, which that name matches nowhere: `grep -c camp-runs` over this
# file returned 0. The campaign path had the SAME hole the probe path had on 2026-08-29, still open,
# and worse: `campaign.sh:1078` does `rm -rf "$TASK_ROOT"` at the head of every attempt, so a retry
# destroys the previous attempt's evidence without any container restart being involved.
#
# This is the rawest measurement on the box: what the loop proposed, what each call cost, which node
# became champion and why. docs/56 is written FROM these, and until 2026-08-30 not one byte of them
# was archived. The 2026-08-29 restart took sixty-nine runs and about $100 of metered spend with it
# and left every conclusion drawn from them uncheckable -- the campaign markers survived, the
# evidence behind them did not.
#
# Copied ONCE, not once per snapshot. A finished run is immutable, so eight rotating copies would be
# seven copies of the same bytes on a shared S3-backed mount. They accumulate in a sibling archive
# the prune below never touches, and each snapshot records what the archive held at its moment.
# `cp -ru` keeps the sync incremental, which matters because a LIVE run's directory grows while this
# script is reading it.
#
# BUT `-u` CANNOT REPAIR WHAT IT ONCE COPIED SHORT, and on this mount copying short is the ordinary
# failure. `-u` copies when the SOURCE is newer than the destination, and a destination truncated by
# an ENOSPC part-way through today's cycle carries TODAY's mtime -- newer than the frozen mtime of
# the finished run it is supposed to be a copy of. So `-u` skips it for ever, and every later cycle
# reports success over it.
#
# Driven end to end 2026-08-31 on a 200,000-line events.jsonl with `ulimit -f` standing in for the
# ENOSPC: the cycle that failed left 33,390 lines in the archive; the next cycle, which exited 0 and
# printed the file as an archived run, left 33,390 lines. The manifest counted it as a whole run,
# because the manifest counts FILES BY NAME.
#
# So the repair asks the only question `-u` cannot: is the archived file SHORTER than its source?
# These are append-only logs, so shorter means unfinished, and `cp -ru` has already handled every
# case where the source is merely newer. The incrementality the comment above defends is untouched:
# a finished run whose archived copy is the same length (or longer -- see below) is not re-read.
#
# The verify is separate from the repair on purpose. `cp` exiting 0 is not the claim the archive
# needs; "the bytes are there" is, and until now nothing checked it, which is how a truncated run
# spent a day being reported as archived.
RUNS_ARCHIVE="${SNAPSHOT_RUNS_ARCHIVE:-$DEST/../runs-archive}"
archive_tree() {  # $1 = source tree, $2 = archive root. Sets ARCH_REPAIRED / ARCH_STILL_SHORT.
  local S="$1" A="$2" B rel ssz dsz rc=0
  B="$(basename "$S")"
  ARCH_REPAIRED=0; ARCH_STILL_SHORT=0
  mkdir -p "$A" || return 1
  cp -ru "$S" "$A/" || rc=1
  while IFS= read -r -d '' rel; do
    # The source can vanish mid-walk -- `campaign.sh` rm -rf's a task root to start an attempt --
    # and a file that is no longer there is not a shortfall in the archive.
    ssz=$(stat -c %s "$S/$rel" 2>/dev/null) || continue
    dsz=$(stat -c %s "$A/$B/$rel" 2>/dev/null || echo -1)
    [ "$dsz" -lt "$ssz" ] || continue
    # LONGER than the source is left alone: the box's writable layer is where a run gets deleted or
    # restarted, and the archive is the durable half. Only SHORT is a defect.
    if cp -p "$S/$rel" "$A/$B/$rel"; then ARCH_REPAIRED=$((ARCH_REPAIRED + 1)); else rc=1; fi
    dsz=$(stat -c %s "$A/$B/$rel" 2>/dev/null || echo -1)
    if [ "$dsz" -lt "$ssz" ]; then ARCH_STILL_SHORT=$((ARCH_STILL_SHORT + 1)); rc=1; fi
  done < <(find "$S" -type f -printf '%P\0' 2>/dev/null)
  return $rc
}
FOUND_RUNS=0
while IFS= read -r D; do
  [ -n "$D" ] || continue
  FOUND_RUNS=$((FOUND_RUNS + 1))
  B="$(basename "$D")"
  if archive_tree "$D" "$RUNS_ARCHIVE"; then
    N=$(find "$RUNS_ARCHIVE/$B" -name events.jsonl 2>/dev/null | wc -l)
    R=""
    [ "$ARCH_REPAIRED" -gt 0 ] && R=", $ARCH_REPAIRED re-copied SHORT of its source"
    echo "  runs -> archive       $B $(du -sh "$RUNS_ARCHIVE/$B" 2>/dev/null | cut -f1) ($N run records$R)"
    echo "$B $N $RUNS_ARCHIVE/$B" >> "$OUT/runs-manifest.txt"
  else
    echo "  COPY FAILED          $B -- the per-run events and spans are NOT archived"
    if [ "$ARCH_STILL_SHORT" -gt 0 ]; then
      echo "                       $ARCH_STILL_SHORT file(s) in the archive are SHORTER than the"
      echo "                       source they claim to copy -- that is a TRUNCATED run, and the"
      echo "                       manifest above counts files by name and cannot see it"
    fi
    SHORT=$((SHORT + 1))
  fi
done < <(bench_run_trees "$SRC")
# WHICH MODE THE BOX IS IN IS NOT A SHORTFALL. Campaigns and probe runs are two INDEPENDENT ways of
# measuring here, and either can be absent because it is simply not in use.
#
# This was got wrong twice in two days, in opposite directions, and both times the cost was real.
# First the campaign check alone made every cycle on a freshly rebuilt box exit 1; the timer then
# refused to record the fingerprint -- correctly, by its own rule -- and re-wrote a 110 MB snapshot
# every thirty minutes without pruning, since the prune sits downstream of the completeness check.
# Nine snapshots and 3.0 GB. Making the claim conditional on "neither exists" fixed that case and
# broke the next one within the hour: two probes started, `model-probes/` appeared, `campaign*` did
# not, and the same unbounded loop resumed under a new name.
#
# So the question this block used to ask -- "is every mode present?" -- is the wrong question. What
# the archive owes is everything that EXISTS, and `copy` above already counts a source that is there
# and could not be read. An absent MODE is reported, because an operator reading a restore should
# know which of the two this box was doing, but it is not a shortfall.
#
# What this does NOT weaken: the 2026-08-29 loss was never an absence this could have caught. The
# runs were present the whole time and simply not copied, because no line of the script knew they
# existed. That is fixed by the copying, not by an alarm.
if [ "$FOUND_CAMPAIGN" = 0 ] && [ "$FOUND_RUNS" = 0 ]; then
  echo "  (idle box: neither $SRC/campaign* nor a run tree -- nothing has been measured here yet.)"
elif [ "$FOUND_CAMPAIGN" = 0 ]; then
  echo "  (no $SRC/campaign* -- this box is running probes, not a campaign. Not a shortfall.)"
elif [ "$FOUND_RUNS" = 0 ]; then
  echo "  (no run tree under $SRC holds an events.jsonl -- campaign markers only, no run logs yet.)"
fi

# 3. Which commit of OUR repo produced them, and what the box looked like.
{
  echo "snapshot $STAMP"
  # WHICH BOX, AND WHICH ROOT ON IT. A snapshot that does not name its source cannot be told apart
  # from somebody else's, and on 2026-08-31 that was not hypothetical: a snapshot of a synthetic
  # BENCH_ROOT landed in this box's live rotation and the only way to identify it was to read what
  # was inside. The restorer's first question is "is this mine?", and until now the archive had no
  # answer to it.
  echo "bench root: $SRC   (on $(hostname 2>/dev/null || echo '?'))"
  echo "destination: $OUT"
  echo "looplab:  $(cd "$SRC/looplab" && git log --oneline -1) ($(cd "$SRC/looplab" && git status --porcelain | wc -l) dirty files)"
  echo "AlgoTune: $(cd "$SRC/AlgoTune" && git log --oneline -1)"
  echo "runs archive: $RUNS_ARCHIVE (not pruned; see runs-manifest.txt for what it held)"
  echo "nproc $(nproc) | cpu.max $(cat /sys/fs/cgroup/cpu.max 2>/dev/null) | free $(free -g | awk '/Mem:/{print $7}')G"
} > "$OUT/PROVENANCE.txt"
cat "$OUT/PROVENANCE.txt"

# The EXIT CODE is the claim. Anything this snapshot could not copy -- absent, or attempted and
# failed -- makes it a partial one, and the caller (`campaign.sh`, `snapshot_timer.sh`) is the only
# place left that can say so out loud.
#
# ASKED BEFORE THE PRUNE, deliberately. The prune deletes the OLDEST snapshot to make room for this
# one, and doing that first traded a complete archive for an incomplete one -- on a mount where the
# failure that produces an incomplete one (ENOSPC) is also the failure that makes the prune look
# like the fix.
if [ "$SHORT" -gt 0 ]; then
  echo "  INCOMPLETE SNAPSHOT: $SHORT source(s) missing or unwritable (listed above). Do not restore"
  echo "  from this one without checking what it is short of, and NOTHING has been pruned to make"
  echo "  room for it -- the older, complete snapshots are still there."
  exit 1
fi

# Keep the last N snapshots; the measurements accumulate and the mount is shared. But AGE IS NOT
# WORTH, and this prune used to act as if it were.
#
# Found 2026-08-31, one restart too late to be theoretical. `ls | head -n -KEEP` deletes the OLDEST
# directories, full stop. On this box the oldest were the eight taken on 2026-08-29 -- the only ones
# holding `campaign-final/` (twenty task-arms, both arms, the finished paired campaign) and the
# meter ledgers. Every snapshot taken after the container restart holds two git bundles and nothing
# measured, because nothing has been measured here since. Nine of those would have arrived within
# five hours, and the prune would have traded the irreplaceable for the reproducible, silently, as
# ordinary successful operation. The bundles regenerate from git in a minute; the campaign does not
# regenerate at all.
#
# So worth is asked about, not assumed. A snapshot is MEASURED if it carries a campaign directory or
# a non-empty runs manifest; unmeasured ones are spent first, oldest among them going first, and the
# newest snapshot is never a candidate whatever it holds -- it is the current state of the box.
# Only if that is not enough does the prune reach a measured snapshot, and then it says so out loud,
# because deleting one is a real loss rather than housekeeping.
KEEP="${SNAPSHOT_KEEP:-8}"
_measured() {  # $1 = snapshot dir -- does it hold anything that cannot be recomputed?
  # ONLY what the snapshot itself carries counts, and the runs manifest is not that. The first
  # version also accepted a manifest line with a count above zero, which read as "this snapshot has
  # runs behind it". It does not: that count is `find "$RUNS_ARCHIVE/$B" -name events.jsonl | wc -l`
  # over the EXTERNAL archive, which is cumulative and which the prune never touches. So the moment
  # one probe run existed anywhere, every later snapshot answered "measured", the worth ordering
  # collapsed back into plain oldest-first, and the sentence reserved for real loss printed on every
  # routine cycle -- observed in this box's own timer log on 2026-08-31, three cycles running.
  # The manifest is a RECEIPT ABOUT ANOTHER STORE; losing this snapshot loses none of those runs.
  compgen -G "$1/campaign*" > /dev/null && return 0
  return 1
}
ALL=$(ls -1d "$DEST"/2* 2>/dev/null | sort)
TOTAL=$(printf '%s\n' "$ALL" | grep -c . )
OVER=$((TOTAL - KEEP))
if [ "$OVER" -gt 0 ]; then
  NEWEST=$(printf '%s\n' "$ALL" | tail -1)
  UNMEASURED=""; MEASURED=""
  for D in $ALL; do
    [ "$D" = "$NEWEST" ] && continue
    if _measured "$D"; then MEASURED="$MEASURED $D"; else UNMEASURED="$UNMEASURED $D"; fi
  done
  for D in $UNMEASURED $MEASURED; do
    [ "$OVER" -gt 0 ] || break
    if _measured "$D"; then
      echo "  pruning $D  -- WITH MEASUREMENTS: every unmeasured snapshot was already spent"
    else
      echo "  pruning $D  (carries no campaign and no runs; the checkouts in it regenerate from git)"
    fi
    rm -rf "$D"; OVER=$((OVER - 1))
  done
fi
exit 0
