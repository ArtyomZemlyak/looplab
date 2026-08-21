"""HOW DOES THIS NODE DIFFER FROM THAT ONE — the tool an agent asks before it reads a metric.

THE DEFECT THIS EXISTS TO CLOSE
-------------------------------
An agent proposing the next experiment is handed a table of `node -> metric`. It reasons about the
NUMBERS without being able to see what actually differs between the rows, so it "improves" a
parameter that was never the difference, or re-proposes a difference already tried. The operator's
instruction was to fix this at the level of TOOLING rather than by adding a sentence to a prompt:
give the agent something it can ASK, so the answer is a fact instead of an instruction it may skip.

WHY THE EVENT LOG AND NOT THE WORKDIR
-------------------------------------
`node_created` and `node_repaired` carry `files` as `path -> full text`. That is the durable,
byte-exact record of what the Developer wrote, and it survives the workdir being reaped — which the
workdirs are, routinely, while the metric they produced stays in the record forever. Diffing
directories would also drown the answer in noise: a plain `diff -rq` between two real nodes of
`rubertlite-dr-unified-v8` returns `__pycache__/*.pyc`, `train.log`, `.looplab-manifest` and
`.looplab-metrics-attempt.json` before it reaches one line of experiment source.

THE PROPERTY THAT MATTERS MOST HERE
-----------------------------------
**An empty diff and a missing diff must never render the same.** "These two nodes are identical in
`train.py`" and "I could not read either node's `train.py`" are opposite facts, and a tool that
returns "" for both teaches an agent that nothing changed. Every section below either reports a
difference, states that it compared and found none, or states what it could not recover — never
silence. This is the same rule `comparability_notice` follows for `UNKNOWN`, and for the same reason:
silence is read as assent by everything downstream.

The `params` section carries the disagreement that motivated the whole thing: `Idea.params` is a
PROPOSAL, and under `params_style: "none"` the engine applies nothing — the Developer realises the
idea by editing the repo. Across every run on disk that proposal diverges from what ran on 9.0% of
comparisons (41 of 457), and the e5 champion at 0.793426 is recorded as batch 8192 / 15 epochs while
having run batch 512 / 3 epochs. So this tool always shows PROPOSED and APPLIED side by side and
never collapses them into one column called "params".
"""
from __future__ import annotations

import difflib
import json
from typing import Optional

from looplab.engine.comparability import SAME, comparability_notice, comparability_status
from looplab.tools._base import RESULT_CAP, fn_spec

# What one `diff_nodes` answer may spend. The loop hard-caps every tool result at `RESULT_CAP` and
# cuts the TAIL there, so a budget derived from that cap is the only one that cannot silently eat
# its own closing receipt. The per-file bound exists on top of it because one 18,000-character
# `config.yaml` rewritten wholesale would otherwise consume the entire answer and hide the
# three-line `train.py` change that is the actual difference.
_MAX_ANSWER = RESULT_CAP - 600
_MAX_FILE_DIFF_LINES = 120
_MAX_FILES_SHOWN = 12
_DIFF_CONTEXT = 2

# The paths a node diff is never about. These are the engine's own bookkeeping and the eval's
# output, not the experiment: they differ between ANY two nodes, so including them would make every
# diff look large and bury the part a human or an agent would act on.
_NOISE_SUFFIXES = (".pyc", ".log", ".lock", ".safetensors", ".parquet", ".bin", ".pt", ".onnx")
_NOISE_NAMES = (".looplab-manifest", ".looplab-metrics-attempt.json")
_NOISE_PARTS = ("__pycache__", ".git", ".venv", "experiments")

# Every way this module declines to answer, as a closed vocabulary. A caller distinguishing "no
# difference" from "no answer" must not do it by looking for a parenthesis — a real diff line can
# start with one.
NOT_RECOVERABLE = "NOT RECOVERABLE"
NO_DIFFERENCE = "NO DIFFERENCE"


def _is_noise(path: str) -> bool:
    p = str(path or "")
    if not p or p in _NOISE_NAMES:
        return True
    if p.endswith(_NOISE_SUFFIXES):
        return True
    parts = p.replace("\\", "/").split("/")
    return any(part in _NOISE_PARTS for part in parts)


