"""Run-introspection tools (ADR-7 tool protocol): let the Researcher / DeepResearcher read the
search's OWN experiments and data mid-loop — just-in-time retrieval instead of stuffing everything
into the prompt. Two providers expose `.specs()`/`.execute()` like the knowledge/web tools, and are
run-aware via `bind_state(state, parent)` which the agent loop calls each turn.

Every `execute` returns a STRING and soft-fails (never raises) — a junk tool call must not crash the
run. Long output is additionally truncated by the agent layer (4000 chars).
"""
from __future__ import annotations

import csv
import io
import math
from pathlib import Path
from typing import Optional

from looplab.events import digest
from looplab.core.advisory_payloads import (MAX_RESEARCH_CLAIMS, VERDICT_UNVERIFIED,
                                            memo_verification_view, verdict_tally)
from looplab.core.models import (NodeStatus, RunState, card_lineage_brief,
                                 extra_metric_channel)
from looplab.core.param_carriers import node_params_brief
from looplab.tools._base import RESULT_CAP, clip, fit_rows, fn_spec
from looplab.tools._runcache import RunStateCache

# How many groundless claims the memo render LEADS with, and how much of each it shows. Both are
# measured, not chosen: over all 98 verification blocks in `runs/`, a cap of 6 covers 79 of them
# completely (81 %), 8 would cover 94, and the unsupported statements have a median length of 178
# chars and a p90 of 273. The whole leading block is therefore bounded at roughly 1.6 kB of the
# agent layer's 4,000-char `RESULT_CAP` — which matters because the rest of this render routinely
# does not fit: replayed over all 102 memos in `runs/`, 88 of them render LONGER than the cap and
# the median render is 6,974 chars, so the loop's head-keep cut throws the tail away. On v8's own
# `at_node: 0` memo the `Claims` section begins at char 3,889 — 111 chars before the cut — so a
# verdict rendered only beside its claim would not have reached the Researcher at all. Anything the
# reader must not miss has to be ABOVE the summary, not below the findings.
_MEMO_LEAD_VERDICTS = 6
_MEMO_LEAD_STATEMENT = 140
_MEMO_LEAD_NOTE = 110

#: The sections `read_research_memo` can be asked for. `overview` is what an omitted `section`
#: means, and it is the one that had to change: over all 90 memos in `runs/` the whole-memo render
#: is a median 9,083 chars against a 4,000-char HEAD cut, and `Recommended directions` — the memo's
#: conclusion — began past that cut in 89 of the 89 memos it exists in. Sections are ADDRESSABLE
#: rather than merely reordered because reordering only moves which section is silently amputated;
#: an addressable one can be NAMED in the omission receipt as a call the caller has not yet spent
#: (`_memo_elsewhere`), which is this repo's bounded-answer rule (`tools/log_tools.py`, rule 3).
_MEMO_SECTIONS = ("overview", "directions", "findings", "claims", "summary")
_MEMO_DEFAULT_SECTION = "overview"
#: Per-section row caps, unchanged from the single-string render they came out of, so a section page
#: shows exactly the population the whole-memo answer used to promise.
_MEMO_FINDING_ROWS = 12
_MEMO_CLAIM_ROWS = 12
_MEMO_DIRECTION_ROWS = 8
#: What the OVERVIEW spends on the summary. Measured: the summary is a median 1,395 chars (p90
#: 1,620), the whole overview shares 4,000 with a verifier block of up to 1,827 and a directions
#: section of up to 3,116, and the summary is the memo's LEAST load-bearing field on both counts
#: that matter — `trust/memo_verify.py::verify_memo` never looks at it, so nothing checks it, and
#: `agents/roles.py::_state_brief` already pushes its first 300 chars into every proposal prompt
#: unasked, so a PULL that re-spends the window on it buys the least. 600 keeps roughly twice what
#: the push channel already delivered and names the call that returns the rest.
_MEMO_OVERVIEW_SUMMARY = 600
_MEMO_SUMMARY_LABEL = "the memo's summary"
#: Below this the clipped summary is not worth its own line — a 100-char head of a 1,400-char
#: summary is a sentence fragment, and the receipt naming the whole section is more use than it.
_MEMO_SUMMARY_FLOOR = 200
_MEMO_SUMMARY_CLIPPED = '…[+{n} chars — read_research_memo(section="summary")]'
#: The directions' own omission marker. `fit_rows` drops whole rows from the END and its default
#: marker says only how many; here it must also name the call that returns them, and that call gets
#: the WHOLE cap to itself — a remedy the caller has not already spent, unlike "ask again bigger".
_MEMO_DIRECTIONS_OMITTED = ('... ({receipt}{n} more direction(s) dropped to fit the result cap — '
                            'read_research_memo(section="directions") returns them all)')


def _is_number(v: str) -> bool:
    """True only if the string parses as a FINITE number. Rejects the 'nan'/'inf'/'infinity'
    sentinels (which float() happily accepts) so a column of textual missing-markers reads as
    categorical — flagging it as needing missing-value handling — instead of numeric with
    NaN/inf-poisoned (and order-dependent) min/max/mean."""
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _clip(text: str, n: int) -> str:
    """Return the LAST `n` chars of `text` (logs are read tail-first — the end is where the error and
    the final metric line live), flagging how much was dropped off the front (doc 25 TO-08)."""
    return clip(text, n, keep="tail", note="…[+{n} earlier chars truncated]\n")




