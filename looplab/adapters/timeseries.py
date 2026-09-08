"""I2 · Time-series forecasting TaskAdapter (ADR-2). A net-new task kind: forecast a synthetic
seasonal+trend series, scored by a rolling-origin BACKTEST (MASE) the ADAPTER owns.

WHAT THIS ADAPTER OWNS, AND WHAT IT NO LONGER DOES (2026-09-08, docs/BACKLOG.md §14). It used to own
the FORECASTER as well: a `_TS_TEMPLATE` string with an exponential/seasonal blend written inline and
handed to BOTH role pairs, so even under `backend=llm` the model only ever picked two floats and no
run ever wrote a forecaster — the task validated LoopLab's plumbing rather than any forecasting
capability. What it ships now is the DATA and the METRIC, as three assets staged into every eval
workdir (and protected from edits there, like every `assets()` entry):

  * `series.json`  — the data: the series plus the number of backtest origins;
  * `backtest.py`  — the METRIC: a rolling-origin one-step-ahead backtest scored as MASE;
  * `baseline.py`  — a DECLARED BASELINE: the seasonal blend that used to BE the solution.

The forecaster is the candidate's job. `llm_roles` hands the model an `LLMDeveloper` that writes the
script against those three files — the same shape `CodeRegressionTask` uses — and the offline
`build_roles` pair stays deterministic by running the DECLARED BASELINE at the parameters the
searcher picks, which is what keeps `backend=toy` a full end-to-end run with no model on the wire.

The eval is a backtest with a forecasting metric (MASE = model MAE / naive-1-step MAE; <1 beats the
naive baseline), the standard scale-free TS metric. Validates LoopLab's generality beyond i.i.d.
tabular tasks.
"""
from __future__ import annotations

import json
import random
from typing import Optional

from pydantic import BaseModel, field_validator

from looplab.core.comparison import ComparisonContract
from looplab.core.models import Idea, Node, RunState, validate_direction
from looplab.core.parse import LLMClient
from looplab.agents.roles import LLMDeveloper, LLMResearcher

SERIES_ASSET = "series.json"      # the data
HARNESS_ASSET = "backtest.py"     # the metric
BASELINE_ASSET = "baseline.py"    # the declared baseline


def make_series(seed: int, n: int, period: int, trend: float, noise: float) -> list[float]:
    """A synthetic series with a linear trend + a seasonal cycle of length `period` + noise."""
    rng = random.Random(seed)
    season = [rng.uniform(-3.0, 3.0) for _ in range(period)]
    out = []
    for t in range(n):
        y = trend * t + season[t % period] + rng.gauss(0.0, noise)
        out.append(round(y, 4))
    return out


# The METRIC, shipped as an asset so the candidate is scored by code it did not write.
_BACKTEST_HARNESS = '''\
"""Rolling-origin backtest for the seasonal-forecast task — the METRIC, not a model.

`score(forecast)` walks the last `h` origins of the series, asks `forecast(history)` for the point
that follows each prefix, and returns MASE (model MAE / naive-1-step MAE): below 1 beats the naive
forecast. The forecaster never sees the future because it is only ever handed a PREFIX.

THE ORIGIN SET IS FIXED BY THE SERIES AND `h` ALONE, deliberately. The inline template this harness
replaced started its walk at `max(period + 1, n - h)` — a bound that moved with the candidate's own
seasonal period, so two nodes were scored over different windows and their MASEs were not
comparable. A metric one of the candidate's hyperparameters can move is not a metric.
"""
import json


def load_series(path="series.json"):
    """The task's data as `(y, h)`: the series and the number of backtest origins."""
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    return [float(v) for v in spec["y"]], int(spec["h"])


def origins(n, h):
    """The scored indices, oldest first. Index 0 is never an origin: it has no history to forecast
    from, and the naive denominator needs `y[t - 1]`."""
    return list(range(max(1, n - max(0, int(h))), n))


def score(forecast, y=None, h=None):
    """MASE of `forecast` over `origins(len(y), h)`; reads the shipped series when not given one."""
    if y is None or h is None:
        y, h = load_series()
    errs, naive = [], []
    for t in origins(len(y), h):
        errs.append(abs(y[t] - float(forecast(y[:t]))))
        naive.append(abs(y[t] - y[t - 1]))
    if not errs:
        return float("inf")
    mae = sum(errs) / len(errs)
    denom = sum(naive) / len(naive)
    return mae / denom if denom > 0 else mae
'''


