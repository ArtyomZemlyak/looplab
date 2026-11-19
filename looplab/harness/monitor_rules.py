"""DECLARED ROUTINE ANSWERS for evaluation checkpoints, executed by LoopLab (doc 78 §4).

WHY (audit 2026-10-14, L-05). An external run with live monitoring opens a question at every
observation tick, and the engine waits for each answer. Codex answered them with a regex bot
(`monitor.py`: grep Traceback/OOM/NaN, then POST `continue`): 142 of 218 answers were one identical
string with a 1-2 s latency. The answers were indistinguishable from the agent's own — the UI said
"external agent observed" and the agent-activity chip stayed green while the LLM was dead — and when
the bot met a traceback it raised, so the question hung and the stage failure was never recorded.

WHAT. The agent DECLARES its routine policy once (`PUT /api/runs/{run_id}/harness-monitor-rules`,
stored as `<run>/harness_monitor_rules.json`), and `harness/checkpoints.py::ask` evaluates it the
moment a question opens. When every declared condition holds, LoopLab writes the answer itself —
`continue` for a live monitor, `proceed` for a stage check — with `answerer: "rule"`, the rule that
fired and the facts it read. Anything else (a forbidden pattern, a non-finite loss, no visible
progress, a missing required line, a question kind without rules) stays open for the agent, and only
those are what the agent is woken for (`monitor_question`).

Rules may only say "this is routine". They never abort a run, fail a stage or extend a deadline: an
anomaly is exactly what the agent must judge. The vocabulary is closed and small on purpose:

  * `loss_finite`: the observation shows a loss value and the last one is finite;
  * `progress_advances`: a step/iteration/epoch counter is visible and, after the first answered
    observation of the same node lifecycle and phase, strictly larger than the last one a rule saw;
  * `forbid_patterns`: none of these case-insensitive regexes match the observation;
  * `require_patterns`: every one of these matches.
"""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Optional

RULES_FILE = "harness_monitor_rules.json"
RULE_PHASES = ("train_monitor", "asha_live", "stage_check")
ROUTINE_VERDICT = {"train_monitor": "continue", "asha_live": "continue", "stage_check": "proceed"}
_CONDITION_KEYS = ("loss_finite", "progress_advances", "forbid_patterns", "require_patterns")
_MAX_PATTERNS = 32
_MAX_PATTERN_CHARS = 300
_MAX_ANY_OF = 8
_MAX_BYTES = 64 * 1024

# A loss value: `loss=0.31`, `loss: 0.31`, `'loss': 0.31`, `train_loss 1.2e-3`, `loss=nan`.
_LOSS = re.compile(r"""(?ix)\b[a-z_]*loss['"]?\s*[:=]?\s*
                       (?P<v>[-+]?(?:nan|inf(?:inity)?|\d+(?:\.\d*)?(?:e[-+]?\d+)?|\.\d+(?:e[-+]?\d+)?))""")
# Progress counters, most specific first: `step 1200`, `global_step=1200`, `1200/5000 [` (tqdm),
# `iter 30`, `epoch 2.5`, `rows: 32768` (scoring/inference batches).
_PROGRESS = (
    re.compile(r"(?i)\b(?:global_)?step['\"]?\s*[:=]?\s*(\d+)\b"),
    re.compile(r"(?<![\d.])(\d+)\s*/\s*\d+\s*\["),
    re.compile(r"(?i)\biter(?:ation)?['\"]?\s*[:=]?\s*(\d+)\b"),
    re.compile(r"(?i)\bepoch['\"]?\s*[:=]?\s*(\d+(?:\.\d+)?)\b"),
    re.compile(r"(?i)\brows['\"]?\s*[:=]?\s*(\d+)\b"),
)


class RulesRefused(ValueError):
    pass


def rules_path(rd) -> Path:
    return Path(rd) / RULES_FILE


def _validate_branch(phase: str, spec, *, default_rule_id: str) -> dict:
    cond = spec.get("routine_if") if isinstance(spec, dict) else None
    if not isinstance(cond, dict) or not cond:
        raise RulesRefused(f"{phase} needs a non-empty `routine_if` object")
    bad = set(cond) - set(_CONDITION_KEYS)
    if bad:
        raise RulesRefused(f"{phase}: unknown conditions {sorted(bad)}; "
                           f"choose from {list(_CONDITION_KEYS)}")
    clean = {}
    for key in ("loss_finite", "progress_advances"):
        if key in cond:
            if not isinstance(cond[key], bool):
                raise RulesRefused(f"{phase}.{key} must be true or false")
            clean[key] = cond[key]
    for key in ("forbid_patterns", "require_patterns"):
        if key in cond:
            pats = cond[key]
            if (not isinstance(pats, list) or len(pats) > _MAX_PATTERNS
                    or not all(isinstance(p, str) and 0 < len(p) <= _MAX_PATTERN_CHARS
                               for p in pats)):
                raise RulesRefused(f"{phase}.{key} must be a list of at most {_MAX_PATTERNS} "
                                   "short regex strings")
            for pat in pats:
                try:
                    re.compile(pat)
                except re.error as exc:
                    raise RulesRefused(f"{phase}.{key}: invalid regex {pat!r}: {exc}") from None
            clean[key] = list(pats)
    if not any(clean.get(k) for k in _CONDITION_KEYS):
        raise RulesRefused(f"{phase}: at least one condition must be enabled")
    rule_id = spec.get("rule_id") if isinstance(spec, dict) else None
    return {"routine_if": clean,
            "rule_id": (rule_id if isinstance(rule_id, str) and rule_id
                        else default_rule_id)[:80]}


