"""DatasetTask (kind="dataset"): the fully-generative "here is my data — write the whole
solution and optimize the best metric you see fit" task.

Unlike `code_regression` (synthetic polynomial data, a fixed CV-MSE objective) this points the
agent at the OPERATOR'S OWN data on disk and lets the Developer write a complete solution from
scratch each iteration — read the data, build/evaluate a model, and print one JSON line
`{"metric": <float>}`. The metric is OPEN-ENDED: if the user names one it's optimized; otherwise
the agent CHOOSES the most appropriate metric for the data + goal and self-reports it (the
"выбей лучшую метрику какую считаешь нужной" mode).

TRUST-BOUNDARY CAVEAT: like `code_regression`, the solution computes AND self-reports its own
metric — there is no private grader, so a reward-hacking model could print a fake number. That is
the deliberate trade for a zero-setup "just point at data" mode (the reward-hack / code-leakage
monitors still audit it). For the hard "the agent never authors its own metric" guarantee, use a
`repo` task (operator's own eval command + protected metric reader) or `mlebench_real` (held-out
official grader).

Data access: the generated solution reads the data by its ABSOLUTE path (given in the brief). This
works under the default `trusted_local` tier (no FS isolation — the subprocess reads any path the
operator can). Under the `untrusted`/`hostile` docker tiers an absolute host path is NOT visible
inside the container, so mount the data (a `repo` task with a `data` mount) for those tiers.
"""
from __future__ import annotations

import csv
import json
import os
import random
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .models import Idea, Node, RunState
from .parse import LLMClient
from .roles import LLMDeveloper, LLMResearcher


def _resolve(p: str) -> str:
    """Expand ~/$ENV then make ABSOLUTE: the generated solution runs in a tmp sandbox workdir, so a
    relative path would not resolve from there. Resolved once at load time (recorded in the snapshot),
    so — like a repo task — resuming on a different machine needs the path re-pointed."""
    if not isinstance(p, str) or not p:
        return p
    return os.path.abspath(os.path.expanduser(os.path.expandvars(p)))


def _coerce(v):
    """Coerce a CSV cell string to int/float when it is fully numeric, else leave it as-is. CSV cells
    are always strings, so without this the grounding profiler would label every numeric column as
    categorical (it keys numeric-ness off real int/float values)."""
    if not isinstance(v, str):
        return v
    s = v.strip()
    if not s:
        return v
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return v


# The offline baseline solution: read the dataset and report its row count. Offline (backend=toy)
# there is no LLM to write a real model, so this is a deterministic, data-touching placeholder that
# lets the engine run end to end (and proves the path plumbing) without a model.
_BASELINE_TEMPLATE = '''\
import csv, json, os

PATH = {path!r}


def _rows(path):
    if not path or not os.path.exists(path):
        return 0
    if os.path.isdir(path):
        return sum(1 for _ in os.scandir(path))
    if path.lower().endswith((".csv", ".tsv")):
        delim = "\\t" if path.lower().endswith(".tsv") else ","
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
            return max(0, sum(1 for _ in csv.reader(f, delimiter=delim)) - 1)  # minus header
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return sum(1 for line in f if line.strip())


print(json.dumps({{"metric": float(_rows(PATH)), "metric_name": "row_count (offline baseline)"}}))
'''


class DatasetResearcher:
    """Free-form proposer: a draft, then improve-from-best. The Developer writes the code from the
    rationale, so params are empty (no fixed numeric knobs in an open-ended task)."""

    def __init__(self, seed: int = 0):
        self.rng = random.Random(seed)

    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        if parent is None:
            return Idea(operator="draft", params={},
                        rationale="Establish a working baseline solution on the dataset.")
        return Idea(operator="improve", params={},
                    rationale=(f"Improve on node {parent.id} (metric={parent.metric}). Make one "
                               "focused modeling change to raise the metric."))


class DatasetBaselineDeveloper:
    """Offline developer: emits the deterministic data-reading baseline (no LLM)."""

    def __init__(self, path: str):
        self.path = path

    def implement(self, idea: Idea) -> str:
        return _BASELINE_TEMPLATE.format(path=self.path)

    def repair(self, idea: Idea, code: str, error: str) -> str:   # crash -> re-emit the baseline
        return _BASELINE_TEMPLATE.format(path=self.path)