def node_record(state, node_id: int) -> Optional[dict]:
    """Everything this module knows about one node, read off the FOLDED `RunState`, or None.

    None when the run has no such node — which is a different answer from "the node exists and is
    identical to the other one", and the two must never render the same.

    THE FOLD IS THE SOURCE, not the raw rows, and that is deliberate: the fold already applies every
    repair, so `node.files` is the file set the node's LAST attempt ran. A node repaired five times
    produced its metric from the fifth write, and showing the originally-created files beside that
    metric is the same class of lie this whole tool exists to end. It is also the state every other
    run-introspection provider binds (`RunTools.bind_state`), so an agent cannot get one answer from
    `read_experiment` and a contradicting one from here.

    Duck-typed on purpose: anything with `.id`, `.files`, `.idea`, `.metric` and
    `.metric_provenance` is a node here. Never raises — a tool on an agent's request path that
    throws on a half-built state is a worse failure than one that says it could not read it.
    """
    nodes = getattr(state, "nodes", None) or {}
    node = None
    try:
        node = nodes.get(node_id)
    except Exception:  # noqa: BLE001 — a mapping that is not one is "no such node", not a crash
        node = None
    if node is None:
        return None
    files = getattr(node, "files", None)
    idea = getattr(node, "idea", None)
    params = getattr(idea, "params", None)
    if params is None and isinstance(idea, dict):
        params = idea.get("params")
    prov = getattr(node, "metric_provenance", None)
    return {"node_id": node_id,
            "files": {str(k): v for k, v in (files or {}).items() if isinstance(v, str)},
            "params": params if isinstance(params, dict) else {},
            "generation": getattr(node, "attempt", None),
            "status": str(getattr(node, "status", "") or ""),
            "metric": getattr(node, "metric", None),
            "provenance": prov if isinstance(prov, dict) else None}


def known_nodes(state) -> list[int]:
    """The node ids this run has, sorted. Empty when the state carries none."""
    nodes = getattr(state, "nodes", None) or {}
    try:
        return sorted(int(n) for n in nodes if isinstance(n, int) and not isinstance(n, bool))
    except Exception:  # noqa: BLE001
        return []


def _declared(record: dict) -> dict:
    return record.get("params") or {}


def _applied(record: dict) -> Optional[dict]:
    prov = record.get("provenance")
    if not isinstance(prov, dict):
        return None
    applied = prov.get("applied_params")
    return applied if isinstance(applied, dict) else None


def diff_files(left: dict, right: dict, *, max_files: int = _MAX_FILES_SHOWN,
               max_lines: int = _MAX_FILE_DIFF_LINES) -> list[str]:
    """The CODE section: a real unified diff per changed file, bounded, and honest at both edges.

    Bounded twice and both bounds announce themselves. A truncated diff that does not say it was
    truncated is indistinguishable from a complete one, and an agent acting on it believes it has
    seen the whole change.
    """
    lrec, rrec = left.get("files") or {}, right.get("files") or {}
    if not lrec and not rrec:
        return [f"code: {NOT_RECOVERABLE} — neither node's file set is in the record "
                f"(no `node_created`/`node_repaired` row carried `files`)."]
    if not lrec or not rrec:
        missing = left if not lrec else right
        return [f"code: {NOT_RECOVERABLE} — node {missing['node_id']}'s file set is not in the "
                f"record, so nothing can be compared against it. This is NOT 'no difference'."]

    names = sorted(set(lrec) | set(rrec))
    considered = [n for n in names if not _is_noise(n)]
    skipped = len(names) - len(considered)
    changed = [n for n in considered if lrec.get(n) != rrec.get(n)]
    out: list[str] = []
    if not changed:
        out.append(f"code: {NO_DIFFERENCE} — compared {len(considered)} experiment files "
                   f"byte for byte and every one is identical"
                   + (f" ({skipped} engine/output files not compared)." if skipped else "."))
        return out

    out.append(f"code: {len(changed)} of {len(considered)} experiment files differ"
               + (f" ({skipped} engine/output files not compared)" if skipped else "")
               + ": " + ", ".join(changed[:max_files])
               + (f" … and {len(changed) - max_files} more" if len(changed) > max_files else ""))
    for name in changed[:max_files]:
        lt, rt = lrec.get(name), rrec.get(name)
        if lt is None or rt is None:
            side = right["node_id"] if lt is None else left["node_id"]
            out.append(f"  --- {name}: present ONLY on node {side}")
            continue
        lines = list(difflib.unified_diff(
            lt.splitlines(), rt.splitlines(),
            fromfile=f"node {left['node_id']}/{name}", tofile=f"node {right['node_id']}/{name}",
            lineterm="", n=_DIFF_CONTEXT))
        if len(lines) > max_lines:
            dropped = len(lines) - max_lines
            lines = lines[:max_lines] + [f"  … {dropped} more diff lines in {name}, not shown"]
        out.extend("  " + ln for ln in lines)
    return out


