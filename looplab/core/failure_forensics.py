"""FAILURE FORENSICS: what a failed evaluation actually died of, read off its own output.

WHY (audit 2026-10-14, L-07, incidents C1 and G0). `node_failed.error` is the repair-prompt tail:
the LAST traceback, which in a wrapped pipeline is the WRAPPER's. C1 recorded only
`AssertionError('Stage failed', …)` while the real `FileNotFoundError: …/compact_pieces.json` sat in
the child's log; G0 recorded `AssertionError: ('G0', 'stage1', 1)` while several torchrun ranks had
died of SIGSEGV — no signal, no ranks, no exit codes anywhere in the record. Diagnosing either took
the agent's own scripts and the S3 copy of the child log.

WHAT. `failure_forensics(...)` is a pure, deterministic parser over text the evaluation already
produced (the captured streams and the stage logs' tails) that extracts, without any model:

  * every Python exception line in the order it was RAISED, the first non-wrapper one as
    `root_exception` (a wrapper is `ChildFailedError`, `CalledProcessError`, `SystemExit`, or an
    exception whose traceback chain says it was raised while handling another);
  * process deaths by SIGNAL: a negative exit code, torchrun's per-rank failure blocks
    (`rank : 3 (local_rank: 3)` / `exitcode : -11` / `Signal 11 (SIGSEGV)`), `subprocess`'s
    "died with <Signals.SIGSEGV: 11>", a shell's "Segmentation fault" / "Killed";
  * exit codes per stage and per rank, and the last lines each `[rankN]:`-prefixed rank printed;
  * a `faulthandler` dump's first lines ("Fatal Python error: Segmentation fault" and the stack);
  * a one-line `headline` naming the most specific cause found.

It is a RECORD, never a verdict: nothing that decides (triage, repair, selection) reads it; it rides
on `node_failed` / `node_repaired` as `failure_forensics` and is shown in the UI, the harness
progress and the agent's `node_failed` event. The caller redacts every string (`redact`).
"""
from __future__ import annotations

import re
import signal as _signal
from typing import Callable, Iterable, Optional

_TAIL_LINES = 30
_RANK_TAIL_LINES = 6
_MAX_EXCEPTIONS = 12
_MAX_RANKS = 64
_MAX_LINE = 400

WRAPPER_TYPES = frozenset({"ChildFailedError", "CalledProcessError", "SystemExit",
                           "KeyboardInterrupt"})
_CHAIN_MARKERS = ("During handling of the above exception, another exception occurred",
                  "The above exception was the direct cause of the following exception")
_RANK_PREFIX = re.compile(r"^\[(?:default)?rank(\d+)\]:\s?")
_EXC_LINE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Failure|Fault|"
                       r"Warning|Refused|Exceeded|Unavailable)|[A-Za-z_][\w.]*\.[A-Z]\w*)"
                       r"(?::\s?(.*))?$")
_TORCHRUN_RANK = re.compile(r"^\s*rank\s*:\s*(\d+)(?:\s*\(local_rank:\s*(\d+)\))?")
_TORCHRUN_EXIT = re.compile(r"^\s*exitcode\s*:\s*(-?\d+)(?:\s*\(pid:\s*(\d+)\))?")
_TORCHRUN_TB = re.compile(r"^\s*traceback\s*:\s*(.*)$")
_SIGNAL_TEXT = re.compile(r"Signal\s+(\d+)\s+\((SIG[A-Z0-9]+)\)")
_SIGNALS_ENUM = re.compile(r"<Signals\.(SIG[A-Z0-9]+):\s*(\d+)>")
_SHELL_SIGNALS = (("Segmentation fault", "SIGSEGV"), ("Bus error", "SIGBUS"),
                  ("Illegal instruction", "SIGILL"), ("Aborted (core dumped)", "SIGABRT"),
                  ("Floating point exception", "SIGFPE"))
_KILLED = re.compile(r"(^|\s)Killed\s*$")
_FAULTHANDLER = re.compile(r"^Fatal Python error: (.+)$")
_OOM = re.compile(r"CUDA out of memory|OutOfMemoryError|std::bad_alloc|Cannot allocate memory")
_NCCL = re.compile(r"NCCL (?:error|WARN)|ncclInternalError|ncclSystemError|ProcessGroupNCCL")


def signal_name(code) -> Optional[str]:
    """`SIGSEGV` for -11 (a Python child) or 139 (a shell's 128+11); None otherwise."""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return None
    num = -code if code < 0 else code - 128 if 128 < code < 160 else None
    if num is None:
        return None
    try:
        return _signal.Signals(num).name
    except ValueError:
        return f"SIG{num}"


def _strip_rank(line: str) -> tuple[Optional[int], str]:
    m = _RANK_PREFIX.match(line)
    if m:
        return int(m.group(1)), line[m.end():]
    return None, line


def _clip(text: str, n: int = _MAX_LINE) -> str:
    text = text.rstrip()
    return text if len(text) <= n else text[: n - 1] + "…"


