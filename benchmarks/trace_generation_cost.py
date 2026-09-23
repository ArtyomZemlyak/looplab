#!/usr/bin/env python3
"""What ONE traced LLM generation costs its caller, and what it leaves in `spans.jsonl`.

WHY THIS FILE EXISTS. A tool loop re-sends its whole growing conversation on every turn, and with
`trace_llm_io` on `core/tracing.py::generation` records it. Review 2026-09-22 (CORE-02) measured
12-100 ms of CPU per LLM call on the caller's thread and 64 KB rows where 4 KB ones had been, and
named three causes: every persisted string re-walked `os.environ` (`core/redact.py::
secret_env_values`, uncached), the conversation was sanitized TWICE per generation (once by
`_trace_messages`, again by `SpanHandle.set`), and the delta encoding died once the conversation
outgrew the 64 000-character retention window, because the window slid and the prefix compare
failed. This instrument reproduces all three numbers on a deterministic conversation, so a fix is
held to the same ruler.

WHAT IT MEASURES, per generation of an N-turn tool loop (default 30; a ~4 KB system prompt, a
~6 KB task, then an assistant message of ~500 chars and a tool result of ~3 KB per turn — the
retention window is exceeded from turn 17 on):

  * caller-thread milliseconds, INCLUDING the row's serialization (`_span_jsonl_line`), which runs
    on the span-closing thread even under the async exporter (median of --repeat passes);
  * the generation row's bytes and its `input_carry` (0 = a full base was stored);
  * env walks (`is_secret_env` evaluations / variables per walk) and `_redact_persisted` calls per
    generation, and how many times each MESSAGE went through the redactor.

MEASURED 2026-09-23 on the review box (145 environment variables, 4 cores shared with other
jobs, so absolute milliseconds move with load; the counts do not):

    before CORE-02   ~70-92 ms/generation   146 env walks/gen   146 redactions/gen
                     rows 4.2 KB to turn 16, then 66 KB (input_carry 0 from turn 17)
                     up to 35 redactions of one message over 30 turns

Usage:
    python benchmarks/trace_generation_cost.py [ROOT] [--turns N] [--repeat R]

ROOT is the checkout whose `looplab` package is measured (default: this one), so the same file can
be pointed at an exported older tree for a before/after pair.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import sys
import time
from pathlib import Path

_WORDS = ("the model loss train eval metric epoch batch learning rate schedule warmup optimizer "
          "gradient norm clip validation split fold seed feature column target leakage baseline "
          "ensemble stack blend tokenizer embedding layer dropout weight decay checkpoint resume "
          "config yaml path file module function class method return value error trace stack "
          "retry timeout budget token cost call tool result read write patch diff test assert "
          "score ranking query document passage retrieval index cache shard worker thread process "
          "memory gpu cpu device tensor shape dtype float half precision mixed scaler step").split()


def _prose(rng: random.Random, n: int) -> str:
    out, total = [], 0
    while total < n:
        word = rng.choice(_WORDS)
        out.append(word)
        total += len(word) + 1
    return " ".join(out)[:n]


def _code(rng: random.Random, n: int, turn: int) -> str:
    """Tool output shaped like what a Developer reads: code, a traceback path, a log, a digest."""
    lines, total = [], 0
    while total < n:
        kind = rng.randrange(6)
        if kind == 0:
            line = f"    loss = criterion(outputs_{rng.randrange(99)}, targets)  # step {turn}"
        elif kind == 1:
            line = (f'  File "/home/user/work/nodes/node_{turn}/src/train_{rng.randrange(9)}.py", '
                    f"line {rng.randrange(900)}")
        elif kind == 2:
            line = f"epoch {rng.randrange(40)} loss {rng.random():.4f} val_auc {rng.random():.4f}"
        elif kind == 3:
            line = f"sha256 {rng.getrandbits(160):040x} checkpoint saved"
        else:
            line = "    " + _prose(rng, 60)
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)[:n]


def conversation(turns: int, *, seed: int = 7):
    """(opening messages, per-turn appends); every message carries a unique ` MSGMARK####X `."""
    rng = random.Random(seed)
    count = [0]

    def mark() -> str:
        count[0] += 1
        return f" MSGMARK{count[0]:04d}X "

    opening = [{"role": "system", "content": mark() + _prose(rng, 4000)},
               {"role": "user", "content": mark() + _prose(rng, 6000)}]
    appends = [({"role": "assistant", "content": mark() + _prose(rng, 500)},
                {"role": "tool", "content": mark() + _code(rng, 3000, turn)})
               for turn in range(turns)]
    return opening, appends


def measure(root: Path, turns: int, repeat: int) -> dict:
    sys.path.insert(0, str(root))
    from looplab.core import redact, tracing

    rows: list[bytes] = []

    class _Serialize:
        """The async exporter's CALLER half: `_span_jsonl_line` runs on the span-closing thread."""

        def export(self, span):
            rows.append(tracing._span_jsonl_line(span))

    def one_pass() -> list[float]:
        rows.clear()
        opening, appends = conversation(turns)
        tracer = tracing.Tracer(_Serialize(), run_id="bench", capture_llm_io=True)
        history = list(opening)                  # ONE list grown in place, as `drive_tool_loop` does
        seconds: list[float] = []
        with tracer.span("tool_loop", new_trace=True, node_id=0):
            for assistant, tool_result in appends:
                started = time.perf_counter()
                with tracing.generation(op="chat", model="m", messages=history,
                                        model_parameters={"temperature": 0.6}) as gen:
                    gen.output("ok " + assistant["content"][:80]).usage(
                        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
                    ).cost(0.001)
                seconds.append(time.perf_counter() - started)
                history.append(assistant)
                history.append(tool_result)
        return seconds

    passes = [one_pass() for _ in range(max(1, repeat))]
    per_turn = [statistics.median(p[i] for p in passes) for i in range(turns)]
    generations = [json.loads(r) for r in rows if json.loads(r).get("kind") == "generation"]
    row_bytes = [len(r) for r in rows if json.loads(r).get("kind") == "generation"]
    carries = [g["attributes"].get("input_carry") for g in generations]
    input_bytes = [len(json.dumps(g["attributes"].get("input"), ensure_ascii=False))
                   for g in generations]

    # Counting pass, with wrappers on the redact module's OWN globals (every caller reaches them).
    walks_seen, calls_seen = [0], [0]
    per_message: dict[str, int] = {}
    real_screen, real_redact = redact.is_secret_env, redact._redact_persisted
    marker = re.compile(r"MSGMARK(\d{4})X")
    # One WALK evaluates `is_secret_env` once per variable whose value clears the length floor, on
    # the old code and the new alike, so evaluations / that count = walks.
    floor = getattr(redact, "_MIN_SECRET_ENV_VALUE", 8)
    per_walk = sum(1 for value in os.environ.values() if len(value.strip()) >= floor) or 1

    def counting_screen(name, value=""):
        walks_seen[0] += 1
        return real_screen(name, value)

    def counting_redact(value, **kw):
        calls_seen[0] += 1
        if isinstance(value, str):
            for found in marker.findall(value):
                per_message[found] = per_message.get(found, 0) + 1
        return real_redact(value, **kw)

    redact.is_secret_env, redact._redact_persisted = counting_screen, counting_redact
    if hasattr(redact, "_PROCESS_ENV_SCREEN"):
        redact._PROCESS_ENV_SCREEN = None        # a COLD cache, so its one walk is counted too
    try:
        one_pass()
    finally:
        redact.is_secret_env, redact._redact_persisted = real_screen, real_redact

    def at(values, points):
        return {t: values[t - 1] for t in points if t <= turns}

    return {
        "env_vars": len(os.environ),
        "turns": turns,
        "ms_per_generation_mean": round(1e3 * statistics.mean(per_turn), 2),
        "ms_at_turn": at([round(1e3 * s, 2) for s in per_turn], (1, 10, 20, 30, turns)),
        "row_bytes_mean": round(statistics.mean(row_bytes)),
        "row_bytes_at_turn": at(row_bytes, (1, 10, 16, 17, 20, 30, turns)),
        "input_carry_at_turn": at(carries, (1, 10, 16, 17, 20, 30, turns)),
        "input_bytes_total": sum(input_bytes),
        "env_walks_per_generation": round(walks_seen[0] / per_walk / turns, 2),
        "redactions_per_generation": round(calls_seen[0] / turns, 2),
        "redactions_per_message_max": max(per_message.values(), default=0),
        "redactions_per_message_mean": round(sum(per_message.values()) / (2 + 2 * turns), 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("root", nargs="?", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--turns", type=int, default=30)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(measure(Path(args.root), args.turns, args.repeat), indent=1))


if __name__ == "__main__":
    main()
