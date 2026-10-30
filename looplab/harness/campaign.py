"""A research CAMPAIGN inside one run: pre-registered experiments and recorded amendments (doc 81).

WHY. On 2026-10-12..14 an external agent ran a MiniOneRec campaign through LoopLab and created 22
runs, mostly one experiment per run, because a run froze its protocol, its evaluation inputs and its
protected files, and the contract said "start a new run" to change any of them. The grid the
campaign was about — about twelve models on ONE frozen 40k-user test — was then invisible inside
LoopLab. This module is the in-run alternative: an append-only PROTOCOL JOURNAL
(`protocol_amendments.jsonl`, a run-directory sidecar like `harness_decisions.jsonl`) whose rows are

* ``experiment`` — a pre-registered experiment appended to the run: hypothesis, arms, shared test
  sets, metrics, the declared contrast family and its multiplicity correction. Immutable once
  recorded (a correction is a NEW experiment that names the one it `supersedes`). Each row carries
  the event-log position it was written at (`at_event_seq`), so "registered before the result" is
  checkable against every node's terminal seq, not asserted;
* ``task`` — an amendment of the run's task snapshot (evaluation inputs, protected-file list, data
  mounts), with the JSON diff and the before/after task identity;
* ``files`` — an amendment of a file in the run's editable BASE repository (typically a protected
  file such as a trainer or the scorer's helper), with a unified diff and the before/after SHA-256;
* ``budget`` — an agent's node-budget extension with its reason, served through the ordinary
  durable `budget_extend` command (whose id the row links).

Every row names its AUTHOR (`agent` = the scoped harness token, `operator` = the owner), its
reason, its version number in the run, and the run generation it applies to. Task and file
amendments require a STOPPED engine (the engine reads the task snapshot and copies the base repo
when it starts a lifecycle) and are refused while a durable command is in flight; the engine then
records the change itself at its next re-entry (`task_changed` / `workspace_changed`), which this
journal explains. What the agent may amend is the operator's choice (`harness_policy.json`, written
only with the owner's token): `off` < `experiments` < `inputs` < `all`, plus a cap on the nodes the
agent may add.

Nothing here touches `events.jsonl` or the fold: the journal is a sidecar, so an old run (no file)
reads as an empty protocol and replay is unaffected.
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import orjson
from fastapi import HTTPException

from looplab.core.atomicio import append_jsonl_bytes_locked, atomic_write_bytes, atomic_write_text
from looplab.events.eventstore import EventStoreLockError, interprocess_lock
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.harness.journals import (history_incomplete, read_event_source, read_source,
                                      shape_checked)

JOURNAL = "protocol_amendments.jsonl"
POLICY = "harness_policy.json"
AMENDMENT_KINDS = ("experiment", "task", "files", "budget")
# The operator's amendment levels, each including the ones before it.
POLICY_LEVELS = ("off", "experiments", "inputs", "all")
_LEVEL_FOR_KIND = {"experiment": "experiments", "task": "inputs", "files": "all", "budget": "experiments"}
DEFAULT_POLICY = {"amendments": "all", "max_agent_added_nodes": None, "implicit_canary": "off",
                  "implicit_canary_timeout": 120.0}

SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
METRIC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._:/+-]{0,63}$")
TEST_ID_RE = re.compile(r"^qs1-[0-9a-f]{32}$")
MAX_TEXT = 4000
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_DIFF_CHARS = 200_000


def journal_path(rd: Path) -> Path:
    return Path(rd) / JOURNAL


def author_of(agent_token: bool) -> dict:
    """WHO wrote a record: the scoped agent token, or the owner (operator). A rule never writes
    here — LoopLab's own deterministic computations are labelled `computed_by` on their outputs."""
    return {"kind": "agent" if agent_token else "operator",
            "credential": "agent_token" if agent_token else "owner"}


# ------------------------------------------------------------------------------------------ reads
def read_journal(rd: Path) -> tuple[list[dict], dict]:
    """All rows (shape-checked) plus the source health; a missing file is an empty protocol."""
    rows, health = read_source(journal_path(rd))
    # A dropped row may be an earlier amendment (its action_id, its version): incomplete it is
    # (`journals.shape_checked`); `publish_amendment` refuses until the journal is recovered.
    return shape_checked(rows, health, _journal_row), health


def _journal_row(row: dict) -> bool:
    return (isinstance(row.get("amendment_id"), str) and row.get("kind") in AMENDMENT_KINDS
            and isinstance(row.get("version"), int))


def journal_incomplete(rd: Path) -> HTTPException:
    """The 503 naming the journal's first damaged line (`journals.history_incomplete`)."""
    return history_incomplete(journal_path(rd), JOURNAL, _journal_row)


def experiments_of(rows: list[dict], run_uid: Optional[str] = None) -> dict[str, dict]:
    """`{experiment_id: experiment}` in registration order, each with its registration receipt."""
    out: dict[str, dict] = {}
    for row in rows:
        if row.get("kind") != "experiment" or (run_uid and row.get("run_uid") != run_uid):
            continue
        exp = row.get("payload")
        if not isinstance(exp, dict) or not isinstance(exp.get("experiment_id"), str):
            continue
        out[exp["experiment_id"]] = {
            **exp,
            "registration": {key: row.get(key) for key in (
                "amendment_id", "version", "recorded_at", "at_event_seq", "at_node_count",
                "author", "generation", "reason")},
        }
    superseded = {e.get("supersedes") for e in out.values() if e.get("supersedes")}
    for eid, exp in out.items():
        exp["superseded"] = eid in superseded
    return out


def read_policy(rd: Path) -> dict:
    path = Path(rd) / POLICY
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {**DEFAULT_POLICY, "source": "default"}
    except (OSError, ValueError) as exc:
        raise HTTPException(503, {"code": "harness_policy_unreadable", "source": POLICY}) from exc
    policy = {**DEFAULT_POLICY, "source": POLICY}
    if isinstance(raw, dict):
        if raw.get("amendments") in POLICY_LEVELS:
            policy["amendments"] = raw["amendments"]
        cap = raw.get("max_agent_added_nodes")
        if cap is None or (isinstance(cap, int) and not isinstance(cap, bool) and cap >= 0):
            policy["max_agent_added_nodes"] = cap
        if raw.get("implicit_canary") in ("off", "sandboxed"):
            policy["implicit_canary"] = raw["implicit_canary"]
        timeout = raw.get("implicit_canary_timeout")
        if type(timeout) in (int, float) and 0 < timeout <= 3600:
            policy["implicit_canary_timeout"] = float(timeout)
        for key in ("updated_at", "updated_by", "note"):
            if key in raw:
                policy[key] = raw[key]
    return policy