class RunTools:
    """Read-only view over the live search DAG (the bound `RunState`)."""

    # Logs get a bigger error budget than read_experiment's 300-char slice — but the shared tool loop
    # HEAD-truncates every tool result to RESULT_CAP chars (agent.drive_tool_loop), so a larger budget
    # here is not just wasted, it's harmful: content past the cap (the error tail — a traceback's
    # exception line lives at the BOTTOM) would be silently cut. Stay under that cap with headroom for
    # the header + section markers, so our own tail-preserving clip is what decides what's dropped.
    _LOG_CHARS = RESULT_CAP - 400
    _MAX_LIST_ITEMS = 64
    _MAX_ANALOGOUS_ITEMS = 32
    _MAX_THEME_ITEMS = 64
    _MAX_LINE_CHARS = 700

    def __init__(self, max_chars: int = 3500):
        self.max_chars = max_chars
        self.state: Optional[RunState] = None

    # The agent loop calls this each turn so the tools see the current run. `parent` is ACCEPTED and
    # IGNORED, exactly as MachineRunsTools does: the second argument is part of the `bind_state`
    # contract (`tools/_base.py` — a provider that implements the hook without it raises TypeError at
    # dispatch), but this provider has no use for the parent. It used to be STORED on `self`, which
    # nothing ever read (doc 25 TO-10) while implying a back-reference these tools do not have.
    def bind_state(self, state: RunState, parent=None) -> None:
        self.state = state

    def specs(self) -> list[dict]:
        return [
            fn_spec("list_experiments",
                "List experiments tried so far (the search DAG). Use to see what's been done before "
                "proposing. `sort`: best|worst|recent. The optional theme filter uses the current "
                "receipt-aware concept-axis projection and explicitly qualifies incomplete results.",
                {"sort": {"type": "string", "enum": ["best", "worst", "recent"]},
                 "limit": {"type": "integer", "minimum": 1, "maximum": self._MAX_LIST_ITEMS},
                 "theme": {"type": "string", "description": "filter to one theme slug (optional)"}}),
            fn_spec("read_experiment",
                "Read one experiment's full detail: params, metric, robustness, rationale, failure "
                "reason, extra metrics, and — for a hyperparameter sweep — its trials. `trials` "
                "chooses how many sweep points to return: a number like '20' (a representative sample "
                "spanning best→worst), or 'all' for every trial. Omit for a 10-trial sample.",
                {"node_id": {"type": "integer"},
                 "trials": {"type": "string",
                            "description": "how many sweep trials to include: a number, or 'all'. "
                                           "Default: 10 representative trials (best→worst)."}},
                ["node_id"]),
            fn_spec("read_code",
                "Read the solution code of one experiment (so you can build on or avoid it).",
                {"node_id": {"type": "integer"}}, ["node_id"]),
            fn_spec("read_logs",
                "Read one experiment's EXECUTION LOGS — the captured stdout/stderr TAILS as recorded "
                "in the event log (bounded, not the raw full stream; the END — where a traceback's "
                "error and the final metric line live — is preserved). Far more than the 300-char "
                "failure summary read_experiment shows. Use to see why a node failed, or what it "
                "printed while training.",
                {"node_id": {"type": "integer"}}, ["node_id"]),
            fn_spec("find_analogous",
                "Find experiments most similar to a given one (or to a set of params) by parameter "
                "distance — to see how nearby configs performed before committing.",
                {"node_id": {"type": "integer"},
                 "params": {"type": "object", "description": "param dict to compare instead of a node"},
                 "k": {"type": "integer", "minimum": 1,
                        "maximum": self._MAX_ANALOGOUS_ITEMS}}),
            fn_spec("list_themes",
                "List current experiment concept axes with counts and best metric. Incomplete "
                "materialization is explicitly qualified; legacy themes remain bounded hints.",
                {}),
            fn_spec("read_concept_tree",
                "Read THIS run's CONCEPT hierarchy — the axis/slug concepts the experiments touch, as an "
                "indented tree with the number of experiments under each branch. Use it before proposing "
                "to see the concept vocabulary already in play, so you REUSE existing ids instead of "
                "minting near-duplicates. Richer than list_themes (which only shows coarse axes).",
                {}),
            fn_spec("concept_nodes",
                "List the experiments tagged with a concept (or any of its sub-concepts). Give an "
                "axis/slug id like 'loss/contrastive' (see read_concept_tree for the vocabulary).",
                {"concept": {"type": "string"}}, ["concept"]),
            fn_spec("node_concepts",
                "The canonical concept ids one experiment is tagged with (after consolidation).",
                {"node_id": {"type": "integer"}}, ["node_id"]),
            fn_spec("node_concept_delta",
                "How one experiment's concepts DIFFER from its parent(s): what it ADDED, REMOVED, or "
                "INHERITED. Use it to see the conceptual change a node made relative to where it came from "
                "(a merge compares with every parent; a full-mode root starts empty, while a delta-mode "
                "root compares with the recorded run base). An unavailable dependency is reported as "
                "UNAVAILABLE, never as an empty delta.",
                {"node_id": {"type": "integer"}}, ["node_id"]),
            fn_spec("read_research_memo",
                "Read the latest DEEP-RESEARCH memo, ONE SECTION at a time. The run periodically "
                "does a 'think hard' review over all results (and the web); this is how you pull it. "
                "With no argument you get the OVERVIEW: what this run's own verifier could NOT "
                "ground, then the memo's recommended directions, then a clipped summary. The memo "
                "does not fit one tool result, so the overview ends by naming what it left out and "
                "the exact call that returns it. Ask for section='findings', 'claims' (every claim "
                "with the verdict the verifier gave it — supported / unsupported / unclear, so you "
                "can tell a measured result from an unsupported assertion before you build on it), "
                "'directions' or 'summary' to read one of them in full.",
                {"section": {"type": "string",
                             "enum": list(_MEMO_SECTIONS),
                             "description": ("Which part of the memo to read. Omit for the "
                                             "overview, which names the rest.")}}),
        ]

    def execute(self, name: str, args: dict) -> str:
        st = self.state
        if st is None:
            return "(run state unavailable)"
        try:
            if name == "list_experiments":
                return self._list(st, args)
            if name == "read_experiment":
                return self._read(st, int(args.get("node_id")), args.get("trials"))
            if name == "read_code":
                return self._code(st, int(args.get("node_id")))
            if name == "read_logs":
                return self._logs(st, int(args.get("node_id")))
            if name == "find_analogous":
                return self._analogous(st, args)
            if name == "list_themes":
                return self._themes(st)
            if name == "read_concept_tree":
                return self._concept_tree(st)
            if name == "concept_nodes":
                return self._concept_nodes(st, str(args.get("concept") or ""))
            if name == "node_concepts":
                return self._node_concepts_tool(st, int(args.get("node_id")))
            if name == "node_concept_delta":
                return self._node_concept_delta_tool(st, int(args.get("node_id")))
            if name == "read_research_memo":
                return self._research_memo(st, str(args.get("section") or ""))
            return f"(unknown tool: {name})"
        # BROAD on purpose. The provider contract is that `execute` soft-fails and never raises,
        # and `drive_tool_loop` does NOT guard this call (cross_run_tools.py documents that
        # explicitly) — so anything escaping here kills the whole agent phase, not just the tool
        # call. A narrower tuple missed AttributeError and everything else the concept-projection /
        # digest helpers can raise on an unexpected shape in folded state. Same rule in
        # SiblingRunTools and AllRunsTools below, and in cross_run_tools/memory_tools already.
        except Exception as e:  # noqa: BLE001 - a tool must not be able to end the phase
            return f"(tool error: {e})"

    # --- implementations ----------------------------------------------------
    @staticmethod
    def _bounded_count(value, *, default: int, maximum: int) -> int:
        if value is None:
            return default
        if isinstance(value, bool):
            raise ValueError("count must be an integer")
        return min(max(1, int(value)), maximum)

    def _line(self, n, *, state=None, axes_by_node=None) -> str:
        if n.status is NodeStatus.failed:
            outcome = f"FAILED({n.error_reason or 'error'})"
        else:
            outcome = f"metric={digest.fmt_num(digest.node_metric(n))}"
        # The node's primary CANONICAL axis (folded node_concepts via state, else legacy theme/first authored
        # axis) — the SAME vocabulary list_themes/list_experiments advertise + filter on (node_axes), so the
        # {label} shown here matches the grouping and a concept-authored run isn't shown blank.
        current_state = self.state if state is None else state
        if current_state is not None:
            if axes_by_node is None:
                projection = self._concept_projection(current_state)
                axes_by_node = self._current_theme_axes(current_state, projection)
            axes = axes_by_node.get(n.id, ())
            # a line is a current projection too. Do not revive the frozen authored theme when
            # the folded membership receipt is unavailable, and never borrow a same-numbered node's axis
            # from whichever sibling state happened to be bound to the reusable reader last.
            node_theme = sorted(axes)[0] if axes else None
        else:
            node_theme = digest.node_theme(n)
        theme = f" {{{node_theme}}}" if node_theme else ""
        line = f"#{n.id} {n.operator} {outcome} {digest.fmt_params(n.idea.params)}{theme}"
        return (line if len(line) <= self._MAX_LINE_CHARS else
                line[:self._MAX_LINE_CHARS].rstrip() + " …(truncated)")

    def _list(self, st: RunState, args: dict) -> str:
        sort = (args.get("sort") or "best").lower()
        if sort not in {"best", "worst", "recent"}:
            raise ValueError("sort must be best, worst, or recent")
        limit = self._bounded_count(
            args.get("limit"), default=10, maximum=self._MAX_LIST_ITEMS)
        theme = args.get("theme")
        projection = self._concept_projection(st)
        axes_by_node = self._current_theme_axes(st, projection)
        # append-only audit rows are not current experiments. Use the shared lifecycle
        # boundary even for `recent`, whose raw state.nodes traversal used to resurrect tombstones and
        # aborted work that every other current concept surface had already removed.
        active_nodes = projection.active_nodes
        if sort == "recent":
            nodes = sorted(
                (node for node in st.nodes.values() if node.id in active_nodes),
                key=lambda n: n.id, reverse=True,
            )
        else:
            nodes = [node for node in digest.top_nodes(
                st, len(st.nodes), worst=(sort == "worst")) if node.id in active_nodes]
        if theme:
            # filter on the SAME receipt-aware multi-axis projection `_themes` advertises.
            # Unavailable nodes contribute no inferred authored fallback; absent legacy rows may retain a
            # compatibility hint, but the response remains explicitly non-authoritative.
            nodes = [node for node in nodes if theme in axes_by_node.get(node.id, ())]
        if not nodes:
            if theme and projection.status != "complete":
                return (f"({self._projection_note(projection)}; no retained current experiments "
                        f"match theme={theme}; this is NOT a complete zero)")
            return "(no matching experiments)"
        total = len(nodes)
        selected = nodes[:limit]
        lines = [self._line(node, state=st, axes_by_node=axes_by_node) for node in selected]
        qualifier = (f"{self._projection_note(projection)}; retained current theme matches only:\n"
                     if theme and projection.status != "complete" else "")

        # The schema cap bounds CPU/memory even when a caller bypasses tool validation. Fit the final text
        # too, and preserve a population receipt instead of relying on the agent loop's silent head cut.
        visible = list(lines)
        while visible:
            head = (f"showing {len(visible)} of {total} experiment(s), sort={sort}"
                    + (f", theme={theme}" if theme else ""))
            omitted = total - len(visible)
            suffix = (f"\n… (+{omitted} more matching experiment(s), not shown)" if omitted else "")
            rendered = qualifier + head + ":\n" + "\n".join(visible) + suffix
            if len(rendered) <= self.max_chars:
                return rendered
            visible.pop()
        return qualifier + f"(matching experiment output exceeds the {self.max_chars}-character budget)"

    def _read(self, st: RunState, nid: int, trials_arg=None) -> str:
        n = st.nodes.get(nid)
        if n is None:
            return f"(no experiment #{nid})"
        # THE COORDINATES THAT RAN, not the ones that were asked for. This line read
        # `params={n.idea.params}` — the raw PROPOSAL under an unqualified label — while
        # `events/digest.py` and `agents/roles.py::_state_brief` had both already moved to
        # `node_params_brief`. It is the surface a Researcher calls to read ONE specific past
        # experiment, which is precisely the "size the next idea one knob off this one" path, and
        # under `params_style: "none"` the Developer realises an idea by editing the repo, so the
        # proposal and the run legitimately differ. `node_params_brief` puts the applied value
        # first and the proposal in brackets beside the ones that moved, and falls back to the
        # declaration UNMARKED when no applied record exists — a pre-2026-08-20 node reads exactly
        # as it did.
        out = [f"experiment #{n.id} — operator={n.operator} status={n.status.value}",
               f"parents={n.parent_ids or '[]'}",
               f"params={node_params_brief(n)}"]
        if n.idea.space:
            out.append(f"sweep_space={n.idea.space}")
        out.append(f"metric={digest.fmt_num(n.metric)}")
        if n.confirmed_mean is not None:
            out.append(f"confirmed={digest.fmt_num(n.confirmed_mean)} "
                       f"±{digest.fmt_num(n.confirmed_std)} ({n.confirmed_seeds} seeds)")
        if n.extra_metrics:
            # Each extra metric is rendered WITH the channel it came through, because this surface
            # is read by the Researcher/Strategist when deciding what to try next and an
            # auto-captured `speculation_cuda_probe_v=1.0` reads exactly like a measured
            # `test_auc=0.92` without it. `[auto]` = the candidate printed it, nothing declared or
            # checked it; `[unknown]` = a log written before the channel was recorded (treat as
            # auto). The objective `metric=` above is unchanged and is still the only number
            # selection uses.
            _chans = getattr(n, "extra_metrics_provenance", None)
            out.append("extra_metrics=" + "{" + ", ".join(
                f"{k!r}: {v} [{extra_metric_channel(_chans, k)}]"
                for k, v in n.extra_metrics.items()) + "}")
        if n.violations:
            out.append(f"violations={n.violations}")
        if n.status is NodeStatus.failed:
            out.append(f"failure={n.error_reason}: {(n.error or '')[:300]}")
        if n.trials:
            out.append(self._sweep_view(n, trials_arg, st.direction))
        # THE RESEARCH QUESTION THIS EXPERIMENT ANSWERS, which this surface did not carry at all.
        # A reader could see what ran and never which board row it belongs to, which direction that
        # row serves, or what the other experiments under the same direction have already found —
        # so the sibling evidence was one `list_experiments` + a human join away, every time.
        # Absent card / absent link renders nothing, so a run whose proposals name no direction is
        # byte-identical here.
        lineage = card_lineage_brief(st.cards.get(n.idea.card_id or ""), st.cards)
        if lineage:
            out.append(f"research: {lineage}")
        if n.idea.rationale:
            out.append(f"rationale: {n.idea.rationale.strip()[:400]}")
        text = "\n".join(out)
        return text if len(text) <= self.max_chars else text[:self.max_chars].rstrip() + " …(truncated — ask for fewer trials)"

    @staticmethod
    def _resolve_trial_k(trials_arg, total: int) -> int:
        """How many trials to render. None -> the digest default sample; 'all'/'*'/'-1' -> every
        trial; a number -> that many (representative); anything else -> the default."""
        if trials_arg is None:
            return digest.DEFAULT_TRIAL_K
        s = str(trials_arg).strip().lower()
        if s in ("all", "*", "-1"):
            return total
        try:
            return max(1, int(float(s)))
        except (ValueError, OverflowError):   # 'inf'/'1e999' → int(float()) overflows; fall back
            return digest.DEFAULT_TRIAL_K

    def _sweep_view(self, n, trials_arg, direction: str) -> str:
        """Render a sweep node's trials as `params → metric` lines, best→worst, for the requested
        count (representative sample by default, or all). When the full finite set is shown, any
        no-metric trials are appended so 'all' is genuinely complete."""
        trials = n.trials
        finite = digest.finite_trials(trials)
        k = self._resolve_trial_k(trials_arg, len(trials))
        sel = digest.select_trials(trials, k, direction)
        best = sel[0] if sel else None
        head = f"sweep: {len(trials)} trials" + (f" over {dict(n.idea.space)}" if n.idea.space else "")
        if best:
            head += f"; best {digest.fmt_params(best.params)} metric={digest.fmt_num(best.metric)}"
        n_nometric = len(trials) - len(finite)
        if n_nometric:
            head += f" (+{n_nometric} no-metric)"
        head += (f"\nshowing {len(sel)} of {len(finite)} (best→worst):" if len(sel) < len(finite)
                 else "\ntrials (best→worst):")
        lines = [head] + [f"  {digest.trial_line(t)}" for t in sel]
        if len(sel) >= len(finite):   # complete finite set shown → list the no-metric trials too
            lines += [f"  {digest.fmt_params(t.params)} → (no metric"
                      + (f": {t.error[:60]}" if t.error else "") + ")"
                      for t in trials if t.metric is None or not math.isfinite(t.metric)]
        return "\n".join(lines)

    def _code(self, st: RunState, nid: int) -> str:
        n = st.nodes.get(nid)
        if n is None:
            return f"(no experiment #{nid})"
        if not n.code and not n.files:
            return f"(experiment #{nid} has no code recorded)"
        files = (f"\nother files: {list(n.files)}" if n.files else "")
        return f"# solution.py of experiment #{nid}\n{n.code[:self.max_chars]}{files}"

    def _logs(self, st: RunState, nid: int) -> str:
        """The node's execution logs: the captured stdout tail (what it printed while training/eval)
        and the stderr/error tail — bounded (a chain of tails: 64KB capture → event tail → this clip),
        NOT the raw full stream, but far more than the 300-char failure summary `read_experiment`
        shows. Logs are the whole point of this tool, so they get a larger budget (`_LOG_CHARS`) than
        a normal read."""
        n = st.nodes.get(nid)
        if n is None:
            return f"(no experiment #{nid})"
        head = f"experiment #{n.id} — operator={n.operator} status={n.status.value}"
        if n.error_reason:
            head += f" · failure={n.error_reason}"
        if n.eval_seconds is not None:
            head += f" · eval={digest.fmt_num(n.eval_seconds)}s"
        out = [head]
        stdout = (n.stdout_tail or "").rstrip()
        error = (n.error or "").rstrip()
        budget = max(self.max_chars, self._LOG_CHARS)
        # Split the budget so a huge stdout can't crowd out the error (and vice-versa): give each the
        # larger half only when the other is short, so a lone log still gets the whole budget.
        if stdout and error:
            half = budget // 2
            out.append("--- stdout (tail) ---\n" + _clip(stdout, max(half, budget - len(error) - 200)))
            out.append("--- error / stderr ---\n" + _clip(error, max(half, budget - len(stdout) - 200)))
        elif stdout:
            out.append("--- stdout (tail) ---\n" + _clip(stdout, budget))
        elif error:
            out.append("--- error / stderr ---\n" + _clip(error, budget))
        else:
            out.append("(no stdout or error captured for this experiment)")
        return "\n".join(out)

    def _analogous(self, st: RunState, args: dict) -> str:
        nid = args.get("node_id")
        if args.get("params"):
            target, exclude = dict(args["params"]), None
        elif nid is not None and int(nid) in st.nodes:
            exclude = int(nid)
            target = st.nodes[exclude].idea.params
        else:
            return "(give a node_id or params to compare)"
        projection = self._concept_projection(st)
        axes_by_node = self._current_theme_axes(st, projection)
        scored = []
        for n in st.nodes.values():
            if n.id == exclude or n.id not in projection.active_nodes:
                continue
            d = digest.param_distance(target, n.idea.params)
            if d != float("inf"):
                scored.append((d, n))
        scored.sort(key=lambda t: t[0])
        k = self._bounded_count(
            args.get("k"), default=3, maximum=self._MAX_ANALOGOUS_ITEMS)
        if not scored:
            return "(no comparable experiments — no shared numeric params)"
        return "nearest by param-distance:\n" + "\n".join(
            f"dist={d:.3f}  {self._line(n, state=st, axes_by_node=axes_by_node)}"
            for d, n in scored[:k])

    def _themes(self, st: RunState) -> str:
        projection = self._concept_projection(st)
        roll = self._current_theme_rollup(st, projection)
        if not roll:
            if projection.status != "complete":
                return (f"({self._projection_note(projection)}; no retained current theme assignments; "
                        "this is NOT proof that no themes are assigned)")
            return "(no themes assigned yet)"
        ordered = sorted(roll.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
        visible = [
            f"{t}: {d['count']} experiment(s)" +
            (f", best={digest.fmt_num(d['best_metric'])}" if d['best_metric'] is not None else "")
            for t, d in ordered[:self._MAX_THEME_ITEMS]]
        qualifier = (f"{self._projection_note(projection)}; showing retained current theme hints "
                     "(legacy fallback only where membership is unrecorded):\n"
                     if projection.status != "complete" else "")
        while visible:
            omitted = len(ordered) - len(visible)
            suffix = (f"\n… (+{omitted} more theme axis/axes, not shown)" if omitted else "")
            rendered = qualifier + "\n".join(visible) + suffix
            if len(rendered) <= self.max_chars:
                return rendered
            visible.pop()
        # the tool loop head-clips over-budget strings. Fail explicitly when even one
        # complete row cannot fit, rather than returning a severed axis that looks authoritative.
        return qualifier + f"(theme output exceeds the {self.max_chars}-character budget)"

    @staticmethod
    def _current_theme_axes(st: RunState, projection) -> dict[int, set[str]]:
        """Current coarse axes, retaining legacy hints only for genuinely absent folded rows."""
        axes_by_node: dict[int, set[str]] = {}
        for node_id in projection.active_nodes:
            if node_id in projection.memberships:
                axes = {
                    concept_id.split("/", 1)[0]
                    for concept_id in projection.memberships[node_id]
                    if concept_id
                }
            elif node_id in projection.absent_nodes:
                node = st.nodes.get(node_id)
                axes = digest.node_axes(st, node) if node is not None else set()
            else:
                # a receipt-unavailable membership is unknown, not a license to revive
                # frozen authored concepts as if they were the node's current classification.
                axes = set()
            if axes:
                axes_by_node[node_id] = axes
        return axes_by_node

    @classmethod
    def _current_theme_rollup(cls, st: RunState, projection) -> dict[str, dict]:
        """Receipt/lifecycle-aware equivalent of the legacy digest theme rollup."""
        better = (lambda a, b: a < b) if st.direction == "min" else (lambda a, b: a > b)
        out: dict[str, dict] = {}
        axes_by_node = cls._current_theme_axes(st, projection)
        for node_id in sorted(projection.active_nodes):
            node = st.nodes.get(node_id)
            if node is None:
                continue
            metric = digest.node_metric(node)
            for axis in sorted(axes_by_node.get(node_id, ())):
                entry = out.setdefault(axis, {"count": 0, "best_metric": None})
                entry["count"] += 1
                if (metric is not None
                        and (entry["best_metric"] is None or better(metric, entry["best_metric"]))):
                    entry["best_metric"] = metric
        return out

    @staticmethod
    def _concept_projection(st: RunState):
        from looplab.search.concept_projection import current_concept_projection
        return current_concept_projection(st)

    @staticmethod
    def _projection_note(projection) -> str:
        reasons = ",".join(projection.reasons) or "unspecified"
        return f"{projection.status.upper()} current concept projection (reasons={reasons})"

    def _canon_node_concepts(self, st: RunState) -> dict[int, list[str]]:
        """Strict canonical CURRENT memberships; unresolved and inactive rows are absent by design."""
        projection = self._concept_projection(st)
        return {node_id: list(concepts) for node_id, concepts in projection.memberships.items()}

    def _concept_tree(self, st: RunState) -> str:
        """Indented is_a concept tree with per-branch experiment counts (subtree, deduped)."""
        from collections import defaultdict
        from looplab.search.concept_lens import project_hierarchy
        projection = self._concept_projection(st)
        nc = {node_id: list(concepts) for node_id, concepts in projection.memberships.items()}
        if projection.status == "unavailable":
            return (f"({self._projection_note(projection)}; recorded fallback [] is NOT a known-empty "
                    "taxonomy)")
        if not any(nc.values()):
            if projection.status == "partial":
                return (f"({self._projection_note(projection)}; no usable concept ids remain, which is "
                        "NOT evidence of an empty taxonomy)")
            return "(no concepts tagged yet — experiments carry concepts once the Researcher tags them)"
        cids = sorted({c for ids in nc.values() for c in ids})
        tree = project_hierarchy(cids) or {}
        nodes = tree.get("nodes", {})
        # subtree experiment count: a node counts under each concept AND all its ancestor prefixes
        sub: dict[str, set] = defaultdict(set)
        for nid, ids in nc.items():
            for c in ids:
                parts = c.split("/")
                for i in range(len(parts)):
                    sub["/".join(parts[:i + 1])].add(nid)
        lines: list[str] = []
        seen: set[str] = set()

        _LINE_CAP = 400

        def walk(cid: str, depth: int) -> None:
            if cid in seen or len(lines) >= _LINE_CAP:
                return
            seen.add(cid)
            node = nodes.get(cid) or {}
            leaf = cid.rsplit("/", 1)[-1]
            mark = "" if node.get("tagged") else " ·"   # · = grouping level, no direct tag
            lines.append(f"{'  ' * depth}{leaf}  [{len(sub.get(cid, ()))}]{mark}")
            for ch in sorted(node.get("children") or []):
                walk(ch, depth + 1)
        for r in tree.get("roots", []):
            walk(r, 0)
        if len(lines) >= _LINE_CAP:                     # don't advertise a full count over a cut tree
            lines.append(f"  …(tree truncated at {_LINE_CAP} branches)")
        exps = sum(1 for v in nc.values() if v)
        head = (f"{len(cids)} concept id(s) across {exps} experiment(s)  "
                "([N] = experiments under the branch; · = grouping level, no direct tag):")
        if projection.status == "partial":
            head = self._projection_note(projection) + "; available strict subset follows\n" + head
        text = head + "\n" + "\n".join(lines)
        return text if len(text) <= self.max_chars else text[:self.max_chars].rstrip() + " …(truncated)"

    def _concept_nodes(self, st: RunState, concept: str) -> str:
        from looplab.search.concept_projection import canonical_concept_query
        projection = self._concept_projection(st)
        if projection.status == "unavailable":
            return (f"({self._projection_note(projection)}; experiment membership is UNAVAILABLE, "
                    "not empty)")
        # Canonicalize the QUERY through the SAME chain+normalizer as the stored ids, so an agent that
        # types the displayed (normalized) id — or a since-renamed id — resolves to the same target.
        target = canonical_concept_query(st, concept)
        if not target:
            return "(give an axis/slug concept id — see read_concept_tree)"
        nc = {node_id: list(concepts) for node_id, concepts in projection.memberships.items()}
        theme_axes = self._current_theme_axes(st, projection)
        hits = []
        for nid in sorted(nc):
            if any(c == target or c.startswith(target + "/") for c in nc[nid]):
                n = st.nodes.get(nid)
                hits.append(self._line(
                    n, state=st, axes_by_node=theme_axes) if n else f"#{nid}")
        if not hits:
            if projection.status == "partial":
                return (f"({self._projection_note(projection)}; no match in the available subset, "
                        "which is NOT a complete zero)")
            return f"(no experiments tagged '{target}')"
        prefix = (self._projection_note(projection) + "; available strict subset follows\n"
                  if projection.status == "partial" else "")
        visible = hits[:60]
        while visible:
            omitted = len(hits) - len(visible)
            suffix = (f"\n… (+{omitted} more experiment(s), not shown)" if omitted else "")
            rendered = (prefix + f"{len(hits)} experiment(s) under '{target}':\n"
                        + "\n".join(visible) + suffix)
            if len(rendered) <= self.max_chars:
                return rendered
            visible.pop()
        # preserve the population truth even when a single bounded experiment line is
        # wider than this caller's budget; the outer generic truncator must not fabricate a partial row.
        return prefix + f"(concept membership output exceeds the {self.max_chars}-character budget)"

    def _node_concepts_tool(self, st: RunState, nid: int) -> str:
        if not st.nodes.get(nid):
            return f"(no experiment #{nid})"
        projection = self._concept_projection(st)
        status, reasons = projection.node_status(nid)
        if status == "unavailable":
            return (f"#{nid} concepts: UNAVAILABLE (reasons={','.join(reasons)}); "
                    "this is not a known-empty classification")
        ids = projection.memberships.get(nid, ())
        if not ids:
            if status == "partial":
                return (f"#{nid} concepts: PARTIAL (reasons={','.join(reasons)}); "
                        "no reliable ids remain")
            return f"#{nid}: (no concepts tagged)"
        prefix = f"PARTIAL (reasons={','.join(reasons)}); " if status == "partial" else ""
        return f"#{nid} concepts: " + prefix + self._membership_line(st, nid, ids)

    def _membership_line(self, st: RunState, nid: int, ids) -> str:
        """One node's membership with the EXPERIMENT's own concepts LEADING and the run's said once.

        A concept carried by every experiment in the run distinguishes none of them, and reading it off
        one node's row is exactly the mistake it caused on `rubertlite-dr-unified-v9`: five of each
        node's six ids are the run's own stack, so the one tag that says what the experiment did is
        fifth in a list of six, and a reader asking "which experiments are about hard negatives" cannot
        tell node 0 — whose WHOLE membership is the run constant — from node 4. Ordering is the fix:
        nothing is withheld (every id still appears) and nothing is added.

        Strictly inert unless `run_constant_split` could make its claim over EVERY experiment, so a run
        with one unclassified node renders byte-for-byte as it did before."""
        from looplab.search.concept_lens import run_constant_split
        split = run_constant_split(st)
        constant = split["run_constant"]
        if split["coverage"] != "complete" or not constant:
            return ", ".join(ids)
        own = split["distinguishing"].get(nid) or []
        shared = (f" — plus {len(constant)} carried by all {split['population']} experiments in this "
                  f"run: " + ", ".join(constant))
        if not own:
            return ("(none of its own; every id it carries is run-wide, so the taxonomy says nothing "
                    "about what this experiment varied)" + shared)
        return ", ".join(own) + shared

    def _node_concept_delta_tool(self, st: RunState, nid: int) -> str:
        if not st.nodes.get(nid):
            return f"(no experiment #{nid})"
        from looplab.search.concept_lens import node_concept_delta
        d = node_concept_delta(st, nid)
        parents = d["parent_ids"]
        base = ("root (no parent)" if not parents
                else "parent" + ("s " if len(parents) > 1 else " ") + ", ".join(f"#{p}" for p in parents))
        if d.get("unavailable"):
            reasons = ",".join(d.get("reasons") or ["unspecified"])
            pending = "classification pending; " if d.get("untagged") else ""
            return (f"#{nid} concept delta vs {base}: UNAVAILABLE ({pending}reasons={reasons}); "
                    "no empty delta inferred")
        def _fmt(label, ids):
            return f"{label}: {', '.join(ids)}" if ids else ""
        parts = [p for p in (_fmt("+added", d["added"]), _fmt("-removed", d["removed"]),
                             _fmt("=inherited", d["inherited"])) if p]
        kin = "parent" if len(parents) <= 1 else "parents"
        if d.get("partial"):
            # a partial child can expose retained additions/inheritance, never an exact
            # removal. Name every suppressed dimension so an agent cannot read an empty list as zero.
            unknown = [str(value) for value in (d.get("unknown_dimensions") or [])]
            unknown_note = "; ".join(f"?{dimension}: unknown" for dimension in unknown)
            body = "; ".join(parts) if parts else "(no retained added/inherited concepts)"
            if unknown_note:
                body += "; " + unknown_note
            prefix = f"PARTIAL (reasons={','.join(d.get('reasons') or ['unspecified'])}); "
        else:
            body = "; ".join(parts) if parts else f"(no concepts tagged on #{nid} or its {kin})"
            prefix = ""
        return f"#{nid} concept delta vs {base}: {prefix}{body}"

    def _verifier_lead(self, view: dict, *, spell_out: bool = True) -> tuple[str, set[int]]:
        """The memo's verifier result, rendered FIRST — counts, then the claims it could not ground.

        Returns the block and the claim INDEXES it already spelled out, so the `Claims` section below
        can tag those without repeating their note verbatim: measured over the corpus, the lead costs
        ~1.4 kB and pushes 137 fully-cited claim bullets out of the 4,000-char window it has to share,
        and paying for the same sentence twice is the cheapest of those chars to give back.

        WHY IT LEADS AND WHY IT NAMES ITS OWN ABSENCE. The memo carries a per-claim verifier result
        and no role has ever seen one: this renderer keyed on `verification["summary"]`, a field no
        writer has ever produced (`core/advisory_payloads.py::memo_verification_view` records the
        census — 98 blocks, 0 summaries), so the branch was dead from the commit that added it. What
        that cost is on the record: `rubertlite-dr-unified-v8`'s `at_node: 0` memo carries
        `total_verdicts: 8, unsupported: 8`, the first of them `{"verdict": "unsupported", "note":
        "cited experiments do not exist: [9]"}` against a claim quoting `recall@100=0.8776` from a
        node id belonging to a DIFFERENT run — and that number became the run's stated anchor, rode
        into a hint and into node 9's repair rationale, while its own refusal sat unread in the same
        payload. So the refusal LEADS the claim rather than trailing it; a reader that skims, or a
        4,000-char cut that lands mid-answer, must take the refusal with the number and not after it.

        A groundless claim is still RENDERED, in full, under its verdict. Suppressing it was weighed
        and refused on the corpus: 405 of 833 verdict rows are `unsupported` and 16 of 98 blocks are
        entirely so, and 45 of the 45 verdicts the DETERMINISTIC pass emits are `unsupported` with a
        note about the CITATION (`no evidence cited`, `cited experiments do not exist`, `cited source
        URL was not consulted`) — which is a fact about the footnote and not about the claim. Hiding
        a true finding behind a bad citation is a worse renderer than the one being replaced.

        Fail-LOUD, never fail-shut. `absent` and `malformed` each get a sentence, because this whole
        defect is a missing thing being read as a passing one; but neither withholds a byte of the
        memo. This is a tool OUTPUT STRING and nothing else: it reaches no metric, no champion, no
        selectability decision and no violation, and no model's own text decides its own verdict —
        the verdicts come from `trust/memo_verify.py`, engine-side, before this is ever called
        (docs/36: a wider action space, and a wider CONTEXT, must not widen the trusted set).

        WHO THIS ACTUALLY REACHES, measured over every `spans.jsonl` in `runs/`: 400 calls in NINE
        phases — `propose` 218, `strategist_consult` 37, `hyp_prioritize` 36, `report` 34,
        `foresight_rank` 32, **`create_node` 25**, `triage` 9, `deep_research` 8, `lessons_distill` 1
        — so this is not only the proposing Researcher. The two that matter for the v8 incident are
        the last ones anybody would have guessed: the number reached "the builder's prompt and node
        9's repair rationale" (docs/BACKLOG.md §0.6), and `create_node` and `triage` are exactly those
        two surfaces. They gain CONTEXT only; `TRIAGE_ACTIONS`, the fail-closed degradations and the
        terminal's authenticated `reason` are untouched, the same line `engine/train_monitor.py::
        repair_log_tools` already holds.

        THE ONE SELF-REFERENCE WORTH CHECKING, and it is clear. `readonly_run_tools` hands a full
        `RunTools` to five auxiliary passes including the SEMANTIC MEMO VERIFIER itself
        (`trust/memo_verify.py::_verify_tools`), so a judge could in principle read verdicts while
        producing one. It cannot read its OWN: `engine/research_cadence.py` appends the memo only
        AFTER `verify_memo` returns, so `state.research[-1]` is always the PREVIOUS memo — and on the
        first memo of a run (v8's incident memo, `trigger: run_start`) it is empty. Empirically the
        question has never arisen either: of those 400 calls, ZERO come from a verification pass.
        """
        status = view.get("status")
        if status == "absent":
            return ("Verifier: NOT RUN for this memo — its claims are unchecked. Absence of a "
                    "verdict is not a pass; treat every number below as the memo's own assertion.",
                    set())
        if status == "malformed":
            return ("Verifier: result RECORDED BUT UNREADABLE for this memo (the verification block "
                    "is not the shape this run writes) — treat its claims as unchecked.", set())
        counts = view.get("counts") or {}
        rows = view.get("rows") or []
        checked = sum(counts.get(name, 0) for name in
                      ("supported", "unsupported", "unclear", "cited"))
        # ONE tally vocabulary, shared with the PUSH channel (`roles.py::_state_brief` renders the
        # same block through `advisory_payloads.memo_verdict_cue`). Byte-identical to the literal
        # this replaced; it is a shared table rather than a second copy because a role can see both
        # surfaces in one prompt and two spellings of the same counts read as two different checks.
        tally = verdict_tally(counts)
        head = (f"Verifier ({view.get('method') or 'unknown'} check; {checked} of "
                f"{counts.get('claims', 0)} claims checked): {tally or 'nothing to report'}.")
        lines = [head]
        # Omission is stated, never inferred from a short list: a bounded check that reads as a
        # complete one is the same error this whole renderer exists to stop.
        if counts.get("omitted_verdicts"):
            lines.append(f"  (verification incomplete: {counts['omitted_verdicts']} of "
                         f"{counts.get('total_verdicts', 0)} claims were never checked.)")
        if counts.get("unmatched_verdicts"):
            lines.append(f"  ({counts['unmatched_verdicts']} verdict(s) matched no claim in this "
                         "memo — the check and the claim list disagree.)")
        bad = [(i, r) for i, r in enumerate(rows)
               if r.get("verdict") in ("unsupported", VERDICT_UNVERIFIED)]
        listed: set[int] = set()
        # `spell_out=False` keeps the HEAD — the tally, and the two receipts that stop a bounded or
        # mismatched check reading as a complete one — and drops the per-claim block. Its one caller
        # is a `section` page of `_research_memo` whose own rows already carry each verdict inline,
        # or which is not about claims at all; there the block is the same sentence paid for twice
        # out of a budget the page needs for its own content. The tally still says the check ran and
        # what it found, so no page can read as "verified" when nothing was.
        if bad and spell_out:
            lines.append("  These claims are NOT grounded — the run's own verifier could not tie "
                         "them to an experiment in THIS run or to a source it read. Do not carry "
                         "their numbers forward as measured results:")
            for index, row in bad[:_MEMO_LEAD_VERDICTS]:
                listed.add(index)
                label = ("UNSUPPORTED" if row.get("verdict") == "unsupported" else "UNVERIFIED")
                note = clip(str(row.get("note") or "").strip(), _MEMO_LEAD_NOTE, note="…")
                stmt = clip(str(row.get("statement") or "").strip(), _MEMO_LEAD_STATEMENT, note="…")
                lines.append(f"  - [{label}: {note or 'no reason recorded'}] {stmt}")
            if len(bad) > _MEMO_LEAD_VERDICTS:
                # Name the CALL, not a position. The claims used to be further down the same
                # string; they are their own section now, and "below" would point at nothing.
                lines.append(f"  - (+{len(bad) - _MEMO_LEAD_VERDICTS} more not grounded; every "
                             'claim carries its verdict under '
                             'read_research_memo(section="claims").)')
        return "\n".join(lines), listed

    def _memo_claim_rows(self, view: dict, claims: list, spelled_out: set) -> list:
        """One row per claim, each carrying the verdict THIS run's verifier gave that claim.

        Lifted out of `_research_memo` verbatim when the render became section-addressable, so the
        positional join below has exactly one spelling. The population rule is the VIEW'S OWN, and
        the reason is the join: `view["rows"]` is per claim in claim order, so a claim list that
        differs from the view's by one row tags every later claim with its NEIGHBOUR's verdict.
        A truthy test is not that rule — the sanitizer preserves a whitespace-only statement
        verbatim (`redact_persisted_text(" ") == " "`), so `" "` is truthy here and blank there
        (driven: claims [" ", "A", "B"] rendered "A" under "B"'s verdict and left "B" untagged).
        """
        verdict_rows = view.get("rows") or []
        rows = []
        for index, c in enumerate(claims[:_MEMO_CLAIM_ROWS]):
            nodes = ", ".join(f"#{n}" for n in (c.get("node_ids") or []))
            urls = ", ".join(str(u) for u in (c.get("urls") or []))
            cite = "; ".join(x for x in (nodes, urls) if x)
            row = verdict_rows[index] if index < len(verdict_rows) else {}
            verdict = str(row.get("verdict") or "").strip()
            # No verdict block at all leaves the claim untagged rather than tagged "unverified":
            # the lead already said the check never ran, and repeating it on twelve rows spends
            # the answer's remaining budget saying one thing many times.
            tag = f"[{verdict.upper()}] " if verdict and view.get("status") == "present" else ""
            # The lead already printed this claim's reason verbatim; repeat the LABEL so the
            # row is self-describing, never the sentence.
            note = "" if index in spelled_out else str(row.get("note") or "").strip()
            reason = f"  [verifier: {clip(note, _MEMO_LEAD_NOTE, note='…')}]" if note else ""
            rows.append(f"  - {tag}{str(c['statement']).strip()}"
                        + (f"  [evidence: {cite}]" if cite else "") + reason)
        return rows

    @staticmethod
    def _memo_elsewhere(missing: list) -> str:
        """The omission receipt: what this answer does NOT carry, and the call that carries it.

        Rule 3 of the bounded-answer contract this repo already applies to `log_tools.py`: a bounded
        answer states what it did not cover AND names a call that covers it — and that call must be
        one the caller has not already spent. Each `section` page below gets the WHOLE `RESULT_CAP`
        to itself, so naming one is a real remedy and not the "raise max_bytes" told to a caller
        already at the ceiling.
        """
        if not missing:
            return ""
        parts = [f'{label} — read_research_memo(section="{name}")' for name, label in missing]
        return "NOT IN THIS ANSWER: " + "; ".join(parts) + "."

    def _research_memo(self, st: RunState, section: str = "") -> str:
        """Signal-delivery (§1): the latest deep-research memo, on demand, ONE SECTION AT A TIME.

        WHY THIS IS SECTIONED AND NOT ONE STRING, measured over all 90 memos in `runs/` on
        2026-08-19. The whole-memo render is a median 9,083 chars against the agent layer's
        4,000-char `RESULT_CAP`, which is HEAD-keep (`agents/tool_loop.py::_cap_tool_result`), so
        89 of the 90 were cut and a median 5,180 chars never reached the model. What the cut removed
        was not the padding: `Summary` and `Findings` survived every time, `Claims` began past the
        cut in 80 of 89 and **`Recommended directions` in 89 of 89** — i.e. the section a caller
        ACTS ON was the one section that never arrived, on every memo in the corpus. Confirmed
        against the real traces: of 375 recorded `read_research_memo` calls, 362 rendered over the
        cap, and of the 212 whose recorded render still shows a directions section, 194 have it
        starting past the cut. The run pays for a think-hard review and then discards its
        conclusions in the last 30 characters of the delivery path.

        RAISING THE CAP IS NOT THE FIX and was not done. The cap is the tool loop's bounded-output
        contract, shared by every provider, and a memo that fits is still the "портянка" (wall of
        text) the operator complained about — a bigger blob is a blob. What changed is WHAT is kept:
        the answer is now ordered by what a reader must act on, bounded by this tool rather than by
        a blind head cut, and everything it left out is NAMED beside the call that returns it
        (`_memo_elsewhere`). Each section page gets the whole cap to itself.

        THE ORDER, and why the overview holds these three. The verifier block LEADS — `_verifier_lead`
        records what its absence cost on `rubertlite-dr-unified-v8` and why a refusal must arrive with
        the number rather than after it. `Recommended directions` comes next because it is the memo's
        conclusion and the only section a caller can act on without reading the rest. `Summary` is
        third and is CLIPPED: it is the one field of the memo nothing checks
        (`trust/memo_verify.py::verify_memo` verifies `memo["claims"]` and has never looked at
        `summary`), and `agents/roles.py::_state_brief` already pushes its first 300 chars into every
        proposal prompt unasked — so a PULL that re-spends the window on it buys the least of the
        five. `Findings` and `Claims` are whole sections of their own, named in the receipt —
        and `section="claims"` is where every claim arrives carrying the per-claim VERDICT this
        run's verifier gave it, its population and its positional join unchanged
        (`_memo_claim_rows`).

        Soft-fails to a plain note; returns a string and decides nothing (docs/36)."""
        research = getattr(st, "research", None) or []
        if not research:
            return "(no deep-research memo yet — the run hasn't done a 'think hard' review)"
        m = research[-1]
        if not isinstance(m, dict):
            return "(research memo unavailable)"
        want = str(section or _MEMO_DEFAULT_SECTION).strip().lower() or _MEMO_DEFAULT_SECTION
        if want not in _MEMO_SECTIONS:
            # A junk section is REFUSED by name rather than silently served the overview: a caller
            # that asked for claims and got the overview would read "no claims" off an answer that
            # was never about claims.
            return (f"(unknown memo section {str(section)[:40]!r} — ask for one of: "
                    + ", ".join(_MEMO_SECTIONS) + ")")
        # ONE derivation of the verdicts, from the module that WRITES them, used by both the lead
        # block and the per-claim tags — so the two halves of this answer cannot come to disagree
        # about the same claim.
        view = memo_verification_view(m)
        at = m.get("at_node")
        head = (f"Deep-research memo (at node {at}), section '{want}':" if at is not None
                else f"Deep-research memo, section '{want}':")
        summary = str(m.get("summary") or "").strip()
        findings = [str(f).strip() for f in (m.get("findings") or []) if str(f).strip()]
        dirs = [str(d).strip() for d in (m.get("recommended_directions") or []) if str(d).strip()]
        # THE VIEW'S OWN population rule, spelled the same way — see `_memo_claim_rows`.
        claims = [c for c in (m.get("claims") or [])[:MAX_RESEARCH_CLAIMS]
                  if isinstance(c, dict) and str(c.get("statement") or "").strip()]

        # The spelled-out refusals ride the OVERVIEW only. Everywhere else the page's own rows carry
        # their verdicts inline (`claims`) or are not claims at all (`findings`/`directions`/
        # `summary`), so the block would be the same sentence paid for twice — the argument
        # `_verifier_lead`'s own `spelled_out` return value already makes one level down.
        lead, spelled_out = self._verifier_lead(view, spell_out=(want == _MEMO_DEFAULT_SECTION))

        if want == "claims":
            rows = self._memo_claim_rows(view, claims, spelled_out)
            return fit_rows([head, lead, "Claims (with evidence and verifier verdict):"],
                            rows or ["  (this memo makes no evidence-cited claims)"],
                            cap=RESULT_CAP)
        if want == "findings":
            return fit_rows([head, lead, "Findings:"],
                            [f"  - {f}" for f in findings[:_MEMO_FINDING_ROWS]]
                            or ["  (this memo records no findings)"], cap=RESULT_CAP)
        if want == "directions":
            return fit_rows([head, lead, "Recommended directions:"],
                            [f"  - {d}" for d in dirs[:_MEMO_DIRECTION_ROWS]]
                            or ["  (this memo recommends no directions)"], cap=RESULT_CAP)
        if want == "summary":
            return fit_rows([head, lead, "Summary:"],
                            [summary] if summary else ["  (this memo carries no summary)"],
                            cap=RESULT_CAP)

        # --- the overview -------------------------------------------------------------------
        # Assembled against a BUDGET rather than concatenated and hoped for, and the ORDER of the
        # reservations is the priority order: the refusal block, then the directions IN FULL, and
        # the summary gets only what is left. Reserve the receipt at its LONGEST — as if the summary
        # will also have to be named — before the directions are laid out, because a receipt sized
        # after the fit decision is exactly what pushes it past the cap (`_base.fit_rows` makes the
        # same reservation one layer down for the same reason).
        missing = [(name, label) for name, label, rows in
                   (("findings", f"{len(findings)} finding(s)", findings),
                    ("claims", f"{len(claims)} claim(s) with their verdicts", claims)) if rows]
        reserve = len(self._memo_elsewhere(missing + [("summary", _MEMO_SUMMARY_LABEL)])) + 1
        # ONE named budget, derived from the loop cap — `tests/test_bounded_tool_results.py` holds
        # every provider to deriving its bounds from `RESULT_CAP` rather than from a free-standing
        # ~4000, and a derivation buried in a call argument is the shape that guard cannot see.
        overview_budget = RESULT_CAP - reserve
        rows = [f"  - {d}" for d in dirs[:_MEMO_DIRECTION_ROWS]]
        body = fit_rows([head, lead] + (["Recommended directions:"] if rows else []), rows,
                        cap=max(0, overview_budget), omitted=_MEMO_DIRECTIONS_OMITTED)
        # What the directions did not spend goes to the summary, never the other way round: it is the
        # memo's least load-bearing field (see `_MEMO_OVERVIEW_SUMMARY`) and the section a caller can
        # act on must not lose a row to it. A summary that is cut carries its own remedy through
        # `clip`; one that does not fit at all is named in the receipt, so it is never silently gone.
        room = overview_budget - len(body) - len("\nSummary: ")
        keep = min(len(summary), _MEMO_OVERVIEW_SUMMARY, max(0, room))
        # The floor gates a CLIPPED summary only. Gating a WHOLE one on it too dropped every short
        # summary out of the overview — caught by `tests/test_signal_delivery.py`, whose probe reads
        # exactly that field, which is the point of having a delivery probe per signal.
        if summary and (keep >= len(summary) or keep >= _MEMO_SUMMARY_FLOOR):
            body += "\nSummary: " + clip(summary, keep, note=_MEMO_SUMMARY_CLIPPED, reserve=64)
        elif summary:
            missing.append(("summary", _MEMO_SUMMARY_LABEL))
        receipt = self._memo_elsewhere(missing)
        return body + (f"\n{receipt}" if receipt else "")


class ForeignRunReader:
    """The plumbing every provider that reads ANOTHER run shares (doc 25 TO-05).

    `SiblingRunTools`, `AllRunsTools` and `MachineRunsTools` each held the same composition (a
    traversal-guarded, (size, mtime)-fingerprinted `RunStateCache` plus an inner `RunTools` bound
    per read) and each spelled out the same delegation: resolve the run, refuse a miss, bind the
    reader, fetch the source note, prefix it onto the inner tool's answer. The listing renderers
    triplicated the PARTIAL-SOURCE receipt too, verbatim in two of them.

    What is NOT here is what genuinely differs: each provider keeps its own `specs()`/`execute()`,
    its own listing shape, and its own scope predicate (`_scope_denial`). Only the plumbing merges —
    `SiblingRunTools`'s fail-closed same-task boundary is a policy decision and stays a policy hook.
    """

    def __init__(self, run_root, *, max_chars: int = 3500):
        self.run_root = Path(run_root)
        self.max_chars = max_chars
        self._runs = RunStateCache(self.run_root)
        self._reader = RunTools(max_chars=max_chars)
        # run_id -> EvalContract|None. `task.snapshot.json` is written once at setup and never
        # rewritten, so one read per run per provider is the whole cost. `None` is a CACHED answer
        # ("unknown"), not a cache miss — re-reading a missing snapshot on every listing row would
        # pay the miss 46 times per `list_all_runs` on this box.
        self._contracts: dict[str, object] = {}
        self._self_contract_cached: tuple = ()

    def _state(self, run_id: Optional[str]) -> Optional[RunState]:
        return self._runs.state(run_id)

    # --- evaluation-contract receipt -----------------------------------------
    #
    # WHY THIS SITS IN THE SHARED PLUMBING and not in one provider: all three foreign-run readers
    # emit another run's METRIC, and the number that reached `rubertlite-dr-unified-v8`'s Researcher
    # came through two of them. Measured over v8's `spans.jsonl`, the tool calls whose span carries
    # `0.8776` are `read_run_experiment` 50, `read_research_memo` 36, `read_run_code` 26,
    # `list_all_runs` 20, `read_sibling_experiment` 12 — against `cross_run_search` 2. The cross-run
    # MEMORY store is a secondary carrier here; these tools are the primary one.
    #
    # WHAT IT DOES AND DOES NOT DO. It appends a deterministic sentence beside a foreign run's number
    # saying that run measured it with a different harness. It does NOT withhold the number, does not
    # rank, does not re-label anything, and touches no `RunState`: every caller below is a tool
    # OUTPUT STRING, so nothing here can reach a metric, a champion, a selectability decision or a
    # violation (docs/36) and a replay is unaffected by construction.
    #
    # WHY NOT WITHHOLD, which was the stronger option and was rejected on measurement rather than on
    # taste. The value would have to come out of text `RunTools` formats at eight separate sites, and
    # that text also carries the PARAMS — `rubert-dr-0807` node 9's row holds `loss_temperature 0.05`,
    # `lr 0.001` and `pct_start 0.2` beside its metric — so no rule over the rendered string can drop
    # the one without the others. `RunTools` is also the reader for a run's OWN nodes, where
    # withholding would be a straightforward bug. Threading a per-read flag through all eight is the
    # right shape and is the backlog entry; it is not a change to make in the same hour a run launches.
    # COST, measured 2026-08-16 on the real 59-directory `runs/` corpus (geesefs mount): one full
    # pass of `contract_for_run_dir` over every directory is 675 ms, of which the 25 MISSING snapshots
    # are 382 ms (~15 ms each) — a missing-file open on this mount is the expensive case, which is why
    # `None` is CACHED as an answer rather than retried per row. Against the tool that pays it,
    # `list_all_runs` costs ~2,500 ms warm on that corpus (dominated by one fold per run) and this
    # adds **+23 ms**, i.e. 0.9 %. If a future caller pays it somewhere a fold is not already
    # happening, re-measure before assuming it is free.
    def _contract_for(self, run_id: str):
        from looplab.engine.eval_contract import contract_for_run_dir
        key = str(run_id)
        if key not in self._contracts:
            run_dir = self._runs.safe_dir(key)
            self._contracts[key] = (contract_for_run_dir(run_dir)
                                    if run_dir is not None else None)
        return self._contracts[key]

    def _self_contract(self):
        """This provider's OWN run's contract, or `None` when it is not bound to one.

        `MachineRunsTools` never binds a self run (its `bind_state` is a documented no-op for the
        operator's portfolio assistant), so it gets `None` here and every notice below stays empty —
        the operator's own cross-task reads are unchanged, byte for byte. That is deliberate: this
        boundary is about what a RUN treats as its target, not about what a human may look at.
        """
        run_id = str(getattr(self, "self_run_id", "") or "")
        if self._self_contract_cached[:1] != (run_id,):
            self._self_contract_cached = (run_id, self._contract_for(run_id) if run_id else None)
        return self._self_contract_cached[1]

    def _contract_notice(self, run_id: str) -> str:
        """The full sentence, for a per-read receipt. Empty unless provably a different contract."""
        from looplab.engine.eval_contract import contract_notice
        try:
            return contract_notice(self._self_contract(), self._contract_for(run_id),
                                   other_run_id=str(run_id))
        except Exception:  # noqa: BLE001 - never-raise contract; an unreadable contract is UNKNOWN
            return ""

    def _contract_suffix(self, run_id: str) -> str:
        """The listing-row form, beside `_partial_suffix` — the same shape for the same reason.

        Short on purpose: a listing prints one row per run and the full sentence would swamp it. The
        row says THAT the boundary exists; `read_*_experiment` on that run states which facet differs.
        """
        return " · DIFFERENT EVAL CONTRACT (not this run's scale)" if self._contract_notice(
            run_id) else ""

    def _scope_denial(self, run_id: str, st: RunState) -> str:
        """Non-empty = refuse this read, with the reason. Default: no scope filter at all.

        A DIRECT read takes a model-supplied run_id, so the id itself is the authorization boundary —
        never evidence that the listing tool was called first. A provider whose listing is scoped
        must re-check that scope here, or a guessed id walks straight past it.
        """
        return ""

    def _partial_suffix(self, run_id: str) -> str:
        """The listing-row receipt. A truncated log folds into a state that LOOKS complete, so `best`
        and the node count would silently describe a PREFIX of the run."""
        return (" · PARTIAL SOURCE (read incomplete; later results unknown)"
                if self._runs.partial(run_id) else "")

    def _delegate(self, run_id, tool: str, args: dict, *, prefix: str = "",
                  missing: str = "run") -> str:
        """Bind the inner reader to `run_id`'s state and forward ONE `RunTools` tool.

        Every per-node read carries the same source receipt the listing puts on its rows: without it
        a node read from a truncated foreign log looks authoritative, which is exactly how a prefix
        read becomes a "no later run beat this" absence claim.
        """
        st = self._state(run_id)
        if st is None:
            return f"(no such {missing}: {run_id!r})"
        denial = self._scope_denial(str(run_id), st)
        if denial:
            return denial
        self._reader.bind_state(st, None)
        note = self._runs.source_note(run_id)
        # The contract receipt rides BESIDE the source receipt, at the head, for the same reason that
        # one does: a number read from a foreign run looks authoritative on its own, and by the time
        # the reader reaches the metric line the qualification has to already have been said.
        contract = self._contract_notice(str(run_id))
        head = "\n".join(part for part in (note, contract) if part)
        return (f"{head}\n" if head else "") + prefix + self._reader.execute(tool, args)


class SiblingRunTools(ForeignRunReader):
    """Read-only view over SIBLING runs — other runs of the SAME task under the same run-root — so a
    run can build on what neighbouring runs already learned instead of rediscovering it. Same
    `.specs()`/`.execute()`/`bind_state()` shape as RunTools; every `execute` returns a string and
    soft-fails (a junk tool call must never crash the run).

    Sibling `RunState`s are folded from disk on demand and cached by each event log's (size, mtime)
    fingerprint, so repeated turns don't re-fold unchanged runs. Reading one sibling's experiment/code
    delegates to an internal `RunTools` bound to that sibling — the same reader the in-run agent uses."""

    def __init__(self, run_root, self_run_id: str = "", max_chars: int = 3500):
        super().__init__(run_root, max_chars=max_chars)
        self.self_run_id = self_run_id
        self.task_id = ""

    def _scope_denial(self, run_id: str, st: RunState) -> str:
        """The same-task boundary, fail-CLOSED. Discovery is same-task scoped, but a DIRECT read takes
        a model-supplied run_id, so a caller that guesses one must not read ANOTHER task through a
        same-task tool. `not self.task_id` refuses for the same reason `_sibling_ids` does: with no
        authoritative task id there is no boundary to check against, and `x and ...` skipped the guard
        entirely in exactly that state. Cross-task reads are the deliberately-scoped MachineRunsTools.
        """
        if not self.task_id or getattr(st, "task_id", "") != self.task_id:
            return f"(run {run_id!r} is not a sibling of task {self.task_id!r})"
        return ""

    # The agent loop calls this each turn; we use it to learn our OWN run_id + task_id from the live
    # state (so we never list ourselves, and only surface same-task siblings) without extra wiring.
    def bind_state(self, state: Optional[RunState] = None, parent=None) -> None:
        if state is not None:
            if getattr(state, "run_id", ""):
                self.self_run_id = state.run_id
            if getattr(state, "task_id", ""):
                self.task_id = state.task_id

    def specs(self) -> list[dict]:
        return [
            fn_spec("list_sibling_runs",
                "List OTHER runs of the same task (siblings) with their best metric, node count and "
                "phase — so you can see what neighbouring runs achieved before proposing.", {}),
            fn_spec("read_sibling_experiment",
                "Read one experiment of a SIBLING run in full detail (params, metric, rationale, "
                "failure, sweep trials). Use a run_id from list_sibling_runs.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"},
                 "trials": {"type": "string", "description": "how many sweep trials: a number, or 'all'"}},
                ["run_id", "node_id"]),
            fn_spec("read_sibling_code",
                "Read the solution code of one experiment of a SIBLING run (to reproduce or build on "
                "it — pair with an `import` action to seed it into this run).",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"}},
                ["run_id", "node_id"]),
            fn_spec("find_analogous_across_runs",
                "Find experiments ACROSS sibling runs most similar to a set of params, by parameter "
                "distance — to see how a nearby config performed elsewhere.",
                {"params": {"type": "object", "description": "param dict to compare against"},
                 "k": {"type": "integer", "minimum": 1,
                        "maximum": RunTools._MAX_ANALOGOUS_ITEMS}}, ["params"]),
        ]

    def execute(self, name: str, args: dict) -> str:
        try:
            if name == "list_sibling_runs":
                return self._list_runs()
            if name == "read_sibling_experiment":
                return self._read(args.get("run_id"), int(args.get("node_id")), args.get("trials"))
            if name == "read_sibling_code":
                return self._code(args.get("run_id"), int(args.get("node_id")))
            if name == "find_analogous_across_runs":
                return self._analogous(args)
            return f"(unknown tool: {name})"
        except Exception as e:  # noqa: BLE001 - never-raise contract; see RunTools.execute
            return f"(tool error: {e})"

    # --- internals -----------------------------------------------------------
    def _state(self, run_id: Optional[str]) -> Optional[RunState]:
        return self._runs.state(run_id)

    def _sibling_ids(self) -> list[str]:
        """Run ids under run_root, excluding self, restricted to our task_id.

        FAIL CLOSED without one: absence of an authoritative task id is UNKNOWN scope, not permission
        to widen. `self.task_id` starts empty and `bind_state` only fills it from a truthy
        `state.task_id`, so a missing/failed bind or a legacy log with no `task_id` used to skip the
        filter entirely and list EVERY task — silently turning the default same-task tool into
        AllRunsTools (the deliberately-scoped cross-task reader) for an agent that never asked for it.
        """
        if not self.task_id:
            return []
        cand = self._runs.run_ids()
        out = []
        for rid in cand:
            if rid == self.self_run_id:
                continue
            st = self._state(rid)
            if st is None or st.task_id != self.task_id:
                continue
            out.append(rid)
        return out

    def _list_runs(self) -> str:
        ids = self._sibling_ids()
        if not ids:
            return "(no sibling runs of this task)"
        lines = []
        for rid in ids:
            st = self._state(rid)
            if st is None:
                continue
            best = st.best()
            phase = "finished" if st.finished else "running"
            lines.append(f"{rid}: best={digest.fmt_num(best.metric) if best else '—'} "
                         f"({st.direction}) · {len(st.nodes)} nodes · {phase}"
                         + (f" · best=#{best.id}" if best else "")
                         + self._partial_suffix(rid) + self._contract_suffix(rid))
        head = f"{len(lines)} sibling run(s) of task {self.task_id or '?'}:"
        return head + "\n" + "\n".join(lines) if lines else "(no sibling runs of this task)"

    def _read(self, run_id, nid: int, trials_arg=None) -> str:
        return self._delegate(run_id, "read_experiment", {"node_id": nid, "trials": trials_arg},
                              prefix=f"run {run_id} · ", missing="sibling run")

    def _code(self, run_id, nid: int) -> str:
        return self._delegate(run_id, "read_code", {"node_id": nid},
                              prefix=f"# from run {run_id}\n", missing="sibling run")

    def _analogous(self, args: dict) -> str:
        target = args.get("params")
        if not isinstance(target, dict) or not target:
            return "(give a params dict to compare against)"
        scored = []
        views = {}
        for rid in self._sibling_ids():
            st = self._state(rid)
            if st is None:
                continue
            projection = self._reader._concept_projection(st)
            axes_by_node = self._reader._current_theme_axes(st, projection)
            views[rid] = (st, axes_by_node)
            for n in st.nodes.values():
                if n.id not in projection.active_nodes:
                    continue
                d = digest.param_distance(target, n.idea.params)
                if d != float("inf"):
                    scored.append((d, rid, n))
        scored.sort(key=lambda t: t[0])
        k = self._reader._bounded_count(
            args.get("k"), default=5, maximum=RunTools._MAX_ANALOGOUS_ITEMS)
        if not scored:
            return "(no comparable experiments across siblings — no shared numeric params)"
        return "nearest across sibling runs (by param-distance):\n" + "\n".join(
            f"dist={d:.3f}  run {rid} "
            f"{self._reader._line(n, state=views[rid][0], axes_by_node=views[rid][1])}"
            # `find_analogous_across_runs` ranks by PARAM distance and prints each row's
            # metric, so a nearby config from another harness reads as "how a nearby config
            # performed" on this run's scale. Same receipt, same rule.
            f"{self._contract_suffix(rid)}"
            for d, rid, n in scored[:k])


