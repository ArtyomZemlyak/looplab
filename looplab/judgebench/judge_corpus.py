"""Turn `runs/` into a versioned, regenerable bench dataset for the training-log monitor judge.

ONE judge is implemented end to end rather than a framework for all of them, and it is the
training-log monitor (`engine/train_monitor.py`) because it is the one whose label is strongest:
450 recorded decisions across six runs, and the node each one watched later either produced a
usable metric or did not.

## The row

One row per DECISION, not per LLM call. The monitor is agentic (`train_monitor_tools`), so one
decision is a whole tool loop — 2 to 6 `generation` spans that share a `phase_span`, ending in an
`emit` tool call carrying the `TrainingVerdict`. Without tools it is a single span whose `output` is
the JSON verdict. Both shapes are read; `tools_available` records which, because a candidate
replayed WITHOUT tools is not comparable to a verdict reached WITH them.

Each row carries four things that must never be conflated:

* `prompt` — the recorded input, split back into the ingredients `_training_verdict` assembled it
  from (`system`, `context`, `stage_context`, `trajectory`, `look_invitation`, `digest`). The split
  is verified by re-joining and comparing byte for byte; a row that fails the check keeps
  `messages` and sets `prompt_split_exact=false`. This is what makes it a PROMPT bench: a changed
  prompt can be re-rendered over the same evidence instead of only a changed model being run
  against a frozen string.
* `recorded` — the verdict the run actually got. **This is not the label.** It is the incumbent.
* `label` — derived from what happened NEXT, from facts the judge did not author.
* `provenance` — run, node, generation, span ids, timestamp, model, prompt digest.

## The label

Per decision, from the eval attempt the decision was watching:

1. Group each node's `stage_finished` rows into ATTEMPTS. All rows of one eval attempt are appended
   in a single burst (measured: a node's `mine`/`train`/`score` rows land within 150 ms of each
   other even when `mine` ran for an hour), so `ts - seconds` is NOT that stage's real window and
   cannot be used to place a decision. Clustering the bursts by an `ATTEMPT_GAP_S` gap and taking
   the first attempt that ends at or after the decision IS reliable, and is what this does.
2. The watched stage's status inside that attempt decides the label:

   | attempt status of the watched stage | label | why |
   |---|---|---|
   | `fail` / `expect_failed` / `check_failed` | `wasted` | the stage itself died; its compute bought nothing |
   | `ok` / `reused`, node later scored a degenerate metric | `wasted` | the stage ran to completion and produced a model that learned nothing — the case this judge exists for, and the reason stage status ALONE is not the label |
   | `ok` / `reused`, node later scored a usable metric | `productive` | |
   | `timeout` | `budget_exhausted` | see below |
   | anything else, or no attempt found | `unknown` | |

**`timeout` is its own class and is EXCLUDED from the primary confusion matrix on purpose.** The
compute was wasted, but the monitor's own system prompt tells it that "a run that is merely slow or
plateauing but still progressing is 'watch', not 'broken'" — so scoring a `timeout` as a missed
`broken` would penalise the judge for obeying its instructions. It is reported separately, where it
answers a DIFFERENT question the operator may want to ask (should the judge get budget authority?).
Keeping it inside the matrix would have moved 60 of 450 decisions and changed the headline.

**Degenerate metric.** `metric <= DEGENERATE_FRACTION * best_metric_in_run`, for `direction=max`
runs only; a `direction=min` run gets `unknown` and says so, because "degenerate" inverts and no
run in this corpus exercises it. The fraction is deliberately generous rather than an equality with
zero: `runs/e5small-dr-unified-v2` node 4 scored `2e-05`, which is not `0.0` and is just as dead.
It also, correctly, does NOT fire on `rubertlite-dr-unified-v6` node 4 (0.225 against a 0.728 best)
— that node trained fine to recall 0.726 and then SCORED somebody else's July checkpoint (docs/36).
The training was not wasted, the scoring was, and the monitor watching the train log was right to
call it healthy. A tighter rule would have labelled that node `wasted` and marked three correct
verdicts wrong.

## What the label does NOT support

* **`fault`.** `TrainingVerdict.fault` exists in the schema but not one recorded verdict in this
  corpus carries it (the field postdates every preserved run). It cannot be benched here at all.
* **A verdict on a stage whose failure the log could not show.** `check_failed` / `expect_failed`
  are decided by the engine AFTER the stage exits, over artifacts, not over the log the judge read.
  `stage_exit_code` and `label_basis` are on the row so this slice can be cut out.
* **Attributing a node's degenerate metric to the `mine` stage.** No run in this corpus declares
  stage ROLES (`log_role` is `work` on all 205 alert rows), so the engine itself could not identify
  which stage was the training. The row records the watched stage NAME and its position in the
  pipeline, and the scorer slices on it rather than this module guessing.

## Redaction

The recorded input is a training log tail plus the operator's own goal text and can carry the
candidate's code, its paths and anything it printed. A committed dataset is an egress boundary, so
every stored text field goes through `core/redact.py::redact_output_tail` — the SAME rule
`engine/audit.py::Engine._redact` applies to persisted tails, entropy pass included, not a second
one written here. Two honest limits: `redact_env_values` screens the values in the environment of
the process running the EXTRACTOR, which is not necessarily the environment the run had; and
redaction runs before the prompt split, so the split's byte-for-byte check is performed on the
redacted text.

## Where the dataset lives, and why it is committed

Committed, gzipped, under `tests/data/judge_bench/`. Measured: 450 rows are 4.8 MB raw and
**216 KB gzipped** — progress bars and repeated log lines compress by 22x. The alternative,
generating on demand, was rejected on one fact: `runs/` is not in the repository and does not exist
in CI or on any machine but the operator's, so an on-demand dataset cannot be read by a reviewer
judging a prompt change, cannot gate a merge, and cannot be diffed. 216 KB buys all three.

It is DERIVED and is never hand-edited: `build_dataset` rebuilds it from `runs/`, and
`tests/test_judge_bench.py` re-derives every label from the row's own recorded outcome fields, so an
edited label goes red offline, without `runs/` present.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
from pathlib import Path
from typing import Iterable, Iterator, Optional

from looplab.core.redact import redact_output_tail

# Bumped when the ROW SHAPE or a LABEL RULE changes — a candidate scored against v1 and one scored
# against v2 are not comparable, and the header carries the version so a stale comparison is visible
# rather than silent.
DATASET_SCHEMA = "looplab.judgebench.train_monitor.v1"

JUDGE_TRAIN_MONITOR = "train_monitor"

LABEL_WASTED = "wasted"
LABEL_PRODUCTIVE = "productive"
LABEL_BUDGET_EXHAUSTED = "budget_exhausted"
LABEL_UNKNOWN = "unknown"
# The closed label vocabulary. `budget_exhausted` is a member and is NOT a synonym for `wasted`;
# `score.py::PRIMARY_LABELS` is what decides which members enter the confusion matrix.
LABELS = (LABEL_WASTED, LABEL_PRODUCTIVE, LABEL_BUDGET_EXHAUSTED, LABEL_UNKNOWN)

# The verdict vocabulary, mirrored from `engine/train_monitor.py::TrainingVerdict.status`. A copy
# rather than an import because a bench that moves when the production Literal moves cannot detect
# that it moved; `tests/test_judge_bench.py` asserts the two still agree, which is the point.
VERDICTS = ("healthy", "watch", "broken")

# A stage whose status is one of these bought nothing. `timeout` is deliberately absent — see the
# module docstring; it is its own label class.
STAGE_FAILED_STATUSES = frozenset({"fail", "expect_failed", "check_failed", "error"})
STAGE_OK_STATUSES = frozenset({"ok", "reused"})
STAGE_TIMEOUT_STATUSES = frozenset({"timeout"})

# A node terminal that says nothing about the eval — the node was never run against its own idea.
NON_EVAL_FAILURE_REASONS = frozenset({"superseded", "idea_rejected_pre_eval", "cancelled"})

# `metric <= DEGENERATE_FRACTION * run_best` is dead, not merely bad. 0.05 is a wide margin on
# purpose: the corpus's two dead nodes are 0.0 and 2e-05 against a 0.793 best (both < 0.0001x), and
# its worst LIVE node is 0.225 against 0.728 (0.31x), so nothing in the corpus is near the line and
# the rule's exact value cannot be tuned to flatter a result.
DEGENERATE_FRACTION = 0.05

# All `stage_finished` rows of one eval attempt are appended in one burst; consecutive rows for a
# node more than this far apart are different attempts. Measured spread inside a burst: < 0.15 s.
ATTEMPT_GAP_S = 60.0

# The fixed scaffolding of `_training_verdict`'s user message. The digest sits between them, so the
# log tail — the evidence — can be recovered exactly and a re-rendered prompt can reuse it.
_TAIL_HEADER = "LIVE TRAINING LOG (recent tail):\n"
_TAIL_FOOTER = "\n\nClassify this run's health from the log evidence above."
_LOOK_PREFIX = "YOU CAN LOOK FURTHER."
_STAGE_CONTEXT_PREFIXES = (
    "This is the live log of pipeline stage ",
    "NOTE: this log could not be attributed",
    "This eval runs ONE command that both trains and scores",
    "This eval runs the pipeline ",
)
_STAGE_NAME_RE = re.compile(r"pipeline stage '([^']+)' \(stage (\d+) of (\d+); the pipeline is ([^)]*)\)")

# Travels IN the artefact, printed by every report and stored in the dataset header, because a
# caveat that lives only in a doc is a caveat nobody reading the number sees.
CORPUS_LIMITS = (
    "SCOPE: this corpus is seven preserved runs of ONE task family (ESCI dense retrieval, two "
    "backbones), driven by ONE operator, judged by ONE model. A score measured here is evidence "
    "about THIS deployment and is not a general claim about the judge, the prompt or the model. "
    "Optimising a prompt against it will overfit it. "
    "AGREEMENT WITH THE RECORDED VERDICT IS NOT ACCURACY: the recorded verdict is the incumbent, "
    "not the truth, and 448 of 450 of them were produced under an OLDER system prompt than the one "
    "that ships today."
)


def _redact(text: str) -> str:
    """The one egress screen, and deliberately the SAME one persisted tails already go through."""
    if not text:
        return text or ""
    return redact_output_tail(text, entropy=True)


def _iter_jsonl(path: Path) -> Iterator[dict]:
    """Stream a possibly-huge JSONL (`spans.jsonl` is 242 MB on one run), skipping torn lines."""
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue  # a partially-flushed tail line is not a corpus defect


def _split_prompt(messages: list) -> dict:
    """Recover the ingredients `_training_verdict` concatenated, verified by re-joining.

    Returns a dict with `prompt_split_exact` telling the truth about whether the recovery is
    lossless. A caller re-rendering a changed prompt MUST honour that flag: an inexact split means
    the ingredients do not reproduce the message, so only `messages` is safe to replay.
    """
    out = {"prompt_split_exact": False, "system": "", "context": "", "stage_context": "",
           "trajectory": "", "look_invitation": "", "digest": ""}
    if not (isinstance(messages, list) and len(messages) == 2):
        return out
    system = messages[0].get("content") or ""
    user = messages[1].get("content") or ""
    out["system"] = system
    if _TAIL_HEADER not in user or not user.endswith(_TAIL_FOOTER):
        return out
    head, _, rest = user.partition(_TAIL_HEADER)
    out["digest"] = rest[: len(rest) - len(_TAIL_FOOTER)]
    blocks = head.split("\n\n")
    # Every block is emitted with a trailing "\n\n", so the split leaves one empty tail element.
    if blocks and blocks[-1] == "":
        blocks = blocks[:-1]
    remaining = list(blocks)
    if remaining and remaining[-1].startswith(_LOOK_PREFIX):
        out["look_invitation"] = remaining.pop()
    stage_at = next((i for i, b in enumerate(remaining)
                     if b.startswith(_STAGE_CONTEXT_PREFIXES)), None)
    if stage_at is not None:
        out["stage_context"] = remaining[stage_at]
        out["context"] = "\n\n".join(remaining[:stage_at])
        out["trajectory"] = "\n\n".join(remaining[stage_at + 1:])
    else:
        out["context"] = "\n\n".join(remaining)
    out["prompt_split_exact"] = render_prompt(out) == messages
    return out


def render_prompt(parts: dict) -> list:
    """Re-render `_training_verdict`'s messages from split ingredients — the replay seam.

    This is the inverse of `_split_prompt` and the reason a PROMPT change is benchable: hand it a
    row's `prompt` with `system` (or any other ingredient) replaced and it produces the message list
    the candidate should be asked, over the same recorded evidence.
    """
    pieces = [parts.get(k) or "" for k in ("context", "stage_context", "trajectory",
                                           "look_invitation")]
    body = "".join(p + "\n\n" for p in pieces if p)
    user = body + _TAIL_HEADER + (parts.get("digest") or "") + _TAIL_FOOTER
    return [{"role": "system", "content": parts.get("system") or ""},
            {"role": "user", "content": user}]


def messages_of(row: dict) -> list:
    """The message list a row stands for. THE one way to read a row's prompt.

    An exactly-split row stores no `messages`, because `render_prompt` reproduces them byte for
    byte and the copy cost 156 KB of the committed file. A row that failed the split stores the
    original and cannot be re-rendered.
    """
    stored = row.get("messages")
    if stored:
        return stored
    return render_prompt(row.get("prompt") or {})


def _watched_stage(stage_context: str) -> dict:
    """What the prompt itself says about which stage is being judged. Deterministic, no guessing."""
    match = _STAGE_NAME_RE.search(stage_context or "")
    if not match:
        return {"stage": None, "stage_index": None, "stage_count": None, "pipeline": None}
    return {"stage": match.group(1), "stage_index": int(match.group(2)),
            "stage_count": int(match.group(3)),
            "pipeline": [s.strip() for s in match.group(4).split("->")]}


def _verdict_from_group(spans: list) -> dict:
    """The `TrainingVerdict` a decision actually reached, from either recorded shape.

    Agentic: the last `emit` tool call in the loop. Non-agentic: JSON in the final span's `output`.
    Returns `{}` when neither is present — recorded as `unknown` rather than dropped, because a
    decision that produced no parseable verdict is a real failure mode a candidate can also have.
    """
    verdict: dict = {}
    for span in spans:
        attrs = span.get("attributes") or {}
        for call in (attrs.get("tool_calls") or []):
            if not isinstance(call, dict) or call.get("name") != "emit":
                continue
            try:
                args = json.loads(call.get("arguments") or "{}")
            except ValueError:
                continue
            if isinstance(args, dict) and args.get("status"):
                verdict = args
    if verdict:
        return verdict
    tail = (spans[-1].get("attributes") or {}).get("output")
    if isinstance(tail, str) and tail.strip().startswith("{"):
        try:
            parsed = json.loads(tail)
        except ValueError:
            return {}
        if isinstance(parsed, dict) and parsed.get("status"):
            return parsed
    return {}


def _stage_attempts(events_path: Path) -> dict:
    """node_id -> [(attempt_end_ts, {stage_name: (status, exit_code, seconds)})], oldest first.

    Bursts, not windows: see the module docstring for the measurement that rules out `ts - seconds`.
    """
    per_node: dict = {}
    for row in _iter_jsonl(events_path):
        if row.get("type") != "stage_finished":
            continue
        data = row.get("data") or {}
        per_node.setdefault(data.get("node_id"), []).append(
            (row.get("seq") or 0, float(row.get("ts") or 0.0), data.get("name"),
             data.get("status"), data.get("exit_code"), data.get("seconds")))
    attempts: dict = {}
    for node_id, rows in per_node.items():
        rows.sort()
        bucket: list = []
        grouped: list = []
        for row in rows:
            if bucket and row[1] - bucket[-1][1] > ATTEMPT_GAP_S:
                grouped.append(bucket)
                bucket = []
            bucket.append(row)
        if bucket:
            grouped.append(bucket)
        attempts[node_id] = [
            (max(r[1] for r in group), {r[2]: (r[3], r[4], r[5]) for r in group})
            for group in grouped]
    return attempts


def _node_outcomes(events_path: Path, direction: str) -> dict:
    """node_id -> the terminal fact the label reads. The judge authored none of this."""
    outcomes: dict = {}
    for row in _iter_jsonl(events_path):
        kind = row.get("type")
        if kind not in ("node_evaluated", "node_failed"):
            continue
        data = row.get("data") or {}
        node_id = data.get("node_id")
        if kind == "node_evaluated":
            outcomes[node_id] = {"terminal": "node_evaluated", "metric": data.get("metric"),
                                 "eval_seconds": data.get("eval_seconds"), "reason": None,
                                 "ts": row.get("ts")}
        elif node_id not in outcomes:
            outcomes[node_id] = {"terminal": "node_failed", "metric": None,
                                 "eval_seconds": data.get("eval_seconds"),
                                 "reason": data.get("reason"),
                                 "never_evaluated": bool(data.get("never_evaluated")),
                                 "ts": row.get("ts")}
    metrics = [o["metric"] for o in outcomes.values()
               if isinstance(o.get("metric"), (int, float)) and not isinstance(o.get("metric"), bool)]
    best = max(metrics) if (metrics and direction == "max") else None
    for outcome in outcomes.values():
        outcome["run_best_metric"] = best
        outcome["degenerate"] = _is_degenerate(outcome.get("metric"), best, direction)
    return outcomes


def _is_degenerate(metric, best, direction: str) -> Optional[bool]:
    """None means UNDECIDABLE, and is not the same answer as False — see the docstring's min case."""
    if direction != "max" or best is None or best <= 0:
        return None
    if not isinstance(metric, (int, float)) or isinstance(metric, bool):
        return None
    return bool(metric <= DEGENERATE_FRACTION * best)


