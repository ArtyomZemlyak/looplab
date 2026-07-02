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
from typing import Optional

from pydantic import BaseModel

from .models import Idea, Node, RunState
from .parse import LLMClient
from .roles import LLMResearcher


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


class TimeSeriesTask(BaseModel):
    kind: str = "timeseries"
    id: str = "seasonal_forecast"
    goal: str = "choose a forecaster's smoothing weight + seasonal period to minimize backtest MASE"
    direction: str = "min"
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
        return (TimeSeriesResearcher(max_period=self.max_period, seed=self.seed),
                TimeSeriesDeveloper(self._series(), h=self.backtest_h))

    def llm_roles(self, client: LLMClient, parser: str = "tool_call"):
        hint = (f"Choose 'alpha' (float 0..1, blend of last value vs seasonal value) and 'period' "
                f"(integer 1..{self.max_period}, the seasonal cycle length). Lower backtest MASE is "
                "better (MASE < 1 beats the naive forecast).")
        bounds = {"alpha": (0.0, 1.0), "period": (1.0, float(self.max_period))}
        return (LLMResearcher(client, space_hint=hint, bounds=bounds, parser=parser),
                TimeSeriesDeveloper(self._series(), h=self.backtest_h))
