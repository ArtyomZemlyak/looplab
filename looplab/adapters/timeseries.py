"""I2 · Time-series forecasting TaskAdapter (ADR-2). A net-new task kind: pick a forecaster's
smoothing weight + seasonal period to minimize a rolling-origin BACKTEST error (MASE) on a synthetic
seasonal+trend series. Pure-Python end to end (the generated solution embeds the series + a
self-contained exponential/seasonal forecaster + backtest), so it runs in the sandbox with no
forecasting-library dependency — the same shape as RegressionTask but for sequential data.

The eval is a backtest with a forecasting metric (MASE = model MAE / naive-1-step MAE; <1 beats the
naive baseline), the standard scale-free TS metric. Validates LoopLab's generality beyond i.i.d.
tabular tasks; a real AutoGluon-TS/Darts backend is a drop-in replacement for the templated forecaster.
"""
from __future__ import annotations

import random

from looplab.adapters.synthetic import IntWalk, Jitter, PerturbResearcher, SyntheticTaskBase
from looplab.core.models import Idea
from looplab.core.parse import LLMClient
from looplab.agents.roles import LLMResearcher


def make_series(seed: int, n: int, period: int, trend: float, noise: float) -> list[float]:
    """A synthetic series with a linear trend + a seasonal cycle of length `period` + noise."""
    rng = random.Random(seed)
    season = [rng.uniform(-3.0, 3.0) for _ in range(period)]
    out = []
    for t in range(n):
        y = trend * t + season[t % period] + rng.gauss(0.0, noise)
        out.append(round(y, 4))
    return out


# The generated solution: a seasonal-blend forecaster (alpha*last + (1-alpha)*seasonal) backtested
# over the last H steps; metric = MASE (model MAE / naive-1-step MAE).
_TS_TEMPLATE = '''\
import json

Y = {Y}
ALPHA = {alpha}
PERIOD = {period}
H = {h}


def forecast(hist, alpha, period):
    last = hist[-1]
    seasonal = hist[-period] if len(hist) >= period else last
    return alpha * last + (1.0 - alpha) * seasonal


n = len(Y)
period = max(1, int(PERIOD))
start = max(period + 1, n - H)
errs, naive = [], []
for t in range(start, n):
    yhat = forecast(Y[:t], ALPHA, period)
    errs.append(abs(Y[t] - yhat))
    naive.append(abs(Y[t] - Y[t - 1]))
mae = sum(errs) / len(errs) if errs else float("inf")
denom = sum(naive) / len(naive) if naive else 1.0
mase = mae / denom if denom > 0 else mae
print(json.dumps({{"metric": mase}}))
'''


def timeseries_researcher(max_period: int = 12, seed: int = 0) -> PerturbResearcher:
    """Blind optimizer over (alpha in [0,1], seasonal period int).

    The two knobs are of different KINDS and that is the point of the pair: `alpha` is a continuous
    blend weight that a Gaussian step explores, while `period` is the cycle length — an integer that
    either matches the season or does not, so it walks in whole units. Collapsed onto
    `PerturbResearcher` (doc 25 RA-06); alpha draws first in both branches, as it always did.
    """
    return PerturbResearcher(
        (Jitter("alpha", sigma=0.15, lo=0.0, hi=1.0, ndigits=3, default=0.5),
         IntWalk("period", 1, max_period, default=4.0)),
        seed=seed, draft_rationale="random forecaster config")


class TimeSeriesDeveloper:
    def __init__(self, series: list[float], h: int = 12):
        self.series = series
        self.h = h

    def implement(self, idea: Idea) -> str:
        return _TS_TEMPLATE.format(
            Y=self.series,
            alpha=float(idea.params.get("alpha", 0.5)),
            period=int(round(idea.params.get("period", 4))),
            h=self.h,
        )


class TimeSeriesTask(SyntheticTaskBase):
    kind: str = "timeseries"
    id: str = "seasonal_forecast"
    goal: str = "choose a forecaster's smoothing weight + seasonal period to minimize backtest MASE"
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

    def build_roles(self):
        return (timeseries_researcher(max_period=self.max_period, seed=self.seed),
                TimeSeriesDeveloper(self._series(), h=self.backtest_h))

    def llm_roles(self, client: LLMClient, parser: str = "tool_call"):
        hint = (f"Choose 'alpha' (float 0..1, blend of last value vs seasonal value) and 'period' "
                f"(integer 1..{self.max_period}, the seasonal cycle length). Lower backtest MASE is "
                "better (MASE < 1 beats the naive forecast).")
        bounds = {"alpha": (0.0, 1.0), "period": (1.0, float(self.max_period))}
        return (LLMResearcher(client, space_hint=hint, bounds=bounds, parser=parser),
                TimeSeriesDeveloper(self._series(), h=self.backtest_h))

    def gpu_capable(self) -> bool:
        """Both role pairs end in `TimeSeriesDeveloper`, a fixed seasonal-naive numpy template — the
        roles only pick `alpha`/`period`, never code. Keeps this task out of the host GPU pool lease
        (`engine/resources.py::_task_gpu_capable`)."""
        return False
