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
from typing import Optional

from . import digest
from .models import NodeStatus, RunState


def _is_number(v: str) -> bool:
    """True only if the string parses as a FINITE number. Rejects the 'nan'/'inf'/'infinity'
    sentinels (which float() happily accepts) so a column of textual missing-markers reads as
    categorical — flagging it as needing missing-value handling — instead of numeric with
    NaN/inf-poisoned (and order-dependent) min/max/mean."""
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _fn(name: str, description: str, props: dict, required: Optional[list] = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props, "required": required or []}}}


class RunTools:
    """Read-only view over the live search DAG (the bound `RunState`)."""

    def __init__(self, max_chars: int = 3500):
        self.max_chars = max_chars
        self.state: Optional[RunState] = None
        self.parent = None

    # The agent loop calls this each turn so the tools see the current run.
    def bind_state(self, state: RunState, parent=None) -> None:
        self.state = state
        self.parent = parent

    def specs(self) -> list[dict]:
        return [
            _fn("list_experiments",
                "List experiments tried so far (the search DAG). Use to see what's been done before "
                "proposing. `sort`: best|worst|recent.",
                {"sort": {"type": "string", "enum": ["best", "worst", "recent"]},
                 "limit": {"type": "integer"},
                 "theme": {"type": "string", "description": "filter to one theme slug (optional)"}}),
            _fn("read_experiment",
                "Read one experiment's full detail: params, metric, robustness, rationale, failure "
                "reason, extra metrics, and sweep trials.",
                {"node_id": {"type": "integer"}}, ["node_id"]),
            _fn("read_code",
                "Read the solution code of one experiment (so you can build on or avoid it).",
                {"node_id": {"type": "integer"}}, ["node_id"]),
            _fn("find_analogous",
                "Find experiments most similar to a given one (or to a set of params) by parameter "
                "distance — to see how nearby configs performed before committing.",
                {"node_id": {"type": "integer"},
                 "params": {"type": "object", "description": "param dict to compare instead of a node"},
                 "k": {"type": "integer"}}),
            _fn("list_themes",
                "List the experiment themes explored so far with counts and best metric per theme.",
                {}),
        ]

    def execute(self, name: str, args: dict) -> str:
        st = self.state
        if st is None:
            return "(run state unavailable)"
        try:
            if name == "list_experiments":
                return self._list(st, args)
            if name == "read_experiment":
                return self._read(st, int(args.get("node_id")))
            if name == "read_code":
                return self._code(st, int(args.get("node_id")))
            if name == "find_analogous":
                return self._analogous(st, args)
            if name == "list_themes":
                return self._themes(st)
            return f"(unknown tool: {name})"
        except (KeyError, TypeError, ValueError) as e:
            return f"(tool error: {e})"

    # --- implementations ----------------------------------------------------
    def _line(self, n) -> str:
        if n.status is NodeStatus.failed:
            outcome = f"FAILED({n.error_reason or 'error'})"
        else:
            outcome = f"metric={digest._fmt_num(digest.node_metric(n))}"
        theme = f" {{{n.idea.theme}}}" if getattr(n.idea, "theme", None) else ""
        return f"#{n.id} {n.operator} {outcome} {digest._fmt_params(n.idea.params)}{theme}"

    def _list(self, st: RunState, args: dict) -> str:
        sort = (args.get("sort") or "best").lower()
        limit = int(args.get("limit") or 10)
        theme = args.get("theme")
        if sort == "recent":
            nodes = sorted(st.nodes.values(), key=lambda n: n.id, reverse=True)
        else:
            nodes = digest.top_nodes(st, len(st.nodes), worst=(sort == "worst"))
        if theme:
            nodes = [n for n in nodes if getattr(n.idea, "theme", None) == theme]
        nodes = nodes[:limit]
        if not nodes:
            return "(no matching experiments)"
        head = f"{len(nodes)} experiment(s), sort={sort}" + (f", theme={theme}" if theme else "")
        return head + ":\n" + "\n".join(self._line(n) for n in nodes)

    def _read(self, st: RunState, nid: int) -> str:
        n = st.nodes.get(nid)
        if n is None:
            return f"(no experiment #{nid})"
        out = [f"experiment #{n.id} — operator={n.operator} status={n.status.value}",
               f"parents={n.parent_ids or '[]'}",
               f"params={n.idea.params}"]
        if n.idea.space:
            out.append(f"sweep_space={n.idea.space}")
        out.append(f"metric={digest._fmt_num(n.metric)}")
        if n.confirmed_mean is not None:
            out.append(f"confirmed={digest._fmt_num(n.confirmed_mean)} "
                       f"±{digest._fmt_num(n.confirmed_std)} ({n.confirmed_seeds} seeds)")
        if n.extra_metrics:
            out.append(f"extra_metrics={n.extra_metrics}")
        if n.violations:
            out.append(f"violations={n.violations}")
        if n.status is NodeStatus.failed:
            out.append(f"failure={n.error_reason}: {(n.error or '')[:300]}")
        if n.trials:
            best = min((t for t in n.trials if t.metric is not None),
                       key=lambda t: t.metric if st.direction == "min" else -t.metric, default=None)
            out.append(f"sweep: {len(n.trials)} trials" +
                       (f", best {digest._fmt_params(best.params)} metric={digest._fmt_num(best.metric)}"
                        if best else ""))
        if n.idea.rationale:
            out.append(f"rationale: {n.idea.rationale.strip()[:400]}")
        return "\n".join(out)

    def _code(self, st: RunState, nid: int) -> str:
        n = st.nodes.get(nid)
        if n is None:
            return f"(no experiment #{nid})"
        if not n.code and not n.files:
            return f"(experiment #{nid} has no code recorded)"
        files = (f"\nother files: {list(n.files)}" if n.files else "")
        return f"# solution.py of experiment #{nid}\n{n.code[:self.max_chars]}{files}"

    def _analogous(self, st: RunState, args: dict) -> str:
        nid = args.get("node_id")
        if args.get("params"):
            target, exclude = dict(args["params"]), None
        elif nid is not None and int(nid) in st.nodes:
            exclude = int(nid)
            target = st.nodes[exclude].idea.params
        else:
            return "(give a node_id or params to compare)"
        scored = []
        for n in st.nodes.values():
            if n.id == exclude:
                continue
            d = digest.param_distance(target, n.idea.params)
            if d != float("inf"):
                scored.append((d, n))
        scored.sort(key=lambda t: t[0])
        k = int(args.get("k") or 3)
        if not scored:
            return "(no comparable experiments — no shared numeric params)"
        return "nearest by param-distance:\n" + "\n".join(
            f"dist={d:.3f}  {self._line(n)}" for d, n in scored[:k])

    def _themes(self, st: RunState) -> str:
        roll = digest.theme_rollup(st)
        if not roll:
            return "(no themes assigned yet)"
        return "\n".join(
            f"{t}: {d['count']} experiment(s)" +
            (f", best={digest._fmt_num(d['best_metric'])}" if d['best_metric'] is not None else "")
            for t, d in sorted(roll.items(), key=lambda kv: -kv[1]["count"]))