def diff_params(left: dict, right: dict) -> list[str]:
    """The PARAMS section — PROPOSED and APPLIED as two separate columns, never merged.

    `Idea.params` is a proposal. Under `params_style: "none"` the engine applies nothing and the
    Developer realises the idea by editing the repo, so a deviation is legitimate and expected. What
    is not legitimate is presenting the proposal as the thing that ran, which is what put batch 8192
    into a task goal on the strength of a champion that ran 512.
    """
    out: list[str] = []
    ld, rd = _declared(left), _declared(right)
    if not ld and not rd:
        out.append(f"proposed params: {NOT_RECOVERABLE} — neither node's idea declares any.")
    else:
        keys = sorted(set(ld) | set(rd))
        moved = [k for k in keys if ld.get(k) != rd.get(k)]
        if not moved:
            out.append(f"proposed params: {NO_DIFFERENCE} — both nodes declare the same "
                       f"{len(keys)} coordinates.")
        else:
            out.append(f"proposed params: {len(moved)} of {len(keys)} coordinates differ")
            for k in moved:
                out.append(f"    {k}: node {left['node_id']}={ld.get(k, '(absent)')}  ->  "
                           f"node {right['node_id']}={rd.get(k, '(absent)')}")

    la, ra = _applied(left), _applied(right)
    if la is None and ra is None:
        out.append(f"applied params: {NOT_RECOVERABLE} — neither node recorded what actually ran "
                   "(both predate the applied-params record, or no carrier could be read). "
                   "The proposed values above are a PROPOSAL and may not be what ran.")
        return out
    for rec, side in ((la, left), (ra, right)):
        if rec is None:
            out.append(f"applied params: node {side['node_id']} — {NOT_RECOVERABLE}; "
                       "no applied-params record, so it cannot be compared on what ran.")
            continue
        checked, declared = rec.get("checked"), rec.get("declared")
        diverged = [d for d in (rec.get("diverged") or []) if isinstance(d, dict)]
        out.append(f"applied params: node {side['node_id']} — {rec.get('authority', '?')} authority, "
                   f"{checked} of {declared} declared coordinates answered by the carrier, "
                   f"{len(diverged)} diverge from the proposal")
        for d in diverged[:8]:
            out.append(f"    {d.get('param')}: DECLARED {d.get('declared')} but "
                       f"{d.get('match') or d.get('line')} APPLIED {d.get('applied')}")
    if isinstance(la, dict) and isinstance(ra, dict):
        lv, rv = la.get("applied") or {}, ra.get("applied") or {}
        keys = sorted(set(lv) | set(rv))
        moved = [k for k in keys if lv.get(k) != rv.get(k)]
        if not keys:
            out.append(f"what actually ran: {NOT_RECOVERABLE} — both records are present but "
                       "neither resolved a value.")
        elif not moved:
            out.append(f"what actually ran: {NO_DIFFERENCE} — the {len(keys)} resolved "
                       "coordinates are equal. Whatever separates these two nodes is in the CODE, "
                       "not in these numbers.")
        else:
            out.append(f"what actually ran: {len(moved)} of {len(keys)} resolved coordinates differ")
            for k in moved:
                out.append(f"    {k}: node {left['node_id']}={lv.get(k, '(absent)')}  ->  "
                           f"node {right['node_id']}={rv.get(k, '(absent)')}")
    return out


def diff_metrics(left: dict, right: dict) -> list[str]:
    """The METRIC section, with the comparability verdict ATTACHED rather than offered separately.

    A number and the question "may these two be ranked" belong on the same line. `comparability_
    notice` is deliberately non-empty for `UNKNOWN` as well as `DIFFERENT`, because silence beside a
    foreign number is read as assent — this section inherits that and never prints a bare delta.
    """
    out: list[str] = []
    for side in (left, right):
        m = side.get("metric")
        if m is None:
            out.append(f"metric: node {side['node_id']} produced NO metric "
                       "(failed, aborted, or still running) — there is no number to attribute.")
        else:
            out.append(f"metric: node {side['node_id']} = {m}")
    lm, rm = left.get("metric"), right.get("metric")

    def _comp(rec):
        prov = rec.get("provenance")
        return prov.get("comparability") if isinstance(prov, dict) else None

    status = comparability_status(_comp(left), _comp(right))
    if lm is None or rm is None:
        out.append("delta: not computed — one side has no metric.")
    elif status == SAME:   # imported, never re-spelled — one rule, one behaviour
        out.append(f"delta: {rm - lm:+.6f} (node {right['node_id']} minus node {left['node_id']}), "
                   "and the two were measured against the same evaluation.")
    else:
        out.append(f"delta: {rm - lm:+.6f} (node {right['node_id']} minus node "
                   f"{left['node_id']}) — BUT " + comparability_notice(_comp(left), _comp(right)))
    return out