def write_policy(rd: Path, body: dict) -> dict:
    """The operator's limits (owner token only — the server refuses this route to the agent)."""
    level = body.get("amendments", DEFAULT_POLICY["amendments"])
    if level not in POLICY_LEVELS:
        raise HTTPException(400, f"amendments must be one of {', '.join(POLICY_LEVELS)}")
    cap = body.get("max_agent_added_nodes")
    if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool) or cap < 0
                            or cap > 1_000_000):
        raise HTTPException(400, "max_agent_added_nodes must be null or an integer >= 0")
    note = body.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > 1000):
        raise HTTPException(400, "note must be a string of at most 1000 characters")
    implicit = body.get("implicit_canary", "off")
    if implicit not in ("off", "sandboxed"):
        raise HTTPException(400, "implicit_canary must be off or sandboxed")
    timeout = body.get("implicit_canary_timeout", 120.0)
    if type(timeout) not in (int, float) or not 0 < timeout <= 3600:
        raise HTTPException(400, "implicit_canary_timeout must be within (0, 3600] seconds")
    policy = {"amendments": level, "max_agent_added_nodes": cap, "implicit_canary": implicit,
              "implicit_canary_timeout": float(timeout), "updated_at": time.time(),
              "updated_by": "operator", **({"note": note} if note else {})}
    atomic_write_text(Path(rd) / POLICY, json.dumps(policy, indent=2))
    return {**policy, "source": POLICY}


def protocol_view(rd: Path) -> dict:
    """The run's protocol as the UI and the agent read it: experiments, the amendment audit trail
    and the operator's policy. A GET: no lock, no sequencer wait."""
    rows, health = read_journal(rd)
    run_uid = None
    try:
        from looplab.events.eventstore import EventStore
        events = EventStore(Path(rd) / "events.jsonl").read_all()
        state = fold(events)
        run_uid = state.run_uid or None
        generation = run_generation_token(events) or None
    except (OSError, ValueError):
        generation = None
    current = [row for row in rows if not run_uid or row.get("run_uid") == run_uid]
    trail = [{key: row.get(key) for key in (
        "amendment_id", "version", "kind", "author", "reason", "recorded_at", "at_event_seq",
        "at_node_count", "generation", "summary", "action_id")} | {"payload": _trail_payload(row)}
        for row in current]
    # The latest recorded CONTENT fingerprint per data mount (amendment rows only; a mount never
    # amended has none — the engine's own `workspace_fingerprint` covers it shallowly).
    data_content: dict = {}
    for row in current:
        content = (row.get("payload") or {}).get("data_content") if row.get("kind") == "task" else None
        if isinstance(content, dict):
            for name, pair in content.items():
                if isinstance(pair, dict):
                    data_content[name] = {"fingerprint": pair.get("after"),
                                          "amendment_id": row.get("amendment_id"),
                                          "version": row.get("version")}
    return {"version": 1, "run_uid": run_uid, "generation": generation,
            "protocol_version": len(current),
            "experiments": list(experiments_of(current).values()),
            "amendments": trail, "data_content": data_content, "policy": read_policy(rd),
            "source_health": health}


def _trail_payload(row: dict) -> dict:
    payload = row.get("payload")
    return payload if isinstance(payload, dict) else {}


# --------------------------------------------------------------------------- experiment validation
def _text(value, name: str, *, required: bool = False, limit: int = MAX_TEXT) -> Optional[str]:
    if value is None or value == "":
        if required:
            raise HTTPException(400, f"{name} is required")
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise HTTPException(400, f"{name} must be a string of at most {limit} characters")
    return value.strip()


def _slug(value, name: str) -> str:
    if not isinstance(value, str) or not SLUG_RE.fullmatch(value):
        raise HTTPException(400, f"{name} must match {SLUG_RE.pattern}")
    return value


