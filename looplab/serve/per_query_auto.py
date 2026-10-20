"""Import terminal candidates' per-query sidecars through the ordinary upload writer (doc 82 П7).

The UI's existing event pump calls this AFTER folding terminals, including at startup: no engine
thread imports the server, no new watcher, and a replay is harmless. Files remain candidate-authored
workdir evidence; neither metadata nor a matching mean authenticates their author. A sidecar
`nodes/node_N/per_query/<name>.json` opts in with `metrics`, beside `<name>.npz`. Only upload-schema
options below are accepted. A manual upload of the same source for this lifecycle wins.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from fastapi import HTTPException
from pydantic import ValidationError

from looplab.core.node_evidence import node_workdir, read_bounded_regular_file
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token

_log = logging.getLogger("looplab.server")
OPTIONS = frozenset({"metrics", "query_key", "cluster_key", "test_label", "experiment_id", "arm",
                     "measured_check"})


def import_terminal_arrays(srv, rd: Path) -> list[dict]:
    """Import once per lifecycle/file, with lifecycle-fenced publication so a reset cannot retag bytes."""
    from looplab.harness.per_query import MAX_UPLOAD_BYTES, publish_upload, read_manifest
    from looplab.harness.journals import read_event_source
    from looplab.serve.routers.campaign import PerQueryUploadBody

    imported = []
    # The upload writer owns the sequencer; a lifecycle expectation is checked under its lock.
    events = read_event_source(rd)
    state = fold(events)
    generation = run_generation_token(events)
    rows, health = read_manifest(rd)
    if not health["read_complete"]:
        return []  # the ordinary writer refuses too; never repair a damaged journal here
    for node in state.nodes.values():
        if (node.tombstoned or node.status.value not in ("evaluated", "failed")
                or node.terminal_event_seq is None):
            continue
        wd = node_workdir(rd, node.id)
        if wd is None:
            continue
        directory = wd / "per_query"
        if directory.is_symlink() or not directory.is_dir():
            continue
        for sidecar in sorted(directory.glob("*.json"))[:64]:
            try:
                action = (f"auto-pq:{node.id}:{node.attempt}:" +
                          hashlib.sha256(sidecar.name.encode()).hexdigest()[:24])
                if any(r.get("run_uid") == state.run_uid and r.get("action_id") == action for r in rows):
                    continue  # completed imports need no repeated NPZ read on a busy event pump
                raw = read_bounded_regular_file(sidecar, 256 * 1024)
                if raw is None:
                    continue
                options = json.loads(raw)
                if not isinstance(options, dict) or not options.get("metrics"):
                    continue  # without a declaration, arrays alone imply nothing
                if set(options) - OPTIONS:
                    raise ValueError("unknown per-query sidecar option")
                array_file = sidecar.with_suffix(".npz")
                data = read_bounded_regular_file(array_file, MAX_UPLOAD_BYTES)
                if data is None:
                    continue
                sha = hashlib.sha256(data).hexdigest()
                if any(r.get("run_uid") == state.run_uid and r.get("node_id") == node.id
                       and r.get("node_generation") == node.attempt
                       and (r.get("action_id") == action or
                            (r.get("source", {}).get("sha256") == sha
                             and r.get("experiment_id") == options.get("experiment_id")
                             and r.get("arm") == options.get("arm"))) for r in rows):
                    continue
                body = PerQueryUploadBody(expected_generation=generation, action_id=action,
                                          node_id=node.id,
                                          workdir_path=f"per_query/{array_file.name}", **options)
                result = publish_upload(srv, rd, body, agent_token=False,
                                            auto_attempt=node.attempt)
                imported.append(result["upload"])
                rows.append(result["upload"])
            except (HTTPException, OSError, ValueError, ValidationError) as exc:
                _log.warning("per-query auto-import refused for node %s (%s): %s",
                             node.id, sidecar.name, type(exc).__name__)
    return imported
