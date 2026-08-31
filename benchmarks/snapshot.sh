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

SRC="${BENCH_ROOT:-/var/tmp/looplab-bench}"
DEST="${1:-/home/jovyan/data/looplab-bench/snapshots}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DEST/$STAMP"
mkdir -p "$OUT" || { echo "cannot create $OUT"; exit 1; }

echo "snapshot -> $OUT"

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
for D in "$SRC"/campaign*; do
  [ -d "$D" ] || continue
  FOUND_CAMPAIGN=$((FOUND_CAMPAIGN + 1))
  copy "$D" "$(printf '%-21s' "$(basename "$D")")"
done

# EVERY tree of finished RUNS -- where a probe's `events.jsonl` and `spans.jsonl` actually live.
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
RUNS_ARCHIVE="${SNAPSHOT_RUNS_ARCHIVE:-$DEST/../runs-archive}"
FOUND_RUNS=0
for D in "$SRC"/runs-* "$SRC"/model-probes "$SRC"/probes; do
  [ -d "$D" ] || continue
  FOUND_RUNS=$((FOUND_RUNS + 1))
  B="$(basename "$D")"
  if mkdir -p "$RUNS_ARCHIVE" && cp -ru "$D" "$RUNS_ARCHIVE/"; then
    N=$(find "$RUNS_ARCHIVE/$B" -name events.jsonl 2>/dev/null | wc -l)
    echo "  runs -> archive       $B $(du -sh "$RUNS_ARCHIVE/$B" 2>/dev/null | cut -f1) ($N run records)"
    echo "$B $N $RUNS_ARCHIVE/$B" >> "$OUT/runs-manifest.txt"
  else
    echo "  COPY FAILED          $B -- the per-run events and spans are NOT archived"
    SHORT=$((SHORT + 1))
  fi
done
# AN IDLE BOX IS NOT A SHORTFALL, and telling them apart is the difference between a signal and a
# nuisance that fills a shared mount.
#
# Measured 2026-08-31, by starting the timer on a freshly rebuilt box: with no campaign and no runs
# yet, every cycle exited 1, `snapshot_timer.sh` refused to record the fingerprint ("an incomplete
# archive is not done"), and it therefore re-wrote a 110 MB snapshot every thirty minutes and never
# pruned -- because the prune is deliberately downstream of the completeness check. Nine snapshots
# and 3.0 GB accumulated before this was noticed. The campaign half of that behaviour predates the
# run half; adding a second always-missing source is what made it visible.
#
# So the claim is made conditional on the situation, which is what makes it a claim at all: if
# NEITHER a campaign nor a run tree exists, nothing has been measured on this box yet and the
# snapshot is complete for what there is. If EITHER exists, the other's absence is a real shortfall
# -- and that is exactly the 2026-08-29 shape, where campaign-final/ survived and the sixty-nine
# runs behind its numbers did not.
if [ "$FOUND_CAMPAIGN" = 0 ] && [ "$FOUND_RUNS" = 0 ]; then
  echo "  (idle box: no $SRC/campaign* and no run tree -- nothing has been measured here yet, so"
  echo "   this snapshot is short of nothing. It carries both checkouts and whatever else exists.)"
else
  if [ "$FOUND_CAMPAIGN" = 0 ]; then
    echo "  MISSING              no $SRC/campaign* directory -- NO campaign markers or scores archived"
    SHORT=$((SHORT + 1))
  fi
  if [ "$FOUND_RUNS" = 0 ]; then
    echo "  MISSING              no $SRC/runs-*, model-probes or probes tree -- NO per-run evidence archived"
    SHORT=$((SHORT + 1))
  fi
fi

# 3. Which commit of OUR repo produced them, and what the box looked like.
{
  echo "snapshot $STAMP"
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
  compgen -G "$1/campaign*" > /dev/null && return 0
  # runs-manifest.txt lines are "<tree> <count> <archive path>"; a count of 0 means the tree was
  # there and empty, which is not evidence of anything.
  [ -s "$1/runs-manifest.txt" ] \
    && awk '{ if ($2 + 0 > 0) f = 1 } END { exit !f }' "$1/runs-manifest.txt" && return 0
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