def normalize_experiment(raw: Any, *, root: Optional[Path] = None) -> dict:
    """Validate one experiment declaration; return its canonical form (the frozen record)."""
    if not isinstance(raw, dict):
        raise HTTPException(400, "experiment must be an object")
    allowed = {"experiment_id", "title", "question", "hypothesis", "arms", "tests", "metrics",
               "primary_metric", "contrasts", "correction", "alpha", "ci", "bootstrap_replicates",
               "supersedes", "notes", "relationships", "question_card_id", "research_context"}
    unknown = set(raw) - allowed
    if unknown:
        raise HTTPException(400, f"experiment has unknown field(s): {', '.join(sorted(unknown))}")
    exp: dict[str, Any] = {"experiment_id": _slug(raw.get("experiment_id"), "experiment_id")}
    exp["title"] = _text(raw.get("title"), "title", limit=300) or exp["experiment_id"]
    exp["question"] = _text(raw.get("question"), "question")
    card_id = raw.get("question_card_id")
    if card_id is not None:
        if not isinstance(card_id, str) or not card_id.strip() or len(card_id) > 160 or any(ord(c) < 32 for c in card_id):
            raise HTTPException(400, "question_card_id must be a non-empty existing card id")
        exp["question_card_id"] = card_id
    exp["hypothesis"] = _text(raw.get("hypothesis"), "hypothesis", required=True)
    exp["notes"] = _text(raw.get("notes"), "notes")
    if "research_context" in raw:
        context = raw["research_context"]
        fields = ("change", "motivation", "data_weights", "control", "criterion", "alternatives")
        if not isinstance(context, dict) or set(context) - set(fields):
            raise HTTPException(400, "research_context has unknown fields")
        exp["research_context"] = {key: _text(context.get(key), f"research_context.{key}", limit=1200) or ""
                                   for key in fields}
    arms = raw.get("arms")
    if not isinstance(arms, dict) or not 1 <= len(arms) <= 64:
        raise HTTPException(400, "arms must be an object of 1..64 named arms")
    exp["arms"] = {}
    for label, arm in arms.items():
        _slug(label, "arm label")
        arm = arm if isinstance(arm, dict) else {}
        if set(arm) - {"run_id", "node_id", "description", "work_kind"}:
            raise HTTPException(400, f"arm {label!r} has unknown fields")
        run_id = arm.get("run_id")
        if run_id is not None:
            if not isinstance(run_id, str) or not run_id or "/" in run_id or "\\" in run_id \
                    or run_id in (".", ".."):
                raise HTTPException(400, f"arm {label!r}: run_id must be one run identifier")
            if root is not None and not (Path(root) / run_id / "events.jsonl").is_file():
                raise HTTPException(400, f"arm {label!r}: no run {run_id!r} under this run root")
        node_id = arm.get("node_id")
        if node_id is not None and (not isinstance(node_id, int) or isinstance(node_id, bool)
                                    or node_id < 0):
            raise HTTPException(400, f"arm {label!r}: node_id must be a non-negative integer")
        exp["arms"][label] = {"run_id": run_id, "node_id": node_id,
                              "description": _text(arm.get("description"), "arm description", limit=500)}
        work_kind = arm.get("work_kind")
        if work_kind is not None:
            if work_kind not in ("scientific_experiment", "fit", "score_only", "engineering_recovery"):
                raise HTTPException(400, "arm work_kind is invalid")
            exp["arms"][label]["work_kind"] = work_kind
    relationships = raw.get("relationships", [])
    if not isinstance(relationships, list) or len(relationships) > 128:
        raise HTTPException(400, "relationships must be a list of at most 128 explicit references")
    exp["relationships"] = []
    for relation in relationships:
        if not isinstance(relation, dict) or set(relation) - {"kind", "subject_arm", "run_id", "node_id", "node_generation", "artifact_sha256", "explanation"}:
            raise HTTPException(400, "relationship has unknown fields")
        kind = relation.get("kind")
        if kind not in ("model_producer", "scientific_control", "code_reference"):
            raise HTTPException(400, "relationship kind must be model_producer, scientific_control or code_reference")
        if relation.get("subject_arm") not in exp["arms"]:
            raise HTTPException(400, "relationship subject_arm must name a declared arm")
        nid, attempt = relation.get("node_id"), relation.get("node_generation")
        if type(nid) is not int or nid < 0 or (attempt is not None and (type(attempt) is not int or attempt < 0)):
            raise HTTPException(400, "relationship requires non-negative node_id and optional node_generation")
        rid = relation.get("run_id")
        if rid is not None and (not isinstance(rid, str) or not rid or '/' in rid or '\\' in rid or rid in ('.', '..')):
            raise HTTPException(400, "relationship run_id must be one run identifier")
        digest = relation.get("artifact_sha256")
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise HTTPException(400, "relationship artifact_sha256 must be a lowercase SHA-256")
        exp["relationships"].append({"kind": kind, "subject_arm": relation["subject_arm"], "run_id": rid,
                                     "node_id": nid, "node_generation": attempt, "artifact_sha256": digest,
                                     "explanation": _text(relation.get("explanation"), "relationship explanation", limit=500)})
    tests = raw.get("tests")
    if not isinstance(tests, list) or not 1 <= len(tests) <= 16:
        raise HTTPException(400, "tests must list 1..16 shared evaluation sets")
    exp["tests"] = []
    for test in tests:
        if not isinstance(test, dict) or set(test) - {"label", "test_id", "description"}:
            raise HTTPException(400, "each test is {label, test_id?, description?}")
        label = _slug(test.get("label"), "test label")
        test_id = test.get("test_id")
        if test_id is not None and (not isinstance(test_id, str) or not TEST_ID_RE.fullmatch(test_id)):
            raise HTTPException(400, f"test {label!r}: test_id must match {TEST_ID_RE.pattern} "
                                "(the id an upload receipt returns)")
        exp["tests"].append({"label": label, "test_id": test_id,
                             "description": _text(test.get("description"), "test description",
                                                  limit=500)})
    if len({t["label"] for t in exp["tests"]}) != len(exp["tests"]):
        raise HTTPException(400, "test labels must be distinct")
    metrics = raw.get("metrics")
    if not isinstance(metrics, list) or not 1 <= len(metrics) <= 32:
        raise HTTPException(400, "metrics must list 1..32 per-query metrics")
    exp["metrics"] = []
    for metric in metrics:
        if not isinstance(metric, dict) or set(metric) - {"name", "kind", "direction", "description"}:
            raise HTTPException(400, "each metric is {name, kind: mean|ratio, direction?}")
        name = metric.get("name")
        if not isinstance(name, str) or not METRIC_RE.fullmatch(name):
            raise HTTPException(400, f"metric name must match {METRIC_RE.pattern}")
        kind = metric.get("kind", "mean")
        if kind not in ("mean", "ratio"):
            raise HTTPException(400, f"metric {name!r}: kind must be mean or ratio")
        direction = metric.get("direction", "max")
        if direction not in ("max", "min"):
            raise HTTPException(400, f"metric {name!r}: direction must be max or min")
        exp["metrics"].append({"name": name, "kind": kind, "direction": direction,
                               "description": _text(metric.get("description"),
                                                    "metric description", limit=500)})
    names = [m["name"] for m in exp["metrics"]]
    if len(set(names)) != len(names):
        raise HTTPException(400, "metric names must be distinct")
    primary = raw.get("primary_metric") or names[0]
    if primary not in names:
        raise HTTPException(400, "primary_metric must be one of the declared metrics")
    exp["primary_metric"] = primary
    contrasts = raw.get("contrasts", [])
    if not isinstance(contrasts, list) or len(contrasts) > 128:
        raise HTTPException(400, "contrasts must list at most 128 paired contrasts")
    exp["contrasts"] = []
    labels = {t["label"] for t in exp["tests"]}
    for contrast in contrasts:
        if not isinstance(contrast, dict) or set(contrast) - {
                "id", "treatment", "control", "metric", "test", "family"}:
            raise HTTPException(400, "each contrast is {id?, treatment, control, metric?, test?, family?}")
        treatment, control = contrast.get("treatment"), contrast.get("control")
        if treatment not in exp["arms"] or control not in exp["arms"] or treatment == control:
            raise HTTPException(400, "a contrast names two different declared arms")
        metric = contrast.get("metric") or primary
        if metric not in names:
            raise HTTPException(400, f"contrast metric {metric!r} is not declared")
        test = contrast.get("test") or exp["tests"][0]["label"]
        if test not in labels:
            raise HTTPException(400, f"contrast test {test!r} is not declared")
        family = contrast.get("family", "primary")
        if not isinstance(family, str) or not SLUG_RE.fullmatch(family):
            raise HTTPException(400, "contrast family must be a short slug (e.g. primary)")
        cid = contrast.get("id") or f"{treatment}-{control}:{metric}@{test}"
        if not isinstance(cid, str) or len(cid) > 200:
            raise HTTPException(400, "contrast id must be a string of at most 200 characters")
        exp["contrasts"].append({"id": cid, "treatment": treatment, "control": control,
                                 "metric": metric, "test": test, "family": family})
    if len({c["id"] for c in exp["contrasts"]}) != len(exp["contrasts"]):
        raise HTTPException(400, "contrast ids must be distinct")
    correction = raw.get("correction", "holm")
    if correction not in ("holm", "none"):
        raise HTTPException(400, "correction must be holm or none")
    exp["correction"] = correction
    alpha = raw.get("alpha", 0.05)
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not 0 < alpha < 1:
        raise HTTPException(400, "alpha must be in (0, 1)")
    exp["alpha"] = float(alpha)
    ci = raw.get("ci", "normal")
    if ci not in ("normal", "bootstrap"):
        raise HTTPException(400, "ci must be normal or bootstrap")
    exp["ci"] = ci
    reps = raw.get("bootstrap_replicates", 2000)
    if isinstance(reps, bool) or not isinstance(reps, int) or not 100 <= reps <= 100_000:
        raise HTTPException(400, "bootstrap_replicates must be an integer in [100, 100000]")
    exp["bootstrap_replicates"] = reps
    supersedes = raw.get("supersedes")
    exp["supersedes"] = _slug(supersedes, "supersedes") if supersedes else None
    return exp