class DatasetTask(BaseModel):
    kind: str = "dataset"
    id: str = "dataset_task"
    goal: str = ""
    direction: str = "max"                      # default: a higher-is-better metric
    seed: int = 0
    data_path: str = ""                         # abs path to the data (file or dir) the agent reads
    data: dict[str, str] = Field(default_factory=dict)   # optional extra named paths (name -> path)
    metric: str = ""                            # optional metric name; empty -> the agent chooses
    cv_k: int = 5                               # eval-protocol hint surfaced in the brief
    sample_rows: int = 200                      # rows `columns()` samples for the grounding pre-phase

    @field_validator("direction")
    @classmethod
    def _direction_valid(cls, v):
        if v not in ("min", "max"):
            raise ValueError(f"direction must be 'min' or 'max', got {v!r}")
        return v

    @model_validator(mode="after")
    def _resolve_and_require_data(self):
        self.data_path = _resolve(self.data_path)
        self.data = {k: _resolve(v) for k, v in self.data.items()}
        paths = [p for p in [self.data_path, *self.data.values()] if p]   # drop blank-path entries
        if not paths:
            raise ValueError("DatasetTask needs data: set `data_path` (a file/dir) and/or `data`.")
        # Fail LOUD on a missing path instead of letting the run silently score a degenerate metric
        # (the solution reads the data by absolute path; a typo / wrong-CWD relative path resolved to
        # nowhere would just read 0 rows). Use an absolute path — ~ and $VARS are expanded.
        missing = [p for p in paths if not os.path.exists(p)]
        if missing:
            raise ValueError(f"DatasetTask data path(s) not found: {', '.join(missing)} "
                             "(use an absolute path; ~ and $VARS are expanded).")
        return self

    # ------- TaskAdapter hooks -------
    def _primary_path(self) -> str:
        return self.data_path or (next(iter(self.data.values()), "") if self.data else "")

    def _paths(self) -> list[tuple[str, str]]:
        """(label, abs path) for every data source, primary first."""
        out: list[tuple[str, str]] = []
        if self.data_path:
            out.append(("data", self.data_path))
        out += [(name, path) for name, path in self.data.items()]
        return out

    def columns(self) -> dict[str, list]:
        """Profile a tabular primary data file (CSV/TSV/JSON) for the grounding pre-phase (I16).
        Returns {} for a directory / non-tabular / unparseable source (grounding is optional)."""
        p = self._primary_path()
        if not p or not os.path.isfile(p):
            return {}
        n = max(1, int(self.sample_rows))
        try:
            low = p.lower()
            if low.endswith((".csv", ".tsv")):
                with open(p, newline="", encoding="utf-8-sig", errors="replace") as f:
                    reader = csv.reader(f, delimiter="\t" if low.endswith(".tsv") else ",")
                    rows = []
                    for i, row in enumerate(reader):
                        rows.append(row)
                        if i >= n:                  # header (row 0) + n data rows
                            break
                if len(rows) < 2:
                    return {}
                header = [h or f"col{i}" for i, h in enumerate(rows[0])]
                cols: dict[str, list] = {h: [] for h in header}
                for row in rows[1:]:
                    for h, val in zip(header, row):
                        cols[h].append(_coerce(val))
                return cols
            if low.endswith(".json"):
                with open(p, encoding="utf-8-sig", errors="replace") as f:
                    obj = json.load(f)
                if isinstance(obj, dict) and obj and all(isinstance(v, list) for v in obj.values()):
                    return {k: list(v)[:n] for k, v in obj.items()}
                if isinstance(obj, list) and obj and isinstance(obj[0], dict):
                    keys = list(obj[0].keys())   # guard non-dict rows: a mixed list must not crash
                    return {k: [r.get(k) if isinstance(r, dict) else None for r in obj[:n]]
                            for k in keys}
        except (OSError, ValueError, TypeError, AttributeError, csv.Error):
            return {}
        return {}

    def assets(self) -> dict[str, str]:
        return {}                               # data is read by absolute path, not embedded

    def build_roles(self):                      # offline: deterministic data-reading baseline
        return (DatasetResearcher(seed=self.seed),
                DatasetBaselineDeveloper(self._primary_path()))

    def _brief(self, runtime_caps: Optional[str] = None) -> str:
        higher = self.direction == "max"
        sense = "HIGHER is better" if higher else "LOWER is better"
        objective = ("maximize" if higher else "minimize")
        srcs = "\n".join(f"  - {label}: {path}" for label, path in self._paths())
        goal = self.goal.strip() or ("Explore the data and build the best predictive/analytical "
                                     "solution you can.")
        if self.metric:
            metric_line = (f"Optimize the metric '{self.metric}'. Print it as the JSON `metric` value, "
                           f"with the SAME orientation as the loop ({sense}); if it is naturally an "
                           "error/loss, report its negative so the printed value follows that "
                           "orientation.")
        else:
            metric_line = ("CHOOSE the most appropriate metric for this data and goal (e.g. accuracy / "
                           "F1 / AUC / R^2 / RMSE) and report BOTH its value as `metric` and its name "
                           f"as `metric_name`. The loop will {objective} `metric`, so report it such "
                           f"that {sense} (for an error/loss metric, report its NEGATIVE).")
        caps = runtime_caps or ("You may use numpy, pandas and scikit-learn plus the Python standard "
                                "library; CPU only, no network.")
        return (
            f"You are an ML/DS agent. Goal: {goal}\n"
            f"The dataset is on local disk (read it directly by its absolute path; do NOT assume it is "
            f"in the working directory):\n{srcs}\n"
            "Write a COMPLETE, self-contained Python script that reads the data, builds a model (or "
            "the appropriate analysis), and EVALUATES it honestly — use a held-out split or "
            f"{self.cv_k}-fold cross-validation and avoid train/test leakage. "
            f"{metric_line}\n"
            f"{caps}\n"
            'Print EXACTLY one final line of JSON: {"metric": <float>'
            + (', "metric_name": "<name>"' if not self.metric else "")
            + "}. Print nothing after that line. If a library is missing, fall back to one that is "
            "available rather than crashing.")

    def llm_roles(self, client: LLMClient, parser: str = "tool_call",
                  runtime_caps: Optional[str] = None):
        hint = ("Propose the next concrete modeling approach to try on this dataset (as a short "
                "rationale) — e.g. a model family, feature engineering, or a regularization change. "
                "Leave params empty; the Developer writes the code from your rationale.")
        return (LLMResearcher(client, space_hint=hint, bounds=None, parser=parser),
                LLMDeveloper(client, brief=self._brief(runtime_caps)))
