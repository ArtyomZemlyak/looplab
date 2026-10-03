"""Effective harness obligations require a configuration this server understands.

The ordinary config view stays a lenient diagnostic read. Bootstrap reads are
different: their settings decide gates/next actions. Dropping an unknown field
there can claim permission or empty obligations after a server downgrade.
No snapshot repair, schema migration or engine restart happens here.
"""
from fastapi import HTTPException

from looplab.core.config import (ConfigSnapshotUnreadableError, ConfigSnapshotVersionError,
                                 read_config_snapshot)


def read_harness_settings(rd):
    try:
        return read_config_snapshot(rd / "config.snapshot.json", refuse_unknown=True)
    except ConfigSnapshotVersionError as exc:
        # Unknown keys can themselves contain secrets. Report the source and
        # recovery, never exception text or a list of operator-controlled keys.
        raise HTTPException(503, {"code": "harness_config_incompatible", "source": "config.snapshot.json",
            "message": "This server build cannot interpret config.snapshot.json. Ask the operator to verify the "
                       "server version and use a compatible LoopLab build; preserve the snapshot and archives. "
                       "Do not remove unknown settings to bypass obligations. Refresh bootstrap reads after repair; "
                       "no retry, training or resume was started"}) from exc
    except ConfigSnapshotUnreadableError as exc:
        raise HTTPException(503, {"code": "harness_config_unavailable", "source": "config.snapshot.json",
            "message": "config.snapshot.json is missing, unreadable or invalid. Ask the operator to inspect and "
                       "restore the original run snapshot, then refresh bootstrap reads. "
                       "No retry, training or resume was started"}) from exc