# What a re-filed declaration must AGREE on to count as the registered one (`same_declaration`):
# the statistics and the design. Strict equality of the whole stored payload refused a legitimate
# second candidate over a row an older `normalize_experiment` wrote (a default key added or gone)
# or over a cosmetic title/notes/description edit (review 2026-10-16). `primary_metric` is kept: it
# picks the default contrast metric and what a report leads with.
DECLARATION_FIELDS = ("hypothesis", "arms", "tests", "metrics", "primary_metric", "contrasts",
                      "correction", "alpha", "ci", "bootstrap_replicates", "supersedes", "relationships", "research_context")


def _declaration_substance(exp: dict) -> dict:
    def plain(rows):
        return ({k: v for k, v in row.items() if k != "description"} if isinstance(row, dict)
                else row for row in rows)
    out = {field: exp.get(field) for field in DECLARATION_FIELDS}
    if isinstance(out["arms"], dict):
        out["arms"] = dict(zip(out["arms"], plain(out["arms"].values())))
    for field in ("tests", "metrics"):
        if isinstance(out[field], list):
            out[field] = list(plain(out[field]))
    return out


def same_declaration(saved: dict, proposed: dict) -> bool:
    """Whether `proposed` (already normalized) declares the registered experiment `saved`: both
    through the CURRENT `normalize_experiment` where the stored row still validates, then compared
    on `DECLARATION_FIELDS` without descriptions."""
    try:
        saved = normalize_experiment(saved)
    except HTTPException:
        pass                       # an older row the current rules refuse: compared as stored
    return (_canonical(_declaration_substance(saved))
            == _canonical(_declaration_substance(proposed)))


# ----------------------------------------------------------------------------------- the writer
def _canonical(value) -> bytes:
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS)


def _require_level(policy: dict, kind: str, agent: bool) -> None:
    if not agent:
        return                                   # the operator may always amend their own run
    needed = _LEVEL_FOR_KIND[kind]
    if POLICY_LEVELS.index(policy["amendments"]) < POLICY_LEVELS.index(needed):
        raise HTTPException(403, {
            "code": "amendment_not_permitted",
            "message": (f"the operator's policy for this run allows agent amendments up to "
                        f"{policy['amendments']!r}; a {kind} amendment needs {needed!r}"),
            "remediation": "ask the operator to raise harness_policy.amendments for this run",
            "retryable": False})


def _require_stopped(srv, rd: Path, kind: str) -> None:
    """Task/file amendments change what the NEXT engine reads; a live engine has already read it."""
    try:
        alive = srv.commands.engine_liveness(rd)
    except OSError:
        alive = None
    if alive is not False:
        raise HTTPException(409, {
            "code": "engine_running" if alive else "engine_liveness_unknown",
            "message": (f"a {kind} amendment needs a stopped engine: the engine reads the task "
                        "snapshot and copies the base repository when a lifecycle starts"),
            "remediation": ("pause the run (command `pause`), wait until the run state reports "
                            "engine_running=false, amend, then resume or inject"),
            "retryable": True})
    srv.commands.reject_if_active(rd, f"record a {kind} amendment")


def publish_amendment(srv, rd: Path, body, *, agent_token: bool) -> dict:
    """Validate and append one amendment; idempotent on `action_id` (an exact retry returns the
    original receipt, a different body under the same id is a 409)."""
    from looplab.serve.control_validation import run_is_external

    kind = body.kind
    if agent_token and not run_is_external(rd):
        raise HTTPException(403, {"code": "agent_token_refused",
                                  "message": "the agent token may amend only an external_harness run",
                                  "retryable": False})
    if kind == "budget":
        return _publish_budget(srv, rd, body, agent_token=agent_token)
    path = journal_path(rd)
    try:
        with srv.commands.sequence(rd):
            events = read_event_source(rd)
            generation = run_generation_token(events)
            if not generation or generation != body.expected_generation.lower():
                raise HTTPException(409, {"code": "run_generation_changed",
                                          "current_generation": generation})
            state = fold(events)
            if not state.run_uid:
                raise HTTPException(409, "run has no durable identity")
            policy = read_policy(rd)
            _require_level(policy, kind, agent_token)
            with interprocess_lock(Path(str(path) + ".lock"), required=True):
                rows, health = read_journal(rd)
                if not health["read_complete"]:
                    raise journal_incomplete(rd)
                request_digest = hashlib.sha256(_canonical(body.request_payload())).hexdigest()
                for old in rows:
                    if old.get("run_uid") == state.run_uid and old.get("action_id") == body.action_id:
                        if old.get("request_sha256") != request_digest:
                            raise HTTPException(409, "amendment action_id was reused with different content")
                        return {"ok": True, "replayed": True, "amendment": old}
                current = [row for row in rows if row.get("run_uid") == state.run_uid]
                row = {"v": 1, "kind": kind, "run_uid": state.run_uid, "generation": generation,
                       "action_id": body.action_id, "request_sha256": request_digest,
                       "author": author_of(agent_token), "reason": body.reason,
                       "recorded_at": time.time(), "at_event_seq": events[-1].seq,
                       "at_node_count": len(state.nodes), "version": len(current) + 1,
                       "amendment_id": f"am{len(current) + 1}-{request_digest[:10]}"}
                undo = None
                if kind == "experiment":
                    exp = normalize_experiment(body.experiment, root=Path(rd).parent)
                    card_id = exp.get("question_card_id")
                    if card_id is not None and (card_id not in state.cards or state.cards[card_id].card_kind != "direction"):
                        raise HTTPException(409, "question_card_id does not name a current research direction")
                    known = experiments_of(current)
                    if exp["experiment_id"] in known:
                        saved = {k: v for k, v in known[exp["experiment_id"]].items()
                                 if k not in ("registration", "superseded")}
                        raise HTTPException(409, {
                            "code": "experiment_exists",
                            # Whether THIS declaration is the registered one: a caller filing a
                            # second candidate under the same experiment may proceed; one whose
                            # hypothesis/arms/correction differ may not (weekly-registration-
                            # conflict) — the registration stays immutable either way.
                            "same_declaration": same_declaration(saved, exp),
                            "message": (f"experiment {exp['experiment_id']!r} is already registered "
                                        "and a registration is immutable"),
                            "remediation": ("register a new experiment_id with supersedes="
                                            f"{exp['experiment_id']!r} and a reason")})
                    if exp["supersedes"] and exp["supersedes"] not in known:
                        raise HTTPException(400, "supersedes names no registered experiment")
                    row["payload"] = exp
                    row["summary"] = (f"experiment {exp['experiment_id']}: {len(exp['arms'])} arms, "
                                      f"{len(exp['contrasts'])} contrasts")
                elif kind == "task":
                    _require_stopped(srv, rd, kind)
                    row["payload"], undo = _apply_task_amendment(rd, body.task)
                    row["summary"] = row["payload"]["summary"]
                elif kind == "files":
                    _require_stopped(srv, rd, kind)
                    row["payload"], undo = _apply_file_amendment(rd, body.files)
                    row["summary"] = row["payload"]["summary"]
                try:
                    append_jsonl_bytes_locked(path, _canonical(row))
                except BaseException:
                    if undo is not None:
                        undo()            # the change and its record land together, or neither
                    raise
                return {"ok": True, "replayed": False, "amendment": row}
    except HTTPException:
        raise
    except (OSError, EventStoreLockError, ValueError, KeyError) as exc:
        raise HTTPException(503, "protocol journal unavailable") from exc