class AllRunsTools(ForeignRunReader):
    """Read-only view over every run under ONE configured run root — ACROSS ALL TASKS, not just
    same-task siblings — so an enabled reasoning role can read the code + result of a past experiment
    in that workspace when it wants to reuse or learn from an approach. Where `SiblingRunTools`
    restricts to the current task,
    this deliberately does NOT filter by task: it just gives the agent the capability, and the agent
    decides when a foreign run is relevant. Same `.specs()`/`.execute()`/`bind_state()` shape as the
    other providers; every `execute` returns a string and soft-fails (a junk call must never crash the
    loop). Runs are folded on demand and cached by each event log's (size, mtime) fingerprint (shared
    RunStateCache), and reading one run's experiment/code delegates to an internal `RunTools` bound to
    it — the SAME reader the in-run agent uses, so the output format is identical."""

    def __init__(self, run_root, self_run_id: str = "", max_chars: int = 3500):
        super().__init__(run_root, max_chars=max_chars)
        self.self_run_id = self_run_id

    def bind_state(self, state: Optional[RunState] = None, parent=None) -> None:
        # Learn our OWN run_id so we never list/read ourselves (own experiments already come via RunTools).
        if state is not None and getattr(state, "run_id", ""):
            self.self_run_id = state.run_id

    def specs(self) -> list[dict]:
        return [
            fn_spec("list_all_runs",
                "List every run under this configured run root, ACROSS ALL TASKS (not just same-task "
                "siblings), with its task, best metric, node count and phase — so you can find a run "
                "whose code you want to read or reuse. Broader than list_sibling_runs, but not "
                "machine-wide.", {}),
            fn_spec("read_run_code",
                "Read the solution code (solution + files) of ONE node in any run returned by "
                "list_all_runs — to reuse or learn from how it was implemented. Pair with "
                "read_run_experiment to check that node's result first. Scope is this run root, not "
                "the whole machine.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"}},
                ["run_id", "node_id"]),
            fn_spec("read_run_experiment",
                "Read ONE node of ANY run in detail: params, metric, rationale/idea, failure, sweep "
                "trials — so you can judge whether its approach is worth reading the code for.",
                {"run_id": {"type": "string"}, "node_id": {"type": "integer"},
                 "trials": {"type": "string", "description": "how many sweep trials: a number, or 'all'"}},
                ["run_id", "node_id"]),
        ]

    def execute(self, name: str, args: dict) -> str:
        try:
            if name == "list_all_runs":
                return self._list_runs()
            if name == "read_run_code":
                return self._code(args.get("run_id"), int(args.get("node_id")))
            if name == "read_run_experiment":
                return self._read(args.get("run_id"), int(args.get("node_id")), args.get("trials"))
            return f"(unknown tool: {name})"
        except Exception as e:  # noqa: BLE001 - never-raise contract; see RunTools.execute
            return f"(tool error: {e})"

    # --- internals -----------------------------------------------------------
    def _all_ids(self) -> list[str]:
        """Every run id under run_root EXCEPT self (own experiments already reachable via RunTools)."""
        return [rid for rid in self._runs.run_ids() if rid != self.self_run_id]

    def _list_runs(self) -> str:
        lines = []
        for rid in self._all_ids():
            st = self._state(rid)
            if st is None:
                continue
            best = st.best()
            phase = "finished" if st.finished else "running"
            lines.append(f"{rid} [{st.task_id or '?'}]: best={digest.fmt_num(best.metric) if best else '—'} "
                         f"({st.direction}) · {len(st.nodes)} nodes · {phase}"
                         + (f" · best=#{best.id}" if best else "")
                         + self._partial_suffix(rid) + self._contract_suffix(rid))
        return (f"{len(lines)} run(s) under this configured run root (across all tasks):\n"
                + "\n".join(lines)
                ) if lines else "(no other runs under this configured run root)"

    # No `_scope_denial` override: this provider deliberately does NOT filter by task — it gives the
    # agent the capability and lets the agent decide when a foreign run is relevant.
    def _code(self, run_id, nid: int) -> str:
        return self._delegate(run_id, "read_code", {"node_id": nid},
                              prefix=f"# from run {run_id}\n")

    def _read(self, run_id, nid: int, trials_arg=None) -> str:
        return self._delegate(run_id, "read_experiment", {"node_id": nid, "trials": trials_arg},
                              prefix=f"run {run_id} · ")