# The DECLARED BASELINE, shipped beside the metric: what the candidate has to beat.
_BASELINE = '''\
"""The DECLARED BASELINE for the seasonal-forecast task — the number to beat, not the solution.

A seasonal blend: `alpha * last + (1 - alpha) * history[-period]`. This is the forecaster the
adapter used to embed as THE solution (docs/BACKLOG.md §14). It ships as an asset so a candidate can
import it, beat it or ignore it, and so the offline role pair has something deterministic to
evaluate without a model writing code.
"""


def seasonal_blend(alpha=0.5, period=7):
    """A one-step-ahead forecaster `f(history) -> float` for `backtest.score`."""
    a = min(1.0, max(0.0, float(alpha)))
    p = max(1, int(period))

    def forecast(history):
        last = history[-1]
        seasonal = history[-p] if len(history) >= p else last
        return a * last + (1.0 - a) * seasonal

    return forecast
'''


# What the OFFLINE role pair emits: the declared baseline at the searcher's two knobs. It writes no
# forecaster of its own — both the model and the metric are imported from the shipped assets.
_BASELINE_SOLUTION = '''\
"""The DECLARED BASELINE at alpha={alpha}, period={period} — emitted by the offline role pair.

No forecaster is written here: `baseline.py` (the declared baseline) and `backtest.py` (the metric)
are assets the task ships. Under `backend=llm` an `LLMDeveloper` writes a real forecaster in this
file instead."""
import json

import backtest
import baseline

y, h = backtest.load_series()
mase = backtest.score(baseline.seasonal_blend({alpha}, {period}), y, h)
print(json.dumps({{"metric": mase}}))
'''


class TimeSeriesResearcher:
    """Blind optimizer over (alpha in [0,1], seasonal period int)."""

    def __init__(self, max_period: int = 12, seed: int = 0):
        self.max_period = max_period
        self.rng = random.Random(seed)

    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        if parent is None:
            return Idea(operator="draft",
                        params={"alpha": round(self.rng.random(), 3),
                                "period": float(self.rng.randint(1, self.max_period))},
                        rationale="random forecaster config")
        pa = parent.idea.params.get("alpha", 0.5)
        alpha = min(1.0, max(0.0, round(pa + self.rng.gauss(0.0, 0.15), 3)))
        pp = int(round(parent.idea.params.get("period", 4)))
        period = max(1, min(self.max_period, pp + self.rng.choice([-1, 0, 1])))
        return Idea(operator="improve", params={"alpha": alpha, "period": float(period)},
                    rationale=f"perturb node {parent.id} (alpha={pa})")


class TimeSeriesBaselineDeveloper:
    """The offline (`backend=toy`) Developer: run the DECLARED BASELINE at the searcher's knobs.

    Deliberately NOT a forecaster factory — it emits six lines that import the two shipped assets, so
    the only forecasting code in this task's tree is the baseline the operator declared and whatever
    the candidate writes. Named `Baseline` for the reason `dataset_task.py`'s offline Developer is
    (`DatasetBaselineDeveloper`): what it produces is the floor, not the answer."""

    def implement(self, idea: Idea) -> str:
        return _BASELINE_SOLUTION.format(
            alpha=float(idea.params.get("alpha", 0.5)),
            period=int(round(idea.params.get("period", 4))),
        )


