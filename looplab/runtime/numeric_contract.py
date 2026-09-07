"""A stage's declared NUMERIC relation, evaluated by the engine — `expect.numeric` (doc 52 row 24).

`expect.assert` is one line in the declarer's own words, judged by an LLM against what the stage
printed; that half is only as good as the judge and the tail it reads. Some contracts are a NUMBER a
stage prints against a bound the declarer states — CapCode's cap (`params <= 2e6`), Arbor's margin
(`val_ndcg >= 0.71`), a coverage floor (`hard_neg_queries >= 688208`) — and a number needs no judge.
`expect.numeric` is that form: a bounded list of `{key, op, value}` relations the ENGINE evaluates,
after the stage exits 0 and before the next stage runs, against the LAST value the stage printed for
each key. It fails the stage exactly as `expect.files` does (`expect_failed`, same early return, same
repair loop), and it fails CLOSED: a key the stage never printed is an unmet relation, because a
declared bound about a value nobody reported is not satisfied.

Parsing is the log tools' own three spellings (`key: v`, `key=v`, `'key': v`, case-insensitive) plus
a JSON line carrying the key; the LAST occurrence wins, which is the end-of-stage summary every
trainer prints. `nan`/`inf` are not numbers here: a relation over a non-finite value is unmet.

Imports nothing above `core`, like the rest of `runtime`.
"""
from __future__ import annotations

import json
import math
import re
from typing import Optional

NUMERIC_OPS: tuple[str, ...] = ("<", "<=", ">", ">=", "==", "!=")
MAX_STAGE_NUMERIC_RELATIONS = 8
MAX_NUMERIC_KEY_CHARS = 64
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.@/\-]*$")
_NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"


def validate_numeric(nm: str, relations) -> tuple[Optional[list], Optional[str]]:
    """`expect.numeric` into its canonical list — `([{key, op, value}, …], None)` or `(None, why)`.
    Every refusal names the fix, because `declare_stages` bounces it straight back to the declarer."""
    if not isinstance(relations, list):
        return None, (f"stage {nm!r} `expect.numeric` must be a list like "
                      "[{\"key\":\"params\",\"op\":\"<=\",\"value\":2000000}].")
    if len(relations) > MAX_STAGE_NUMERIC_RELATIONS:
        return None, (f"stage {nm!r} `expect.numeric` declares {len(relations)} relations; at most "
                      f"{MAX_STAGE_NUMERIC_RELATIONS} are checked.")
    clean = []
    for i, rel in enumerate(relations):
        if not isinstance(rel, dict) or set(rel) - {"key", "op", "value"}:
            return None, (f"stage {nm!r} `expect.numeric[{i}]` must be an object with exactly "
                          "`key`, `op` and `value`.")
        key = rel.get("key")
        if (not isinstance(key, str) or not key.strip() or len(key) > MAX_NUMERIC_KEY_CHARS
                or not _KEY_RE.match(key.strip())):
            return None, (f"stage {nm!r} `expect.numeric[{i}].key` must be the name the stage prints "
                          f"the value under (letters, digits, `_ . @ / -`; at most "
                          f"{MAX_NUMERIC_KEY_CHARS} chars).")
        op = rel.get("op")
        if op not in NUMERIC_OPS:
            return None, (f"stage {nm!r} `expect.numeric[{i}].op` must be one of {list(NUMERIC_OPS)!r}.")
        value = rel.get("value")
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value))):
            return None, (f"stage {nm!r} `expect.numeric[{i}].value` must be a finite number.")
        clean.append({"key": key.strip(), "op": op, "value": float(value)})
    return clean, None


def _value_re(key: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9_])['\"]?" + re.escape(key) + r"['\"]?\s*[:=]\s*(" + _NUMBER + ")",
                      re.IGNORECASE)


def last_values(text: str, keys) -> dict:
    """`{key: last finite value the text reports for it}`; a key never reported is absent."""
    out: dict = {}
    if not text:
        return out
    wanted = list(dict.fromkeys(str(k) for k in keys))
    lowered = {k.lower(): k for k in wanted}
    patterns = {k: _value_re(k) for k in wanted}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                doc = json.loads(stripped)
            except ValueError:
                doc = None
            if isinstance(doc, dict):
                for k, v in doc.items():
                    key = lowered.get(str(k).lower())
                    if key is not None and isinstance(v, (int, float)) and not isinstance(v, bool) \
                            and math.isfinite(float(v)):
                        out[key] = float(v)
                continue
        for key, rx in patterns.items():
            for m in rx.finditer(line):
                try:
                    v = float(m.group(1))
                except ValueError:
                    continue
                if math.isfinite(v):
                    out[key] = v
    return out


def _holds(observed: float, op: str, bound: float) -> bool:
    return {"<": observed < bound, "<=": observed <= bound, ">": observed > bound,
            ">=": observed >= bound, "==": observed == bound, "!=": observed != bound}[op]


def numeric_contract_defects(text: str, relations) -> tuple[list[str], dict]:
    """The unmet relations, in declaration order, as sentences that name the value and the bound,
    beside the values that were read. A key the stage never printed is a defect."""
    keys = [r["key"] for r in relations]
    values = last_values(text, keys)
    defects = []
    for rel in relations:
        key, op, bound = rel["key"], rel["op"], float(rel["value"])
        if key not in values:
            defects.append(f"{key} {op} {bound:g} — the stage never printed {key!r}")
        elif not _holds(values[key], op, bound):
            defects.append(f"{key} {op} {bound:g} — the stage printed {key} = {values[key]:g}")
    return defects, values