class DataTools:
    """Read the concrete task data — schema, column profiling, and asset samples — so the Researcher
    proposes from the REAL data rather than guessing. Degrades gracefully for tasks with no dataset
    (e.g. toy/repo tasks). Uses the documented TaskAdapter surface (`columns`/`assets`), plus the
    optional `data_samples()` hook as a fallback for tasks that read their data by absolute path and
    expose `assets()=={}` (the `dataset` kind) — so their on-disk data is still visible here."""

    def __init__(self, task, max_chars: int = 3500):
        self.task = task
        self.max_chars = max_chars
        self.state: Optional[RunState] = None

    def bind_state(self, state: RunState, parent=None) -> None:
        self.state = state

    def specs(self) -> list[dict]:
        return [
            fn_spec("data_schema", "Show the task's data schema — column names, types, and a couple of "
                "sample values — so you propose from the real fields.", {}),
            fn_spec("data_profile", "Per-column statistics of the task data — missing fraction, numeric "
                "min/max/mean, and categorical cardinality (derived from the training table).", {}),
            fn_spec("read_asset", "Read a sample of a task data asset (e.g. train/test). Omit `name` to "
                "list available assets.", {"name": {"type": "string"}}),
        ]

    def execute(self, name: str, args: dict) -> str:
        try:
            if name == "data_schema":
                return self._schema()
            if name == "data_profile":
                return self._profile()
            if name == "read_asset":
                return self._asset(args.get("name"))
            return f"(unknown tool: {name})"
        except Exception as e:  # noqa: BLE001 — data reads are best-effort
            return f"(tool error: {e})"

    def _columns(self) -> Optional[dict]:
        fn = getattr(self.task, "columns", None)
        return fn() if callable(fn) else None

    def _assets(self) -> dict:
        fn = getattr(self.task, "assets", None)
        assets = (fn() if callable(fn) else {}) or {}
        if assets:
            return assets
        # Fallback for tasks that read their data by absolute path and expose assets()=={} (the
        # `dataset` kind): preview the on-disk data as bounded head samples so read_asset /
        # data_schema / data_profile aren't blind. Read-only — NOT materialized into the sandbox.
        sampler = getattr(self.task, "data_samples", None)
        if callable(sampler):
            try:
                return sampler() or {}
            except Exception:  # noqa: BLE001 — previews are best-effort
                return {}
        return {}

    _PROFILE_ROWS = 5000          # cap on retained rows (bounds the parse + the sample size)
    _MAX_TABLE_CHARS = 4_000_000  # cap on the text actually parsed, so neither the StringIO copy
                                  # nor the parse scales with a multi-hundred-MB table

    def _primary_table(self):
        """Pick the most representative training table among the CSV/TSV assets (prefer
        ``train*``, else the first one) and parse a bounded prefix into at most ``_PROFILE_ROWS``
        rows — a ``.tsv`` table is split on tabs. Returns ``(name, header, rows)`` or ``None`` when
        no parseable CSV/TSV asset exists — so schema/profile can derive a real view from the actual
        data even when the task declares no structured ``columns()``. Only the first
        ``_MAX_TABLE_CHARS`` are wrapped/parsed, so a huge file isn't copied whole here. (The task's
        ``assets()`` still materializes each file once upstream — that read is outside this read-only
        tool's control.)"""
        tables = {n: v for n, v in self._assets().items()
                  if isinstance(v, str) and n.lower().endswith((".csv", ".tsv"))}
        if not tables:
            return None
        name = next((n for n in tables if n.lower().startswith("train")), None) or sorted(tables)[0]
        delim = "\t" if name.lower().endswith(".tsv") else ","
        try:
            reader = csv.reader(io.StringIO(tables[name][:self._MAX_TABLE_CHARS]), delimiter=delim)
            header = next(reader, None)
            if not header:
                return None
            rows = []
            for i, r in enumerate(reader):
                if i >= self._PROFILE_ROWS:
                    break
                rows.append(r)
            return name, header, rows
        except (csv.Error, ValueError):
            return None

    def _schema(self) -> str:
        cols = self._columns()
        if cols:
            lines = [f"{len(cols)} column(s):"]
            for name, vals in list(cols.items())[:40]:
                sample = [v for v in (vals[:3] if isinstance(vals, list) else [])]
                dtype = "numeric" if sample and all(isinstance(v, (int, float)) for v in sample) else "categorical"
                lines.append(f"  {name} ({dtype}) e.g. {sample}")
            return "\n".join(lines)[:self.max_chars]
        # Fallback: derive the schema from the training table itself (CSV header + sampled values),
        # so a task that exposes no explicit columns() (e.g. mlebench_real) still gets a real schema.
        tbl = self._primary_table()
        if not tbl:
            return "(this task exposes no structured schema — try read_asset or data_profile)"
        name, header, rows = tbl
        lines = [f"schema inferred from {name} ({len(header)} columns, {len(rows)} rows sampled):"]
        for ci, col in enumerate(header[:60]):
            samples = [r[ci] for r in rows[:50] if ci < len(r) and r[ci] != ""]
            dtype = "numeric" if samples and all(_is_number(v) for v in samples) else "categorical"
            lines.append(f"  {col} ({dtype}) e.g. {samples[:3]}")
        return "\n".join(lines)[:self.max_chars]

    def _profile(self) -> str:
        prof = getattr(self.state, "data_profile", None) if self.state else None
        if prof:
            lines = ["column profile:"]
            for name, p in list(prof.items())[:40]:
                if not isinstance(p, dict):
                    continue
                bits = [f"dtype={p.get('dtype')}", f"missing={p.get('missing_frac')}"]
                if p.get("dtype") == "numeric":
                    bits += [f"min={p.get('min')}", f"max={p.get('max')}", f"mean={p.get('mean')}"]
                else:
                    bits.append(f"unique={p.get('n_unique')}")
                lines.append(f"  {name}: " + " ".join(str(b) for b in bits))
            return "\n".join(lines)[:self.max_chars]
        # Fallback: profile the training table on the fly (count/missing + numeric min/max/mean or
        # categorical cardinality) when the run recorded no profile — real per-column stats, cheaply.
        tbl = self._primary_table()
        if not tbl:
            return "(no data profile recorded for this run)"
        name, header, rows = tbl
        lines = [f"column profile from {name} ({len(rows)} rows sampled):"]
        for ci, col in enumerate(header[:60]):
            # A row too short to reach this column counts as MISSING (denominator = all sampled rows),
            # so a frequently-truncated trailing column in a ragged CSV isn't reported as fully
            # populated based only on the few rows long enough to include it.
            present = [r[ci] for r in rows if ci < len(r) and r[ci] != ""]
            missing = (1 - len(present) / len(rows)) if rows else 0.0
            nums = [float(v) for v in present if _is_number(v)]
            if present and len(nums) == len(present):             # every present value is finite-numeric
                mean = sum(nums) / len(nums)
                lines.append(f"  {col}: numeric missing={missing:.2f} "
                             f"min={digest.fmt_num(min(nums))} max={digest.fmt_num(max(nums))} "
                             f"mean={digest.fmt_num(mean)}")
            else:
                lines.append(f"  {col}: categorical missing={missing:.2f} unique={len(set(present))}")
        return "\n".join(lines)[:self.max_chars]

    def _asset(self, name: Optional[str]) -> str:
        assets = self._assets()
        if not assets:
            return "(this task has no data assets)"
        if not name:
            return "available assets: " + ", ".join(assets)
        if name not in assets:
            return f"(no asset '{name}'; available: {', '.join(assets)})"
        return f"--- {name} (truncated) ---\n{str(assets[name])[:self.max_chars]}"