class TimeSeriesTask(BaseModel):
    kind: str = "timeseries"
    id: str = "seasonal_forecast"
    goal: str = ("forecast a seasonal+trend series: write a forecaster that minimizes the "
                 "rolling-origin backtest MASE")
    direction: str = "min"

    @field_validator("direction")
    @classmethod
    def _direction_valid(cls, v):
        return validate_direction(v)
    comparison_contract: ComparisonContract | None = None
    n: int = 120
    period: int = 7
    trend: float = 0.05
    noise: float = 0.5
    seed: int = 0
    max_period: int = 12
    backtest_h: int = 20

    def _series(self) -> list[float]:
        return make_series(self.seed, self.n, self.period, self.trend, self.noise)

    def columns(self) -> dict[str, list]:
        return {"t": list(range(self.n)), "y": self._series()}

    def assets(self) -> dict[str, str]:
        """The data + the metric + the declared baseline, staged into every eval workdir (and
        protected from edits there). The series is DATA here rather than a literal spliced into the
        solution, which is what lets the candidate own the forecaster."""
        return {
            SERIES_ASSET: json.dumps({"y": self._series(), "h": int(self.backtest_h)}),
            HARNESS_ASSET: _BACKTEST_HARNESS,
            BASELINE_ASSET: _BASELINE,
        }

    def build_roles(self):
        return (TimeSeriesResearcher(max_period=self.max_period, seed=self.seed),
                TimeSeriesBaselineDeveloper())

    def llm_roles(self, client: LLMClient, parser: str = "tool_call"):
        hint = (f"Choose 'alpha' (float 0..1, how much weight the forecaster puts on the most recent "
                f"value rather than the seasonal one) and 'period' (integer 1..{self.max_period}, "
                "the seasonal cycle length it assumes). The Developer WRITES the forecaster from "
                "those two numbers, so a proposal may also change the model family (damped trend, "
                "seasonal naive, drift, a blend of them) in its rationale. Lower backtest MASE is "
                "better (MASE < 1 beats the naive forecast; the declared baseline in baseline.py is "
                "the number to beat).")
        bounds = {"alpha": (0.0, 1.0), "period": (1.0, float(self.max_period))}
        # The I/O CONTRACT, and it is a contract: the metric and the data are files the task ships
        # and the eval protects, so a solution that recomputes the backtest itself is scoring itself.
        brief = (
            "The script MUST read the series with the shipped harness: `import backtest` then "
            f"`y, h = backtest.load_series()` (it reads './{SERIES_ASSET}', a JSON object with 'y', "
            "a list of floats, and 'h', the number of backtest origins). Write a ONE-STEP-AHEAD "
            "forecaster `f(history) -> float` returning the value that follows `history` (a prefix "
            "of the series), and score it with `mase = backtest.score(f, y, h)` — do NOT write your "
            "own backtest loop, the harness is the metric. "
            f"'./{BASELINE_ASSET}' holds the DECLARED BASELINE (`seasonal_blend(alpha, period)`); "
            'beat it. Print EXACTLY one final line of JSON: {"metric": <float>} where <float> is '
            "that MASE (lower is better). Use ONLY numpy and the Python standard library — "
            "statsmodels, sktime, darts, pandas and scipy are NOT installed and importing them will "
            "crash. Print nothing after that JSON line."
        )
        return (LLMResearcher(client, space_hint=hint, bounds=bounds, parser=parser),
                LLMDeveloper(client, brief=brief))

    def external_fallback_uses_llm(self) -> bool:
        return True  # output validation retains the script-writing LLMDeveloper (as code_regression)

    def gpu_capable(self) -> bool:
        """The LLM path writes the code here, but the brief above pins it to numpy + the standard
        library — the same offline stack `core/hardware.py::task_runtime_caps` locks this task to,
        and it never offers it the torch capability sentence — while the offline path only ever runs
        the declared baseline. Keeps this task out of the host GPU pool lease
        (`engine/resources.py::_task_gpu_capable`)."""
        return False