# ------------------------------------------------------------------------------- data content
# A bounded CONTENT fingerprint of a data mount (doc 81 §8 leftover). Bounded three ways because a
# mount can be a multi-GB tree on a slow FUSE mount: at most `_FP_MAX_FILES` entries, at most
# `_FP_HASH_BYTES` of file content hashed in full (larger files contribute their size, mtime and
# the SHA-256 of their first and last MiB — `sampled`), and at most `_FP_SECONDS` of wall time. A
# bound that was hit is stated (`truncated`), never hidden: such a fingerprint still changes when
# the listed part changes, and says it may miss a change past the bound.
_FP_MAX_FILES = 20_000
_FP_HASH_BYTES = 256 * 1024 * 1024
_FP_SAMPLE = 1024 * 1024
_FP_SECONDS = 30.0


def _mount_path(spec) -> Optional[str]:
    path = spec.get("path") if isinstance(spec, dict) else spec
    return os.path.expanduser(os.path.expandvars(path)) if isinstance(path, str) else None


# The fingerprint's VERSION. `lfp2` (2026-10-16) hashes a directory LINK by its target, which `lfp1`
# never saw, so the two digest the same tree differently: a fingerprint of another method is NOT
# COMPARABLE (`data_content_incomparable`), never evidence that the data changed.
FINGERPRINT_METHOD = "lfp2"


def content_fingerprint(path: Optional[str]) -> dict:
    """`{method, digest, files, bytes, hashed_bytes, sampled_files, truncated, missing}` for the file
    or directory tree at `path` (a link at the ROOT is followed — it is the declared mount; links
    inside a tree are recorded by target, never followed)."""
    out = {"method": FINGERPRINT_METHOD, "digest": None, "files": 0, "bytes": 0, "hashed_bytes": 0,
           "sampled_files": 0, "truncated": False, "missing": False}
    if not path or not os.path.exists(path):
        out["missing"] = True
        return out
    deadline = time.monotonic() + _FP_SECONDS
    digest = hashlib.sha256()
    root = Path(path)

    def entries():
        if root.is_file():
            # The mount's own path is what the operator declared: a link AT the root (a common
            # `train.parquet -> /mnt/v1/train.parquet` layout) is followed, else the refresh
            # this fingerprint exists for hashed only the link text and an in-place overwrite of
            # the target read as "unchanged". Links INSIDE a tree stay recorded by target.
            yield "", root.resolve()
            return
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            # A link to a DIRECTORY lands in `dirnames` and is never descended (no followlinks,
            # so a link cycle is never walked), so it was recorded nowhere: retargeting
            # `mount/train` from dataset A to B left the digest unchanged with files=0 (review
            # 2026-10-15, weekly-data-directory-links). It is yielded beside the files and
            # hashed by its TARGET like any inner link, in one sorted name order.
            names = sorted(set(filenames) | {d for d in dirnames
                                             if os.path.islink(os.path.join(dirpath, d))})
            for name in names:
                full = Path(dirpath) / name
                yield full.relative_to(root).as_posix(), full

    for rel, full in entries():
        if out["files"] >= _FP_MAX_FILES or time.monotonic() > deadline:
            out["truncated"] = True
            break
        try:
            st = full.lstat()
        except OSError:
            continue
        out["files"] += 1
        if full.is_symlink():
            digest.update(f"L\0{rel}\0{os.readlink(full)}\n".encode("utf-8", "surrogateescape"))
            continue
        out["bytes"] += st.st_size
        digest.update(f"F\0{rel}\0{st.st_size}\0".encode("utf-8", "surrogateescape"))
        whole = out["hashed_bytes"] + st.st_size <= _FP_HASH_BYTES
        h = hashlib.sha256()
        try:
            with open(full, "rb") as fh:
                if whole:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                    out["hashed_bytes"] += st.st_size
                else:
                    h.update(fh.read(_FP_SAMPLE))
                    fh.seek(max(0, st.st_size - _FP_SAMPLE))
                    h.update(fh.read(_FP_SAMPLE))
                    digest.update(f"{st.st_mtime_ns}\0".encode())
                    out["sampled_files"] += 1
        except OSError:
            digest.update(b"unreadable\0")
            continue
        digest.update(h.hexdigest().encode() + b"\n")
    out["digest"] = digest.hexdigest()
    return out


def _recorded_data_fingerprints(rd: Path) -> dict:
    """The latest content fingerprint the journal recorded per data mount name (this run only)."""
    rows, _health = read_journal(rd)
    try:
        from looplab.events.eventstore import EventStore
        uid = fold(EventStore(Path(rd) / "events.jsonl").read_all()).run_uid
    except (OSError, ValueError):
        uid = None
    rows = [r for r in rows if not uid or r.get("run_uid") == uid]
    latest: dict = {}
    for row in rows:
        content = (row.get("payload") or {}).get("data_content") if row.get("kind") == "task" else None
        if isinstance(content, dict):
            for name, pair in content.items():
                if isinstance(pair, dict):
                    latest[name] = pair.get("after")
    return latest