def parse_exceptions(text: str, *, source: str) -> list[dict]:
    """Python exception lines in raise order, each `{type, message, source, rank, chained}`.

    `chained` marks an exception raised WHILE HANDLING (or directly caused by) the previous one in
    the same rank's stream — the wrapper side of a chain."""
    out = []
    in_tb: dict = {}
    chained_next: dict = {}
    for raw in (text or "").splitlines():
        rank, line = _strip_rank(raw)
        key = rank if rank is not None else -1
        stripped = line.strip()
        if stripped.startswith("Traceback (most recent call last)"):
            in_tb[key] = True
            continue
        if any(marker in stripped for marker in _CHAIN_MARKERS):
            chained_next[key] = True
            continue
        if in_tb.get(key):
            if not line[:1].isspace() and stripped:
                m = _EXC_LINE.match(stripped)
                in_tb[key] = False
                if m:
                    out.append({"type": m.group(1).rsplit(".", 1)[-1],
                                "qualified_type": m.group(1),
                                "message": _clip(m.group(2) or ""), "source": source,
                                "rank": rank, "chained": bool(chained_next.pop(key, False))})
    return out


def parse_torchrun(text: str) -> list[dict]:
    """torchrun's ChildFailedError summary: `[{rank, local_rank, exitcode, pid, signal, root}]`."""
    rows: list[dict] = []
    current: Optional[dict] = None
    root_section = False
    for raw in (text or "").splitlines():
        _, line = _strip_rank(raw)
        if line.strip().startswith("Root Cause"):
            root_section = True
            continue
        m = _TORCHRUN_RANK.match(line)
        if m:
            current = {"rank": int(m.group(1)),
                       "local_rank": int(m.group(2)) if m.group(2) else None,
                       "exitcode": None, "pid": None, "signal": None, "root_cause": root_section,
                       "source": "torchrun"}
            rows.append(current)
            continue
        if current is None:
            continue
        m = _TORCHRUN_EXIT.match(line)
        if m:
            current["exitcode"] = int(m.group(1))
            current["pid"] = int(m.group(2)) if m.group(2) else None
            current["signal"] = current["signal"] or signal_name(current["exitcode"])
            continue
        m = _TORCHRUN_TB.match(line)
        if m:
            sig = _SIGNAL_TEXT.search(m.group(1))
            if sig:
                current["signal"] = sig.group(2)
            elif m.group(1).strip() and "N/A" not in m.group(1):
                current["traceback"] = _clip(m.group(1), 200)
    # One row per rank (the root-cause section repeats a rank listed above).
    merged: dict[int, dict] = {}
    for row in rows:
        prev = merged.get(row["rank"])
        merged[row["rank"]] = {**(prev or {}), **{k: v for k, v in row.items() if v is not None},
                               "root_cause": bool(row["root_cause"] or (prev or {}).get(
                                   "root_cause"))}
    return sorted(merged.values(), key=lambda r: r["rank"])[:_MAX_RANKS]


def _other_signals(text: str, source: str) -> list[dict]:
    out = []
    for m in _SIGNALS_ENUM.finditer(text or ""):
        out.append({"signal": m.group(1), "source": f"{source}: subprocess"})
    for line in (text or "").splitlines():
        _, line = _strip_rank(line)
        for needle, name in _SHELL_SIGNALS:
            if needle in line:
                out.append({"signal": name, "source": f"{source}: {_clip(line.strip(), 120)}"})
        if _KILLED.search(line):
            out.append({"signal": "SIGKILL", "source": f"{source}: Killed (often the OOM killer)"})
    return out


def _faulthandler(text: str) -> Optional[dict]:
    lines = (text or "").splitlines()
    for i, raw in enumerate(lines):
        _, line = _strip_rank(raw)
        m = _FAULTHANDLER.match(line.strip())
        if m:
            return {"error": _clip(m.group(1), 200),
                    "stack": [_clip(_strip_rank(x)[1], 200) for x in lines[i + 1:i + 16]
                              if x.strip()]}
    return None


def _rank_tails(text: str) -> dict:
    ranks: dict[int, list[str]] = {}
    for raw in (text or "").splitlines():
        rank, line = _strip_rank(raw)
        if rank is None:
            continue
        bucket = ranks.setdefault(rank, [])
        bucket.append(_clip(line, 240))
        if len(bucket) > _RANK_TAIL_LINES:
            del bucket[0]
    return {f"rank {r}": lines for r, lines in sorted(ranks.items())[:_MAX_RANKS]}