class DataTools:
    """Read the concrete task data — schema, column profiling, and asset samples — so the Researcher
    proposes from the REAL data rather than guessing. Degrades gracefully for tasks with no dataset
    (e.g. toy/repo tasks). Uses only the documented TaskAdapter surface (`columns`/`assets`)."""

    def __init__(self, task, max_chars: int = 3500):
        self.task = task
        self.max_chars = max_chars
        self.state: Optional[RunState] = None

    def bind_state(self, state: RunState, parent=None) -> None:
        self.state = state

    def specs(self) -> list[dict]:
        return [
            _fn("data_schema", "Show the task's data schema — column names, types, and a couple of "
                "sample values — so you propose from the real fields.", {}),
            _fn("data_profile", "Per-column statistics of the task data — missing fraction, numeric "
                "min/max/mean, and categorical cardinality (derived from the training table).", {}),
            _fn("read_asset", "Read a sample of a task data asset (e.g. train/test). Omit `name` to "
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
        return (fn() if callable(fn) else {}) or {}

    _PROFILE_ROWS = 5000          # cap on retained rows (bounds the parse + the sample size)
    _MAX_TABLE_CHARS = 4_000_000  # cap on the text actually parsed, so neither the StringIO copy
                                  # nor the parse scales with a multi-hundred-MB table

    def _primary_table(self):
        """Pick the most representative training table among the CSV assets (prefer ``train*.csv``,
        else the first CSV) and parse a bounded prefix into at most ``_PROFILE_ROWS`` rows. Returns
        ``(name, header, rows)`` or ``None`` when no parseable CSV asset exists — so schema/profile
        can derive a real view from the actual data even when the task declares no structured
        ``columns()``. Only the first ``_MAX_TABLE_CHARS`` are wrapped/parsed, so a huge file isn't
        copied whole here. (The task's ``assets()`` still materializes each file once upstream — that
        read is outside this read-only tool's control.)"""
        tables = {n: v for n, v in self._assets().items()
                  if isinstance(v, str) and n.lower().endswith(".csv")}
        if not tables:
            return None
        name = next((n for n in tables if n.lower().startswith("train")), None) or sorted(tables)[0]
        try:
            reader = csv.reader(io.StringIO(tables[name][:self._MAX_TABLE_CHARS]))
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
                             f"min={digest._fmt_num(min(nums))} max={digest._fmt_num(max(nums))} "
                             f"mean={digest._fmt_num(mean)}")
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