# ------------------------------------------------------------------------------- task amendments
def _apply_task_amendment(rd: Path, changes) -> tuple[dict, Any]:
    from looplab.adapters.tasks import normalize_task, validate_task
    from looplab.core.setup_identity import task_identity

    if not isinstance(changes, dict) or not changes:
        raise HTTPException(400, "task changes are required")
    allowed = {"inputs_add", "inputs_remove", "protect_add", "protect_remove", "data_add",
               "data_remove", "data_refresh", "eval_command", "eval_canary"}
    if set(changes) - allowed:
        raise HTTPException(400, f"task changes may only be {', '.join(sorted(allowed))}")
    snap = Path(rd) / "task.snapshot.json"
    before_bytes = snap.read_bytes()
    raw = json.loads(before_bytes)
    try:
        task = normalize_task(raw) if isinstance(raw, dict) else {}
    except ValueError as exc:
        raise HTTPException(400, f"the task does not normalize: {str(exc)[:500]}") from None
    if not isinstance(task, dict) or not isinstance(task.get("eval"), dict):
        raise HTTPException(409, {"code": "task_shape_unsupported",
                                  "message": "only a repo task snapshot with an eval block can be amended"})
    before_identity = task_identity(validate_task(json.loads(before_bytes), existing_run=True))

    if "eval_command" in changes:
        update = changes["eval_command"]
        def argv(value):
            return (isinstance(value, list) and 1 <= len(value) <= 256
                    and all(isinstance(s, str) and s and len(s) <= 4096 and "\0" not in s
                            for s in value))
        if (not isinstance(update, dict) or set(update) != {"expected", "command"}
                or not argv(update["expected"]) or not argv(update["command"])):
            raise HTTPException(400, "eval_command requires {expected: [argv], command: [argv]}")
        ev = task["eval"]
        if not ev.get("command") or ev.get("stages") or ev.get("host_scorer"):
            raise HTTPException(400, "eval_command only amends a direct command, not stages or host scorers")
        if ev["command"] != update["expected"]:
            raise HTTPException(409, {"code": "eval_command_changed",
                                      "message": "the eval command differs from expected; reread the snapshot"})
        ev["command"] = list(update["command"])

    if "eval_canary" in changes:
        from looplab.adapters.repo_task import CanarySpec
        update = changes["eval_canary"]
        if not isinstance(update, dict) or set(update) != {"expected", "canary"}:
            raise HTTPException(400, "eval_canary requires {expected: object|null, canary: object|null}")
        if task["eval"].get("canary") != update["expected"]:
            raise HTTPException(409, {"code": "eval_canary_changed",
                                      "message": "the canary differs from expected; reread the snapshot"})
        try:
            value = update["canary"]
            canary = None if value is None else CanarySpec.model_validate(value).model_dump()
        except ValueError as exc:
            raise HTTPException(400, "invalid eval canary declaration") from exc
        task["eval"]["canary"] = canary

    def strings(name, *, absolute=False):
        value = changes.get(name, [])
        if not isinstance(value, list) or len(value) > 256 or not all(
                isinstance(v, str) and v and len(v) <= 4096 for v in value):
            raise HTTPException(400, f"{name} must be a list of at most 256 paths")
        if absolute and not all(os.path.isabs(os.path.expanduser(os.path.expandvars(v)))
                                for v in value):
            raise HTTPException(400, f"{name} must hold absolute paths")
        return value

    inputs = list(task["eval"].get("inputs") or [])
    for p in strings("inputs_remove"):
        if p not in inputs:
            raise HTTPException(400, f"inputs_remove: {p!r} is not a declared input")
        inputs.remove(p)
    for p in strings("inputs_add", absolute=True):
        if p in inputs:
            raise HTTPException(400, f"inputs_add: {p!r} is already declared")
        inputs.append(p)
    task["eval"]["inputs"] = inputs
    protect = list(task.get("protect") or [])
    for p in strings("protect_remove"):
        if p not in protect:
            raise HTTPException(400, f"protect_remove: {p!r} is not protected")
        protect.remove(p)
    for p in strings("protect_add"):
        if p.startswith("/") or ".." in p.replace("\\", "/").split("/"):
            raise HTTPException(400, "protect_add takes paths relative to the editable root")
        if p not in protect:
            protect.append(p)
    task["protect"] = protect
    data_add, data_remove = changes.get("data_add", {}), changes.get("data_remove", [])
    data_refresh = changes.get("data_refresh", [])
    if not isinstance(data_refresh, list) or not all(isinstance(n, str) for n in data_refresh):
        raise HTTPException(400, "data_refresh is a list of data mount names")
    mounts_before = dict(task.get("data") or {})
    for name in data_refresh:
        if name not in mounts_before:
            raise HTTPException(400, f"data_refresh: no data mount {name!r}")
    if isinstance(data_remove, list) and set(data_refresh) & set(data_remove):
        raise HTTPException(400, "a mount is either refreshed or removed, not both")
    if data_add or data_remove:
        if not isinstance(data_add, dict) or not isinstance(data_remove, list):
            raise HTTPException(400, "data_add is {name: path|spec}, data_remove a list of names")
        data = dict(task.get("data") or {})
        for name in data_remove:
            if name not in data:
                raise HTTPException(400, f"data_remove: no data mount {name!r}")
            data.pop(name)
        for name, spec in data_add.items():
            if not isinstance(name, str) or not SLUG_RE.fullmatch(name) or name in data:
                raise HTTPException(400, f"data_add: {name!r} must be a new mount name")
            data[name] = spec
        task["data"] = data
    try:
        adapter = validate_task(json.loads(json.dumps(task)), existing_run=True)
    except Exception as exc:  # noqa: BLE001 — any validation failure is the caller's 400, stated
        raise HTTPException(400, f"the amended task does not validate: {str(exc)[:500]}") from None
    after_identity = task_identity(adapter)
    after_text = json.dumps(task, indent=2)
    before_text = json.dumps(json.loads(before_bytes), indent=2)
    diff = "".join(difflib.unified_diff(before_text.splitlines(True), after_text.splitlines(True),
                                        "task.snapshot.json (before)", "task.snapshot.json (after)"))
    # THE CONTENT of every mount this amendment touches, not only its declaration (doc 81 §8
    # leftover): a fingerprint of what the path held when the amendment was recorded, and for a
    # refreshed or removed mount the fingerprint the journal last recorded for it — so "the data
    # changed under the same name" is a recorded protocol version, not an invisible one.
    data_content = {}
    previous = _recorded_data_fingerprints(rd)
    for name in sorted(set(data_add) | set(data_refresh)):
        spec = (task.get("data") or {}).get(name)
        data_content[name] = {"before": previous.get(name),
                              "after": content_fingerprint(_mount_path(spec))}
    for name in data_remove:
        data_content[name] = {"before": previous.get(name), "after": None}
    refreshed, incomparable = [], []
    for n in data_refresh:
        before, after = data_content[n]["before"] or {}, data_content[n]["after"]
        if before.get("digest") and before.get("method") != after.get("method"):
            incomparable.append(n)       # two fingerprint versions: no verdict either way
        elif before.get("digest") != after.get("digest"):
            refreshed.append(n)
    if not diff and not data_refresh:
        raise HTTPException(400, "the task changes leave the task unchanged")
    if diff:
        atomic_write_text(snap, after_text)

    def undo():
        atomic_write_bytes(snap, before_bytes)
    summary = ", ".join(f"{k}: {len(v)}" for k, v in changes.items() if v)
    out = {"changes": changes, "before_identity": before_identity,
           "after_identity": after_identity, "diff": diff[:MAX_DIFF_CHARS],
           "diff_truncated": len(diff) > MAX_DIFF_CHARS, "applied": True,
           "effective": "the next engine start (resume) reads the amended snapshot",
           "summary": f"task amended ({summary})"}
    if data_content:
        out["data_content"] = data_content
        out["data_content_changed"] = refreshed
        if incomparable:
            out["data_content_incomparable"] = incomparable
    return (out, undo)