def render_diff(left: dict, right: dict, *, sections=("code", "params", "metric"),
                max_answer: int = _MAX_ANSWER) -> str:
    """One assembled answer, bounded, saying at the end what the bound cost."""
    head = (f"=== node {left['node_id']} -> node {right['node_id']} ===\n"
            f"showing node {left['node_id']}'s "
            f"{'generation ' + str(left['generation']) if left.get('generation') is not None else 'record'}"
            f" against node {right['node_id']}'s "
            f"{'generation ' + str(right['generation']) if right.get('generation') is not None else 'record'}"
            " — the LAST files each node ran, not the first it was created with.")
    body: list[str] = []
    if "metric" in sections:
        body += diff_metrics(left, right)
    if "params" in sections:
        body += diff_params(left, right)
    if "code" in sections:
        body += diff_files(left, right)
    text = head + "\n" + "\n".join(body)
    if len(text) <= max_answer:
        return text
    kept = text[:max_answer]
    return kept + (f"\n… ANSWER TRUNCATED at {max_answer:,} characters; "
                   f"{len(text) - max_answer:,} characters not shown. Ask for one section at a "
                   "time (section=\"code\" | \"params\" | \"metric\") to see the rest.")


class NodeDiffTools:
    """ToolProvider (`specs()`/`execute()`) giving a role ONE tool: `diff_nodes`.

    Binds the live `RunState` each turn exactly as `RunTools` does, so the answer moves with the run:
    a node repaired between two tool calls is answered from the newer file set, and an agent cannot
    get one story from `read_experiment` and a contradicting one from here.

    Read-only by construction and not merely by policy: this provider never opens a path. There is
    no argument it accepts that names a file, so there is nothing for a `..` to escape from — the
    whole answer is assembled from state the engine already folded.
    """

    def __init__(self, max_answer: int = _MAX_ANSWER):
        self.state = None
        self.max_answer = max(1, min(int(max_answer), _MAX_ANSWER))

    # `parent` is ACCEPTED and IGNORED, exactly as `RunTools` and `MachineRunsTools` do: it is part
    # of the `bind_state` contract and a provider that implements the hook without it raises
    # TypeError at dispatch.
    def bind_state(self, state=None, parent=None) -> None:
        self.state = state

    def specs(self) -> list[dict]:
        known = ", ".join(str(n) for n in known_nodes(self.state)) or "(none yet)"
        return [fn_spec(
            "diff_nodes",
            "HOW DOES ONE NODE DIFFER FROM ANOTHER — the experiment's own source, the parameters "
            "PROPOSED, the parameters that actually RAN, and the two metrics with the "
            "comparability verdict attached.\n"
            "ASK THIS BEFORE YOU ATTRIBUTE A METRIC TO A PARAMETER. A node's recorded `params` is "
            "a PROPOSAL: under params_style=\"none\" the engine applies nothing and the Developer "
            "realises the idea by EDITING THE REPO, so the number in the record is not necessarily "
            "the number that ran. Across every run on disk the two disagree on 9% of comparisons, "
            "and one champion recorded at batch 8192 / 15 epochs actually ran 512 / 3.\n"
            f"Nodes in this run: {known}. An empty diff and an unrecoverable one are reported "
            "differently — if a section says NOT RECOVERABLE, that is not permission to assume "
            "nothing changed.",
            {"left": {"type": "integer", "description": "The node you are comparing FROM."},
             "right": {"type": "integer", "description": "The node you are comparing TO."},
             "section": {"type": "string", "enum": ["all", "code", "params", "metric"],
                         "description": "Default 'all'. Narrow it when the answer was truncated."}},
            ["left", "right"])]

    def execute(self, name: str, args: dict) -> str:
        if name != "diff_nodes":
            return f"(no tool named {name!r}; this provider offers: diff_nodes)"
        args = args if isinstance(args, dict) else {}
        known = known_nodes(self.state)
        listing = ", ".join(str(n) for n in known) or "(none yet)"
        raw_left, raw_right = args.get("left"), args.get("right")
        try:
            # `bool` is an `int` subclass, so `int(True) == 1` would silently answer about node 1.
            if isinstance(raw_left, bool) or isinstance(raw_right, bool):
                raise ValueError("bool is not a node id")
            left_id, right_id = int(raw_left), int(raw_right)
        except (TypeError, ValueError):
            return ("(name two node ids as integers: left and right. "
                    f"Nodes in this run: {listing})")
        if left_id == right_id:
            return (f"(left and right are both node {left_id} — a node does not differ from "
                    "itself. Name two different nodes.)")
        recs = {}
        for nid in (left_id, right_id):
            rec = node_record(self.state, nid)
            if rec is None:
                return (f"(no node {nid} in this run; nodes are: {listing}. "
                        "This is 'no such node', NOT 'no difference'.)")
            recs[nid] = rec
        section = str(args.get("section") or "all").strip().lower()
        if section not in ("all", "code", "params", "metric"):
            return f"(no section named {section!r}; ask for all, code, params or metric)"
        sections = ("code", "params", "metric") if section == "all" else (section,)
        return render_diff(recs[left_id], recs[right_id], sections=sections,
                           max_answer=self.max_answer)
