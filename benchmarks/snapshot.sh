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
# costs more than recreating it.
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
SHORT=0
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
if [ "$FOUND_CAMPAIGN" = 0 ]; then
  echo "  MISSING              no $SRC/campaign* directory -- NO campaign markers or scores archived"
  SHORT=$((SHORT + 1))
fi

# 3. Which commit of OUR repo produced them, and what the box looked like.
{
  echo "snapshot $STAMP"
  echo "looplab:  $(cd "$SRC/looplab" && git log --oneline -1) ($(cd "$SRC/looplab" && git status --porcelain | wc -l) dirty files)"
  echo "AlgoTune: $(cd "$SRC/AlgoTune" && git log --oneline -1)"
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

# Keep the last N snapshots; the measurements accumulate and the mount is shared.
KEEP="${SNAPSHOT_KEEP:-8}"
ls -1d "$DEST"/2* 2>/dev/null | head -n -"$KEEP" | while read -r old; do
  echo "  pruning $old"; rm -rf "$old"
done
exit 0