# ------------------------------------------------------------------------------- file amendments
def _editable_root(rd: Path) -> tuple[Path, dict]:
    snap = Path(rd) / "task.snapshot.json"
    task = json.loads(snap.read_text(encoding="utf-8"))
    from looplab.adapters.tasks import normalize_task
    task = normalize_task(task) if isinstance(task, dict) else {}
    root = task.get("editable_path")
    if not isinstance(root, str) or not root:
        raise HTTPException(409, {"code": "no_editable_root",
                                  "message": "file amendments need a repo task with editable_path"})
    root_path = Path(os.path.expanduser(os.path.expandvars(root)))
    if not root_path.is_dir():
        raise HTTPException(409, {"code": "editable_root_missing",
                                  "message": "the task's editable_path is not a directory"})
    return root_path, task


def _apply_file_amendment(rd: Path, files) -> tuple[dict, Any]:
    from looplab.core.pathsafe import contained_member
    from looplab.tools.patch import SurfacePolicy

    if not isinstance(files, list) or not 1 <= len(files) <= 16:
        raise HTTPException(400, "files must list 1..16 file changes")
    root, task = _editable_root(rd)
    surface = {str(p).replace("\\", "/") for p in task.get("edit_surface") or []}
    protect = {str(p).replace("\\", "/") for p in task.get("protect") or []}
    planned = []
    for item in files:
        if not isinstance(item, dict) or set(item) - {"path", "content", "content_b64",
                                                      "expected_sha256"}:
            raise HTTPException(400, "each file is {path, content|content_b64, expected_sha256}")
        rel = item.get("path")
        if not isinstance(rel, str) or not rel or len(rel) > 1024:
            raise HTTPException(400, "file path must be a relative path")
        rel = rel.replace("\\", "/")
        # `edit_surface` holds GLOBS (repo default `**/*.py`, composable `**/*`), so a literal
        # membership test matched almost nothing and a candidate-editable file could be rewritten
        # in the BASE. What the candidate may edit is the surface MINUS protect, under the same
        # rule the write gate uses (`tools/patch.py::SurfacePolicy`): a protected file (the
        # trainer, the scorer's harness) is exactly what this amendment exists to change.
        if SurfacePolicy(sorted(surface), sorted(protect), check_escapes=False).check(rel) is None:
            raise HTTPException(400, {
                "code": "edit_surface_file",
                "message": f"{rel!r} is on the candidate's edit surface; change it in a node, "
                           "not by amending the base"})
        target = contained_member(root, rel)
        if target is None:
            raise HTTPException(400, f"{rel!r} is outside the editable root")
        if ("content" in item) == ("content_b64" in item):
            raise HTTPException(400, f"{rel!r}: supply exactly one of content or content_b64")
        if "content" in item:
            if not isinstance(item["content"], str):
                raise HTTPException(400, f"{rel!r}: content must be text")
            after = item["content"].encode("utf-8")
        else:
            try:
                after = base64.b64decode(item["content_b64"], validate=True)
            except (ValueError, TypeError):
                raise HTTPException(400, f"{rel!r}: content_b64 is not base64") from None
        if len(after) > MAX_FILE_BYTES:
            raise HTTPException(413, f"{rel!r}: at most {MAX_FILE_BYTES} bytes per file")
        exists = target.is_file()
        if target.exists() and not exists:
            raise HTTPException(400, f"{rel!r} exists and is not a regular file")
        before = target.read_bytes() if exists else None
        before_sha = hashlib.sha256(before).hexdigest() if before is not None else None
        expected = item.get("expected_sha256", "__missing__")
        if expected == "__missing__":
            raise HTTPException(400, f"{rel!r}: expected_sha256 is required (null for a new file)")
        if expected != before_sha:
            raise HTTPException(409, {"code": "file_changed",
                                      "message": f"{rel!r} is not the content you amended",
                                      "path": rel, "current_sha256": before_sha})
        if before == after:
            raise HTTPException(400, f"{rel!r}: the new content equals the current content")
        planned.append((rel, target, before, after))
    receipts, written = [], []

    def undo():
        for target, before, mode in reversed(written):
            if before is None:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
            else:
                atomic_write_bytes(target, before, mode=mode)
    try:
        for rel, target, before, after in planned:
            mode = target.stat().st_mode & 0o7777 if before is not None else None
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(target, after, mode=mode)
            written.append((target, before, mode))
            receipts.append({"path": rel, "protected": rel in protect,
                             "before_sha256": hashlib.sha256(before).hexdigest() if before is not None else None,
                             "after_sha256": hashlib.sha256(after).hexdigest(),
                             "bytes": len(after), "diff": _diff(rel, before, after)})
    except BaseException:
        undo()
        raise
    total = sum(len(r["diff"]) for r in receipts)
    names = ", ".join(r["path"] for r in receipts)
    return ({"files": receipts, "root": str(root), "applied": True, "diff_chars": total,
             "effective": "lifecycles materialized after this point (the next resume records "
                          "workspace_changed)",
             "summary": f"base files amended: {names}"}, undo)