def validate_rules(body) -> dict:
    """The one validator of a rules declaration. Raises `RulesRefused` with a readable reason."""
    if not isinstance(body, dict):
        raise RulesRefused("rules must be an object keyed by phase")
    unknown = set(body) - set(RULE_PHASES) - {"version", "declared_by", "declared_at"}
    if unknown:
        raise RulesRefused(f"unknown phases {sorted(unknown)}; rules exist for {list(RULE_PHASES)} "
                           "(deadline_grace is always the agent's decision)")
    out = {"version": 1}
    for phase in RULE_PHASES:
        spec = body.get(phase)
        if spec is None:
            continue
        if isinstance(spec, dict) and "any_of" in spec:
            if "routine_if" in spec:
                raise RulesRefused(f"{phase}: specify either `routine_if` or `any_of`, not both")
            raw_branches = spec["any_of"]
            if (not isinstance(raw_branches, list) or not (1 <= len(raw_branches) <= _MAX_ANY_OF)
                    or not all(isinstance(b, dict) for b in raw_branches)):
                raise RulesRefused(f"{phase}.any_of must be a list of 1..{_MAX_ANY_OF} rule objects")
            out[phase] = {"any_of": [
                _validate_branch(phase, branch, default_rule_id=f"{phase}-routine-{idx + 1}")
                for idx, branch in enumerate(raw_branches)
            ]}
        else:
            out[phase] = _validate_branch(phase, spec, default_rule_id=f"{phase}-routine")
    return out


def read_rules(rd) -> Optional[dict]:
    path = rules_path(rd)
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > _MAX_BYTES:
        return None
    try:
        data = json.loads(raw)
        rules = validate_rules(data)
    except (ValueError, RulesRefused):
        return None     # a damaged declaration answers nothing: every question goes to the agent
    for key in ("declared_by", "declared_at"):
        if key in data:
            rules[key] = data[key]
    return rules


def write_rules(rd, body, *, author: str = "agent") -> dict:
    rules = validate_rules(body)
    rules["declared_by"] = author
    rules["declared_at"] = time.time()
    path = rules_path(rd)
    tmp = Path(f"{path}.tmp")
    tmp.write_text(json.dumps(rules, indent=1), encoding="utf-8")
    tmp.replace(path)
    return rules


def last_loss(text: str) -> Optional[float]:
    values = [m.group("v") for m in _LOSS.finditer(text or "")]
    if not values:
        return None
    try:
        return float(values[-1])
    except ValueError:
        return None


def progress_marker(text: str) -> Optional[float]:
    for pattern in _PROGRESS:
        found = pattern.findall(text or "")
        if found:
            try:
                return max(float(v) for v in found)
            except ValueError:
                continue
    return None


def _evaluate_branch(phase: str, branch: dict, text: str,
                     previous_progress: Optional[float]) -> Optional[dict]:
    cond = branch["routine_if"]
    facts: dict = {}
    reasons = []
    for pat in cond.get("forbid_patterns") or []:
        if re.search(pat, text, flags=re.IGNORECASE):
            return None
    if cond.get("forbid_patterns"):
        reasons.append("no forbidden pattern")
    for pat in cond.get("require_patterns") or []:
        if not re.search(pat, text, flags=re.IGNORECASE):
            return None
    if cond.get("require_patterns"):
        reasons.append("required lines present")
    if cond.get("loss_finite"):
        loss = last_loss(text)
        if loss is None or not math.isfinite(loss):
            return None
        facts["loss"] = loss
        reasons.append(f"loss finite ({loss:g})")
    if cond.get("progress_advances"):
        progress = progress_marker(text)
        if progress is None:
            return None
        if previous_progress is not None and not progress > previous_progress:
            return None
        facts["progress"] = progress
        reasons.append(f"progress {progress:g}" + (f" > {previous_progress:g}"
                                                   if previous_progress is not None else ""))
    return {"verdict": ROUTINE_VERDICT[phase], "rule_id": branch["rule_id"],
            "reason": "rule " + branch["rule_id"] + ": " + "; ".join(reasons), "facts": facts}


def evaluate(rules: Optional[dict], question: dict, *,
             previous_progress: Optional[float] = None,
             previous_by_rule: Optional[dict] = None) -> Optional[dict]:
    """The routine answer for `question`, or None when the agent must decide.

    Returns `{verdict, rule_id, reason, facts}`; `facts.progress` is what the next question of the
    same lifecycle compares against."""
    if not rules:
        return None
    phase = question.get("phase_id")
    spec = rules.get(phase)
    if not spec:
        return None
    text = str(question.get("observation") or "")
    if isinstance(spec.get("any_of"), list):
        for branch in spec["any_of"]:
            prev = (previous_by_rule.get(branch["rule_id"])
                    if isinstance(previous_by_rule, dict) else previous_progress)
            verdict = _evaluate_branch(phase, branch, text, prev)
            if verdict is not None:
                return verdict
        return None
    return _evaluate_branch(phase, spec, text, previous_progress)