def readonly_run_tools(state) -> Optional["object"]:
    """Read-only run-introspection tools bound to `state`, or None when they cannot be built.

    Five auxiliary LLM passes wanted exactly this and each wrote it out: let the model READ the run's
    actual experiments (read_code / read_experiment / read_logs / list_experiments) before it judges,
    tags, distills or reports, instead of deciding blind from the aggregate summary baked into its
    prompt. The semantic verifier (`trust/memo_verify.py`), the CLI's agentic tagging/briefing
    diagnostics, engine reflection/distillation, the run report, and the LLM novelty gate.

    Degrading to None rather than raising is the SHARED contract, and the reason this is one function:
    grounding is best-effort and every caller has a plain non-agentic path to fall back to, so a
    change to that contract — or to `bind_state(state, parent)`'s signature — must not have to be
    found by grep in five places. A caller whose degradation differs (the novelty gate keeps its
    proposal rather than making a plain call) tests for None itself.

    `CompositeTools` is imported lazily because it lives in `agents`, which imports this module — a
    module-level import here would close that cycle into an ImportError at startup.
    """
    try:
        from looplab.agents.agent import CompositeTools
        rt = RunTools()
        rt.bind_state(state, None)
        return CompositeTools([rt])
    except Exception:  # noqa: BLE001 — grounding is best-effort; the caller degrades to a plain call
        return None