def _diff(rel: str, before: Optional[bytes], after: bytes) -> str:
    try:
        old = (before or b"").decode("utf-8").splitlines(True)
        new = after.decode("utf-8").splitlines(True)
    except UnicodeDecodeError:
        return "(binary content; compare before_sha256/after_sha256)"
    text = "".join(difflib.unified_diff(old, new, f"a/{rel}", f"b/{rel}"))
    return text if len(text) <= MAX_DIFF_CHARS // 4 else text[:MAX_DIFF_CHARS // 4] + "\n…(diff truncated)\n"


# ----------------------------------------------------------------------------- budget amendments
def agent_added_nodes(srv, rd: Path) -> int:
    """Nodes the agent token has already added by `budget_extend` (its durable command records,
    failed and rejected ones excluded)."""
    total = 0
    for _path, record in srv.commands._scan_command_records(rd, on_symlink="unreadable"):
        # Both agent parties (doc 82 П5): the scoped token and the open UI's declared client.
        if (not record or record.get("submitted_by") not in ("agent_token", "agent_client")
                or record.get("event_type") != "budget_extend"
                or record.get("status") in ("failed", "rejected", "timed_out")):
            continue
        value = (record.get("data") or {}).get("add_nodes")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            total += value
    return total


def agent_budget_level_refusal(rd: Path, policy: Optional[dict] = None
                               ) -> Optional[HTTPException]:
    """The operator's policy LEVEL for any agent budget extension — nodes, wall-clock or eval
    clocks alike. Spelled apart from the node cap below because it binds every budget field: while it
    lived inside `agent_budget_refusal` it ran only when `add_nodes` was sent, so an agent under
    `amendments: off` could still extend `max_seconds` / `timeout` (review 2026-10-15)."""
    policy = read_policy(rd) if policy is None else policy
    if POLICY_LEVELS.index(policy["amendments"]) < POLICY_LEVELS.index(_LEVEL_FOR_KIND["budget"]):
        return HTTPException(403, {"code": "amendment_not_permitted",
                                   "message": "the operator's policy refuses agent budget extensions",
                                   "retryable": False})
    return None


def agent_budget_refusal(srv, rd: Path, add_nodes) -> Optional[HTTPException]:
    """The operator's cap on agent-added nodes, for the raw command and the amendment alike."""
    if not isinstance(add_nodes, int) or isinstance(add_nodes, bool) or add_nodes <= 0:
        return None
    policy = read_policy(rd)
    cap = policy.get("max_agent_added_nodes")
    refused = agent_budget_level_refusal(rd, policy)
    if refused is not None:
        return refused
    if cap is None:
        return None
    used = agent_added_nodes(srv, rd)
    if used + add_nodes > cap:
        return HTTPException(403, {
            "code": "agent_budget_cap",
            "message": (f"the operator allows the agent to add at most {cap} node(s) to this run; "
                        f"{used} already added, {add_nodes} requested"),
            "remediation": "ask the operator to raise harness_policy.max_agent_added_nodes",
            "retryable": False})
    return None


def _publish_budget(srv, rd: Path, body, *, agent_token: bool) -> dict:
    budget = body.budget if isinstance(body.budget, dict) else {}
    if set(budget) - {"add_nodes"}:
        raise HTTPException(400, "a budget amendment takes {add_nodes}")
    add_nodes = budget.get("add_nodes")
    if not isinstance(add_nodes, int) or isinstance(add_nodes, bool) or not 1 <= add_nodes <= 1000:
        raise HTTPException(400, "add_nodes must be an integer in [1, 1000]")
    path = journal_path(rd)
    request_digest = hashlib.sha256(_canonical(body.request_payload())).hexdigest()
    rows, health = read_journal(rd)
    if not health["read_complete"]:
        raise journal_incomplete(rd)
    events = read_event_source(rd)
    state = fold(events)
    for old in rows:
        if old.get("run_uid") == state.run_uid and old.get("action_id") == body.action_id:
            if old.get("request_sha256") != request_digest:
                raise HTTPException(409, "amendment action_id was reused with different content")
            return {"ok": True, "replayed": True, "amendment": old}
    if agent_token:
        _require_level(read_policy(rd), "budget", True)
    # The command service owns admission (its own sequencer, generation fence and the cap check in
    # `normalize_control` for the agent token); the same key makes a retry resolve, not repeat.
    record = srv.commands.submit(rd, f"protocol-budget:{state.run_uid}:{body.action_id}",
                                 "budget_extend", {"add_nodes": add_nodes},
                                 expected_generation=body.expected_generation,
                                 agent_token=agent_token)
    try:
        with srv.commands.sequence(rd):
            with interprocess_lock(Path(str(path) + ".lock"), required=True):
                rows, _health = read_journal(rd)
                for old in rows:
                    if old.get("run_uid") == state.run_uid and old.get("action_id") == body.action_id:
                        return {"ok": True, "replayed": True, "amendment": old, "command": record}
                current = [row for row in rows if row.get("run_uid") == state.run_uid]
                events = read_event_source(rd)
                row = {"v": 1, "kind": "budget", "run_uid": state.run_uid,
                       "generation": run_generation_token(events), "action_id": body.action_id,
                       "request_sha256": request_digest, "author": author_of(agent_token),
                       "reason": body.reason, "recorded_at": time.time(),
                       "at_event_seq": events[-1].seq, "at_node_count": len(fold(events).nodes),
                       "version": len(current) + 1,
                       "amendment_id": f"am{len(current) + 1}-{request_digest[:10]}",
                       "payload": {"add_nodes": add_nodes, "command_id": record.get("id"),
                                   "command_status": record.get("status")},
                       "summary": f"+{add_nodes} node(s) via budget_extend"}
                append_jsonl_bytes_locked(path, _canonical(row))
                return {"ok": True, "replayed": False, "amendment": row, "command": record}
    except HTTPException:
        raise
    except (OSError, EventStoreLockError, ValueError, KeyError) as exc:
        raise HTTPException(503, "protocol journal unavailable") from exc


__all__ = ["AMENDMENT_KINDS", "DEFAULT_POLICY", "JOURNAL", "POLICY", "POLICY_LEVELS",
           "agent_added_nodes", "agent_budget_refusal", "author_of", "experiments_of",
           "journal_path", "normalize_experiment", "protocol_view", "publish_amendment",
           "read_journal", "read_policy", "write_policy"]
