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
copy() {  # $1 = path under $SRC (or absolute), $2 = label
  [ -e "$1" ] || return 0
  cp -r "$1" "$OUT/" 2>/dev/null && echo "  $2 $(du -sh "$OUT/$(basename "$1")" | cut -f1)"
}
copy "$SRC/AlgoTune/reports"                     "reports              "
copy "$SRC/looplab/benchmarks/algotune/.baseline_times" "baseline_times       "
copy "$SRC/campaign"                             "campaign             "
copy "$SRC/meter"                                "meter                "
copy "$SRC/logs"                                 "logs                 "

# 3. Which commit of OUR repo produced them, and what the box looked like.
{
  echo "snapshot $STAMP"
  echo "looplab:  $(cd "$SRC/looplab" && git log --oneline -1) ($(cd "$SRC/looplab" && git status --porcelain | wc -l) dirty files)"
  echo "AlgoTune: $(cd "$SRC/AlgoTune" && git log --oneline -1)"
  echo "nproc $(nproc) | cpu.max $(cat /sys/fs/cgroup/cpu.max 2>/dev/null) | free $(free -g | awk '/Mem:/{print $7}')G"
} > "$OUT/PROVENANCE.txt"
cat "$OUT/PROVENANCE.txt"

# Keep the last N snapshots; the measurements accumulate and the mount is shared.
KEEP="${SNAPSHOT_KEEP:-8}"
ls -1d "$DEST"/2* 2>/dev/null | head -n -"$KEEP" | while read -r old; do
  echo "  pruning $old"; rm -rf "$old"
done
