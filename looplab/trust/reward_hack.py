"""B5 · Reward-hacking detector (ADR-13 trust). A lightweight, host-side monitor that flags
*suspicious* wins so an operator can see specification-gaming live — agents that import the grader
/ answer key, write to protected (frozen) files at runtime, or report a suspiciously-perfect metric.

Audit-only and defense-in-depth: it NEVER changes best-selection or a node's metric (that's the
host-side scoring job, §B1). It only emits a `reward_hack_suspected` event surfaced in the Trust
panel. Pure function of the node's code + metric + the protected-name set, so it's deterministic and
adds nothing to the event log on a clean node. OFF by default (`reward_hack_detect=False`).
"""
from __future__ import annotations

import re

# Tokens that, appearing in a solution's code, suggest it is reaching for the held-out answer key
# rather than learning. Tuned for LoopLab's eval contracts (mlebench `grader._Y`, repo answer files).
_GRADER_PATTERNS = [
    r"\bimport\s+grader\b", r"\bfrom\s+grader\b", r"grader\._Y", r"\b_Y\b",
    r"answer[_-]?key", r"test[_-]?labels", r"y[_-]?test\b.*read", r"solutions?\.csv",
]
_WRITE_RE = re.compile(r"open\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"][wa]")


def detect_reward_hacks(code: str, metric: float | None, direction: str,
                        protected_names: set[str] | None = None,
                        stdout: str = "") -> list[dict]:
    """Return a list of {signal, detail} for each suspicious pattern found (empty == clean).

    Signals:
      - grader_access   : the code references the grader / answer key / held-out labels.
      - protected_write : the code opens a *protected* (frozen) file for writing at runtime.
      - perfect_metric  : a suspiciously-optimal score (exact theoretical floor/ceiling).
    """
    code = code or ""
    protected = {str(p).replace("\\", "/").lower() for p in (protected_names or set())}
    signals: list[dict] = []

    # Match against the ORIGINAL code (case-insensitively): the answer-key tells `grader._Y` and
    # `\b_Y\b` are uppercase, so searching a pre-lowercased copy would make them dead — they could
    # never match. IGNORECASE keeps the textual tells (import grader, answer_key) firing too.
    for pat in _GRADER_PATTERNS:
        m = re.search(pat, code, re.IGNORECASE)
        if m:
            signals.append({"signal": "grader_access",
                            "detail": f"code references the answer key / grader ({m.group(0)!r})"})
            break

    if protected:
        for w in _WRITE_RE.findall(code):
            if w.replace("\\", "/").lower() in protected:
                signals.append({"signal": "protected_write",
                                "detail": f"runtime write to a protected/frozen file: {w!r}"})
                break

    # Suspiciously-perfect score: an exact theoretical optimum is rare from real learning and is the
    # classic specification-gaming tell (e.g. MSE == 0.0, accuracy == 1.0). Heuristic, audit-only.
    if metric is not None:
        if direction == "min" and metric <= 0.0:
            signals.append({"signal": "perfect_metric",
                            "detail": f"metric {metric} at/below the theoretical floor (0.0)"})
        elif direction == "max" and metric >= 1.0:
            signals.append({"signal": "perfect_metric",
                            "detail": f"metric {metric} at/above the theoretical ceiling (1.0)"})

    return signals