def failure_forensics(*, exit_code=None, timed_out: bool = False, stdout: str = "",
                      stderr: str = "", logs: Optional[dict] = None,
                      failed_stage: Optional[str] = None, stage_rows: Iterable = (),
                      redact: Callable[[str], str] = lambda s: s) -> dict:
    """The forensic record of ONE failed attempt. `logs` maps a log name to its tail text."""
    sources = [("stderr", stderr or ""), ("stdout", stdout or "")]
    sources += [(name, text or "") for name, text in (logs or {}).items()]
    exceptions: list[dict] = []
    signals: list[dict] = []
    ranks: list[dict] = []
    fault = None
    hints: set[str] = set()
    rank_tails: dict = {}
    for name, text in sources:
        if not text:
            continue
        exceptions += parse_exceptions(text, source=name)
        for row in parse_torchrun(text):
            if not any(r["rank"] == row["rank"] for r in ranks):
                ranks.append(row)
        signals += _other_signals(text, name)
        fault = fault or _faulthandler(text)
        if _OOM.search(text):
            hints.add("out_of_memory")
        if _NCCL.search(text):
            hints.add("nccl")
        for key, lines in _rank_tails(text).items():
            rank_tails.setdefault(key, lines)
    seen = set()
    unique = []
    for exc in exceptions:
        ident = (exc["type"], exc["message"], exc["rank"])
        if ident not in seen:
            seen.add(ident)
            unique.append(exc)
    exceptions = unique[:_MAX_EXCEPTIONS]
    root = next((e for e in exceptions
                 if e["type"] not in WRAPPER_TYPES and not e["chained"]), None) or (
        exceptions[0] if exceptions else None)
    exit_codes: dict = {}
    for row in stage_rows or ():
        if isinstance(row, dict) and row.get("name") is not None and row.get("exit_code") is not None:
            exit_codes[f"stage {row['name']}"] = row.get("exit_code")
    for row in ranks:
        if row.get("exitcode") is not None:
            exit_codes[f"rank {row['rank']}"] = row["exitcode"]
    own_signal = signal_name(exit_code)
    if own_signal:
        signals.insert(0, {"signal": own_signal, "source": f"exit code {exit_code}"})
    rank_signals = [r for r in ranks if r.get("signal")]
    if rank_signals:
        by_sig: dict[str, list[int]] = {}
        for r in rank_signals:
            by_sig.setdefault(r["signal"], []).append(r["rank"])
        for sig, rs in by_sig.items():
            signals.append({"signal": sig, "ranks": rs, "source": "torchrun"})
    sig_seen = set()
    sig_unique = []
    for s in signals:
        ident = (s["signal"], tuple(s.get("ranks") or ()), s.get("source"))
        if ident not in sig_seen:
            sig_seen.add(ident)
            sig_unique.append(s)
    signals = sig_unique[:16]

    parts = []
    if timed_out:
        parts.append("timed out")
    for s in signals:
        if s.get("ranks"):
            parts.append(f"{s['signal']} on rank(s) {','.join(map(str, s['ranks'][:16]))}")
            break
    else:
        if signals:
            parts.append(signals[0]["signal"])
    if root is not None:
        parts.append(f"{root['type']}: {root['message']}".rstrip(": "))
    if fault is not None and not any("Fatal Python" in p for p in parts):
        parts.append(f"Fatal Python error: {fault['error']}")
    if "out_of_memory" in hints:
        parts.append("out of memory")
    wrapper = next((e for e in reversed(exceptions) if e is not root), None)
    if wrapper is not None and root is not None:
        parts.append(f"(wrapper: {wrapper['type']})")
    if not parts:
        parts.append(f"exit code {exit_code}" if exit_code not in (None, 0) else "no metric")
    if failed_stage:
        parts.insert(0, f"stage {failed_stage}:")
    tails = {}
    for name, text in (logs or {}).items():
        lines = [line for line in (text or "").splitlines() if line.strip()]
        if lines:
            tails[name] = [_clip(line, 240) for line in lines[-_TAIL_LINES:]]

    def _r(value):
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, list):
            return [_r(v) for v in value]
        if isinstance(value, dict):
            return {k: _r(v) for k, v in value.items()}
        return value

    record = {
        "version": 1,
        "headline": _clip(" ".join(parts), 300),
        "exit_code": exit_code, "timed_out": bool(timed_out),
        "failed_stage": failed_stage,
        "root_exception": ({k: root[k] for k in ("type", "message", "source", "rank")}
                           if root else None),
        "exceptions": [{k: e[k] for k in ("type", "message", "source", "rank", "chained")}
                       for e in exceptions],
        "signals": signals, "exit_codes": exit_codes, "ranks": ranks,
        "faulthandler": fault, "hints": sorted(hints),
        "rank_tails": rank_tails, "log_tails": tails,
    }
    return _bounded(_r(record))


# The whole record must stay a small fraction of an event row: a 64-rank SIGSEGV with long tails
# would otherwise write ~100 KB per failed attempt into `events.jsonl`.
MAX_RECORD_CHARS = 24_000


def _bounded(record: dict) -> dict:
    import json

    def size():
        return len(json.dumps(record, ensure_ascii=False, default=str))

    if size() <= MAX_RECORD_CHARS:
        return record
    record["rank_tails"] = dict(list(record["rank_tails"].items())[:8])
    for key in record["rank_tails"]:
        record["rank_tails"][key] = record["rank_tails"][key][-3:]
    if size() <= MAX_RECORD_CHARS:
        return record
    for name in list(record["log_tails"]):
        record["log_tails"][name] = record["log_tails"][name][-10:]
    if size() <= MAX_RECORD_CHARS:
        return record
    record["rank_tails"] = {}
    record["ranks"] = record["ranks"][:16]
    record["log_tails"] = {k: v[-5:] for k, v in list(record["log_tails"].items())[:2]}
    record["truncated"] = True
    return record
