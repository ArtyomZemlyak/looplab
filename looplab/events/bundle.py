"""The reviewer bundle: one run's seeds, traces, code, claims and record, packaged as an RO-Crate
(doc 52 row 23; the survey's 38 % dimension, the field's export).

Seeds (`LOOPLAB_EVAL_SEED`, `confirm_seed_base`) and traces (`events.jsonl`, `spans.jsonl`) existed
on disk and nothing packaged them with the code and the claims for a reviewer. `export_bundle`
copies the run's OWN record — never a re-derivation of it — into one directory and describes every
file in `ro-crate-metadata.json` (RO-Crate 1.1: a JSON-LD graph with the root dataset, one `File`
entity per member carrying its size and SHA-256), so a reviewer can check what they were handed
against what the run wrote:

  * `events.jsonl` — the authoritative log; `spans.jsonl` — the trace, when present;
  * `config.snapshot.json` / `task.snapshot.json` — what the run was launched with;
  * `champion/` — the champion's committed code (`solution.py` + every file), off the FOLD, so it
    is the code the record says won, not whatever the workdir holds now;
  * `claims.json` — every research memo's claims with their evidence ids, and the plan;
  * `summary.json` — the run row a reviewer reads first: the number, `best_metric_caveats`, the
    Mislead pair, the seeds, the champion's official report and the extras sidecars when present;
  * `nodes/node_<id>/mlebench_report.json`, `mlebench_extras.json`, `bait_audit.json` — the graded
    report and the audit sidecars, copied when the run has them.

What is deliberately NOT in the bundle: node workdirs (data mounts and checkpoints are the box's,
and the code is already here), the cross-run stores (they are not this run's record), and anything
re-computed at export time beyond the summary row — a bundle is evidence, and evidence that was
derived at packaging time cannot be checked against the log.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

RO_CRATE_CONTEXT = "https://w3id.org/ro/crate/1.1/context"
RO_CRATE_METADATA = "ro-crate-metadata.json"
BUNDLE_VERSION = 1
_SIDECARS = ("mlebench_extras.json", "bait_audit.json")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _encoding(name: str) -> str:
    if name.endswith(".jsonl"):
        return "application/x-ndjson"
    if name.endswith(".json"):
        return "application/json"
    if name.endswith(".py"):
        return "text/x-python"
    return "text/plain"


def bundle_summary(run_dir: Path, state, events) -> dict:
    """The run row a reviewer reads first, from the same derivations the server publishes."""
    from looplab.engine.champion_caveats import champion_metric_caveats, mislead_gap

    best = state.best()
    seeds: dict = {"confirm_seed_base": None, "LOOPLAB_EVAL_SEED": None}
    try:
        snap = json.loads((run_dir / "config.snapshot.json").read_text(encoding="utf-8"))
        if isinstance(snap, dict):
            seeds["confirm_seed_base"] = snap.get("confirm_seed_base")
            env = snap.get("eval_env") if isinstance(snap.get("eval_env"), dict) else {}
            seeds["LOOPLAB_EVAL_SEED"] = env.get("LOOPLAB_EVAL_SEED")
    except (OSError, ValueError):
        pass
    private = None
    if best is not None:
        for e in events:
            d = e.data if isinstance(e.data, dict) else {}
            if e.type == "holdout_evaluated" and d.get("node_id") == best.id:
                private = {"metric": d.get("metric"), "gap": d.get("gap"), "protocol": d.get("protocol")}
    return {"bundle_version": BUNDLE_VERSION, "run_id": state.run_id, "run_uid": getattr(state, "run_uid", None),
            "task_id": state.task_id, "goal": state.goal, "direction": state.direction,
            "finished": bool(state.finished), "nodes": len(state.nodes),
            "champion": best.id if best is not None else None,
            "best_metric": best.metric if best is not None else None,
            "best_metric_caveats": champion_metric_caveats(state), "mislead_gap": mislead_gap(state),
            "private_grade": private, "seeds": seeds,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def export_bundle(run_dir, out_dir) -> dict:
    """Write the bundle under `out_dir` and return the RO-Crate metadata it wrote."""
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold

    run_dir, out_dir = Path(run_dir), Path(out_dir)
    if not (run_dir / "events.jsonl").is_file():
        raise FileNotFoundError(f"no events.jsonl under {run_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    events = EventStore(run_dir / "events.jsonl").read_all()
    state = fold(events)
    members: list[tuple[str, str]] = []          # (relative path, description)

    def copy(name: str, description: str, *, required: bool = False) -> None:
        src = run_dir / name
        if src.is_file():
            (out_dir / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, out_dir / name)
            members.append((name, description))
        elif required:
            raise FileNotFoundError(src)

    copy("events.jsonl", "the run's append-only event log — authoritative for the replayable state", required=True)
    copy("spans.jsonl", "the run's trace: every LLM call, tool call and eval span")
    copy("config.snapshot.json", "the Settings the run was launched with")
    copy("task.snapshot.json", "the task document the run was launched with")
    for name in _SIDECARS:
        copy(name, f"audit sidecar {name}")
    best = state.best()
    if best is not None:
        champ = out_dir / "champion"
        champ.mkdir(exist_ok=True)
        (champ / "solution.py").write_text(best.code or "", encoding="utf-8")
        members.append(("champion/solution.py", f"the champion's (node {best.id}) code, off the folded record"))
        for fn, src in sorted((best.files or {}).items()):
            rel = Path("champion") / Path(str(fn).replace("\\", "/"))
            if ".." in rel.parts or rel.is_absolute():
                continue
            (out_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            (out_dir / rel).write_text(str(src), encoding="utf-8")
            members.append((rel.as_posix(), f"the champion's committed file {fn}"))
        copy(f"nodes/node_{best.id}/mlebench_report.json", "the champion's official MLE-bench report")
    claims = {"plan": getattr(state, "research_plan", None),
              "memos": [{"index": i, "claims": memo.get("claims", []), "literature": memo.get("literature", [])}
                        for i, memo in enumerate(getattr(state, "research", []) or [])
                        if isinstance(memo, dict)]}
    (out_dir / "claims.json").write_text(json.dumps(claims, indent=1, ensure_ascii=False), encoding="utf-8")
    members.append(("claims.json", "every research memo's claims with their evidence ids, and the plan"))
    summary = bundle_summary(run_dir, state, events)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    members.append(("summary.json", "the run row a reviewer reads first: number, caveats, Mislead pair, seeds"))
    metadata = crate_metadata(out_dir, members, summary)
    (out_dir / RO_CRATE_METADATA).write_text(json.dumps(metadata, indent=1, ensure_ascii=False), encoding="utf-8")
    return metadata


def crate_metadata(out_dir: Path, members, summary: dict) -> dict:
    """RO-Crate 1.1: the metadata descriptor, the root dataset, one File entity per member."""
    files = []
    for rel, description in members:
        path = out_dir / rel
        files.append({"@id": rel, "@type": "File", "name": rel, "description": description,
                      "encodingFormat": _encoding(rel), "contentSize": path.stat().st_size,
                      "sha256": _sha256(path)})
    root = {"@id": "./", "@type": "Dataset", "name": f"LoopLab run {summary.get('run_id')}",
            "description": "A LoopLab run's reviewer bundle: seeds, traces, code, claims and record",
            "datePublished": summary.get("exported_at"), "license": "see the repository",
            "hasPart": [{"@id": rel} for rel, _ in members],
            "looplab:task_id": summary.get("task_id"), "looplab:champion": summary.get("champion"),
            "looplab:best_metric": summary.get("best_metric"),
            "looplab:mislead_gap": (summary.get("mislead_gap") or {}).get("gap") if summary.get("mislead_gap") else None,
            "looplab:seeds": summary.get("seeds")}
    descriptor = {"@id": RO_CRATE_METADATA, "@type": "CreativeWork",
                  "conformsTo": {"@id": "https://w3id.org/ro/crate/1.1"}, "about": {"@id": "./"}}
    return {"@context": RO_CRATE_CONTEXT, "@graph": [descriptor, root, *files]}


def verify_bundle(out_dir) -> list[str]:
    """Every File entity in the crate exists with the recorded size and digest; the defects, if any."""
    out_dir = Path(out_dir)
    try:
        meta = json.loads((out_dir / RO_CRATE_METADATA).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"unreadable {RO_CRATE_METADATA}: {exc}"]
    defects = []
    for entity in meta.get("@graph", []):
        if entity.get("@type") != "File":
            continue
        path = out_dir / entity["@id"]
        if not path.is_file():
            defects.append(f"missing {entity['@id']}")
            continue
        if path.stat().st_size != entity.get("contentSize"):
            defects.append(f"size mismatch {entity['@id']}")
        elif _sha256(path) != entity.get("sha256"):
            defects.append(f"digest mismatch {entity['@id']}")
    return defects