def _label(stage: Optional[str], attempt: Optional[dict], outcome: Optional[dict]) -> dict:
    """The per-decision label and the BASIS it rests on. The basis is stored so a reader can cut a
    slice out rather than having to trust the whole rule."""
    if attempt is None or stage is None:
        return {"label": LABEL_UNKNOWN, "label_basis": "no_stage_attempt",
                "stage_status": None, "stage_exit_code": None}
    status, exit_code, _seconds = attempt.get(stage, (None, None, None))
    base = {"stage_status": status, "stage_exit_code": exit_code}
    if status in STAGE_TIMEOUT_STATUSES:
        return {**base, "label": LABEL_BUDGET_EXHAUSTED, "label_basis": "stage_timeout"}
    if status in STAGE_FAILED_STATUSES:
        return {**base, "label": LABEL_WASTED, "label_basis": "stage_failed"}
    if status not in STAGE_OK_STATUSES:
        return {**base, "label": LABEL_UNKNOWN, "label_basis": "stage_status_unknown"}
    if outcome is None:
        return {**base, "label": LABEL_UNKNOWN, "label_basis": "no_node_terminal"}
    if outcome.get("terminal") == "node_evaluated":
        degenerate = outcome.get("degenerate")
        if degenerate is None:
            return {**base, "label": LABEL_UNKNOWN, "label_basis": "metric_undecidable"}
        return {**base, "label": LABEL_WASTED if degenerate else LABEL_PRODUCTIVE,
                "label_basis": "node_metric_degenerate" if degenerate else "node_metric_usable"}
    if outcome.get("never_evaluated") or outcome.get("reason") in NON_EVAL_FAILURE_REASONS:
        return {**base, "label": LABEL_UNKNOWN, "label_basis": "node_never_evaluated"}
    # The watched stage SUCCEEDED and something later in the same node did not. That is not this
    # stage's waste and the judge could not have seen it, so the stage is productive and the row
    # says exactly which node-level reason it is declining to charge here.
    return {**base, "label": LABEL_PRODUCTIVE,
            "label_basis": "stage_ok_node_failed_elsewhere:%s" % (outcome.get("reason") or "?")}


