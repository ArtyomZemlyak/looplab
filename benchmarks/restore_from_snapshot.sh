#!/bin/bash
{
# Restore the bench checkouts from a snapshot, choosing one that is FINISHED.
#
#   restore_from_snapshot.sh <destination-dir> [snapshots-root]
#
# WHY THIS EXISTS AS A SCRIPT. Every sweep asserts "the bundle is verified by restoring it" and the
# verification has been a person typing `git clone` by hand. On 2026-09-01 22:04:30 that person read
# the NEWEST snapshot mid-write and saw one artefact of two; the harmless direction. The costly one
# is the same read taken for a finished snapshot: `git bundle create` writes straight to its final
# path, so a bundle caught mid-write is TRUNCATED, and the directory looks exactly like every other.
#
# `.complete` was added the same day (9f72d32c) -- written last, and only past snapshot.sh's
# shortfall check. It had no reader until this. A marker nobody consults is documentation, not a
# guard, which is the shape this bench keeps finding in its own work.
#
# WHAT THIS DOES NOT DO: touch the live tree. It restores INTO a fresh directory and says what it
# found. Deciding to swap it in is the operator's, and doing it silently is how a half-restore
# replaces a working checkout.
set -u

DEST="${1:?usage: restore_from_snapshot.sh <destination-dir> [snapshots-root]}"
ROOT="${2:-${SNAPSHOT_DEST:-/home/jovyan/data/looplab-bench/snapshots}}"

[ -d "$ROOT" ] || { echo "no snapshots root at $ROOT" >&2; exit 2; }

# NEWEST COMPLETE, not newest. Skipped ones are named: "there were none" and "there were three and
# all were unfinished" call for different actions, and a script that prints one for the other is the
# reason this file has a header.
PICK=""; SKIPPED=""
while IFS= read -r d; do
  if [ -f "$d/.complete" ]; then PICK="$d"; break; fi
  SKIPPED="$SKIPPED $(basename "$d")"
done < <(ls -1dt "$ROOT"/2*/ 2>/dev/null)

[ -n "$SKIPPED" ] && echo "skipped as unfinished (no .complete):$SKIPPED"
[ -n "$PICK" ] || { echo "NO COMPLETE SNAPSHOT under $ROOT -- nothing here is safe to restore from" >&2; exit 1; }
echo "restoring from $(basename "$PICK")  (marked complete $(cat "$PICK/.complete"))"

mkdir -p "$DEST" || exit 1
RC=0
for NAME in looplab AlgoTune; do
  B="$PICK/$NAME.bundle"
  if [ ! -s "$B" ]; then echo "  $NAME: no bundle in this snapshot"; RC=1; continue; fi
  OUT="$DEST/$NAME"
  [ -e "$OUT" ] && { echo "  $NAME: $OUT already exists -- refusing to write over it"; RC=1; continue; }
  if ! git clone -q "$B" "$OUT" 2>/dev/null; then
    echo "  $NAME: BUNDLE WILL NOT CLONE -- this snapshot cannot restore it"; RC=1; continue
  fi
  GOT=$(cd "$OUT" && git rev-parse HEAD 2>/dev/null)
  # WHAT THE SNAPSHOT SAID ITS HEAD WAS, compared with what came out. A clone that succeeds onto the
  # wrong branch is the documented failure this checks for; the bundle carries several.
  #
  # COMPARED AT THE RECORDED LENGTH. The first version cut the clone's sha to twelve and the
  # snapshot writes `git log --oneline`, which abbreviates to seven or eight -- so every restore
  # reported WRONG TREE against a tree that was right. Caught by running it, not by reading it.
  WANT=$(head -1 "$PICK/$NAME-HEAD.txt" 2>/dev/null | awk '{print $1}')
  if [ -n "$WANT" ] && [ "${GOT:0:${#WANT}}" != "$WANT" ]; then
    echo "  $NAME: restored ${GOT:0:12} but the snapshot recorded $WANT -- WRONG TREE"; RC=1; continue
  fi
  echo "  $NAME: ${GOT:0:12} on $(cd "$OUT" && git branch --show-current)  ($(cd "$OUT" && git ls-tree -r HEAD --name-only | wc -l) tracked files)"
done
[ "$RC" = 0 ] && echo "restore OK into $DEST" || echo "restore INCOMPLETE -- see above"
exit "$RC"
}