def rederive_label(row: dict) -> dict:
    """Recompute a row's label from the row's OWN stored facts, through the production rule.

    The dataset is derived and must never be hand-edited, but the file is committed, so "never" has
    to be enforceable by something. This is that something: `tests/test_judge_bench.py` re-derives
    every row and compares, offline, with no `runs/` present. Editing a label alone goes red.
    Editing the stage status and the metric and the label together does not — that forgery is
    caught by the regeneration check, which needs the runs.
    """
    label = row.get("label") or {}
    context = row.get("context") or {}
    stage = context.get("stage")
    status = label.get("stage_status")
    attempt = None if label.get("label_basis") == "no_stage_attempt" else {
        stage: (status, label.get("stage_exit_code"), None)}
    terminal = label.get("node_terminal")
    outcome = None if terminal is None else {
        "terminal": terminal,
        "metric": label.get("node_metric"),
        "reason": label.get("node_reason"),
        "never_evaluated": label.get("node_never_evaluated"),
        "degenerate": _is_degenerate(label.get("node_metric"), label.get("run_best_metric"),
                                     str(context.get("direction") or "max")),
    }
    return _label(stage, attempt, outcome)


def extract_run(run_dir) -> list:
    """Every training-log-monitor DECISION in one run, as bench rows. Reads only; writes nothing."""
    run_dir = Path(run_dir)
    spans_path = run_dir / "spans.jsonl"
    events_path = run_dir / "events.jsonl"
    if not spans_path.exists() or not events_path.exists():
        return []
    try:
        task = json.loads((run_dir / "task.snapshot.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        task = {}
    direction = str(task.get("direction") or "max")

    groups: dict = {}
    for span in _iter_jsonl(spans_path):
        attrs = span.get("attributes") or {}
        if span.get("kind") != "generation" or attrs.get("phase") != JUDGE_TRAIN_MONITOR:
            continue
        groups.setdefault((attrs.get("phase_span"), attrs.get("node_id")), []).append(span)

    attempts = _stage_attempts(events_path)
    outcomes = _node_outcomes(events_path, direction)

    rows = []
    for (phase_span, node_id), spans in groups.items():
        spans.sort(key=lambda s: s.get("start") or 0.0)
        first = spans[0]
        attrs = first.get("attributes") or {}
        messages = attrs.get("input")
        if not isinstance(messages, list):
            continue
        redacted = [{"role": m.get("role"), "content": _redact(m.get("content") or "")}
                    for m in messages if isinstance(m, dict)]
        parts = _split_prompt(redacted)
        stage = _watched_stage(parts["stage_context"])
        verdict = _verdict_from_group(spans)
        started = float(first.get("start") or 0.0)
        attempt = next((smap for end, smap in attempts.get(node_id, []) if started <= end), None)
        outcome = outcomes.get(node_id)
        label = _label(stage["stage"], attempt, outcome)
        tool_names = sorted({c.get("name") for s in spans
                             for c in ((s.get("attributes") or {}).get("tool_calls") or [])
                             if isinstance(c, dict) and c.get("name")})
        rows.append({
            "schema": DATASET_SCHEMA,
            "judge": JUDGE_TRAIN_MONITOR,
            "case_id": "%s:n%s:%s" % (run_dir.name, node_id, phase_span),
            "provenance": {
                "run": run_dir.name,
                "run_id": first.get("run_id"),
                "node_id": node_id,
                "generation": attrs.get("generation"),
                "phase_span": phase_span,
                "trace_id": first.get("trace_id"),
                "span_ids": [s.get("span_id") for s in spans],
                "ts": started,
                "model": attrs.get("model"),
                "llm_calls": len(spans),
                "tools_available": bool(parts["look_invitation"]),
                "tools_used": tool_names,
                "system_prompt_sha256": hashlib.sha256(
                    (parts["system"] or "").encode("utf-8")).hexdigest()[:16],
            },
            "prompt": parts,
            # Only when the split is NOT lossless. Storing both doubled the committed file (343 KB
            # against 187 KB) for a byte-identical copy of what `render_prompt` reproduces; read a
            # row's messages through `messages_of`, never off the key.
            **({} if parts["prompt_split_exact"] else {"messages": redacted}),
            "recorded": {
                "status": verdict.get("status"),
                "fault": verdict.get("fault"),
                "confidence": verdict.get("confidence"),
                "recheck_after_s": verdict.get("recheck_after_s"),
                "reason": _redact(str(verdict.get("reason") or "")),
            },
            "context": {
                **stage,
                "direction": direction,
                "attempt_end_ts": next((end for end, _s in attempts.get(node_id, [])
                                        if started <= end), None),
            },
            # Every FACT the rule consumed is stored beside the verdict it produced, so
            # `rederive_label` can recompute the label offline from the row alone — which is what
            # makes a hand-edited label a red test on a machine that has no `runs/` at all.
            "label": {
                **label,
                "node_terminal": (outcome or {}).get("terminal"),
                "node_metric": (outcome or {}).get("metric"),
                "node_reason": (outcome or {}).get("reason"),
                "node_never_evaluated": bool((outcome or {}).get("never_evaluated")),
                "run_best_metric": (outcome or {}).get("run_best_metric"),
                "degenerate_fraction": DEGENERATE_FRACTION,
            },
        })
    rows.sort(key=lambda r: (r["provenance"]["run"], r["provenance"]["ts"], r["case_id"]))
    return rows


def build_dataset(run_dirs: Iterable) -> dict:
    """`{"header": {...}, "rows": [...]}` — the whole regenerable artefact."""
    rows: list = []
    sources = []
    for run_dir in run_dirs:
        run_rows = extract_run(run_dir)
        if not run_rows:
            continue
        sources.append({"run": Path(run_dir).name, "rows": len(run_rows)})
        rows.extend(run_rows)
    rows.sort(key=lambda r: (r["provenance"]["run"], r["provenance"]["ts"], r["case_id"]))
    return {"header": {
        "schema": DATASET_SCHEMA,
        "judge": JUDGE_TRAIN_MONITOR,
        "rows": len(rows),
        "sources": sources,
        "labels": list(LABELS),
        "verdicts": list(VERDICTS),
        "degenerate_fraction": DEGENERATE_FRACTION,
        "redaction": "core.redact.redact_output_tail(entropy=True)",
        "limits": CORPUS_LIMITS,
    }, "rows": rows}


def write_dataset(dataset: dict, path) -> Path:
    """Header line first, then one row per line, gzipped. Sorted + `sort_keys` so a rebuild from the
    same runs is BYTE-IDENTICAL and the committed file's diff is only ever real change."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(
        [json.dumps(dataset["header"], sort_keys=True, ensure_ascii=False)]
        + [json.dumps(r, sort_keys=True, ensure_ascii=False) for r in dataset["rows"]]) + "\n"
    # mtime=0: gzip stamps the current time by default, which would make every rebuild a diff.
    with gzip.GzipFile(filename="", mode="wb", fileobj=path.open("wb"), compresslevel=9,
                       mtime=0) as handle:
        handle.write(payload.encode("utf-8"))
    return path


def read_dataset(path) -> dict:
    """The inverse of `write_dataset`. Raises on an empty or truncated file rather than returning a
    dataset with no rows, which would score as a vacuous pass."""
    with gzip.open(Path(path), "rt", encoding="utf-8") as handle:
        lines = [line for line in handle.read().splitlines() if line.strip()]
    if not lines:
        raise ValueError("judge-bench dataset is empty: %s" % path)
    header = json.loads(lines[0])
    rows = [json.loads(line) for line in lines[1:]]
    if header.get("rows") != len(rows):
        raise ValueError("judge-bench dataset header claims %s rows, file has %s"
                         % (header.get("rows"), len(rows)))
    return {"header": header, "rows": rows}


# Resolved from THIS file, not from the cwd: the bench is run from wherever the operator
# happens to be standing, and a cwd-relative default silently means "no dataset here".
DEFAULT_DATASET = (Path(__file__).resolve().parents[2]
                  / "tests" / "data" / "judge_bench" / "train_monitor.v1.jsonl.gz")
