#!/usr/bin/env python3
"""Reconcile the probe corpus against the live meter, and NAME every part of the gap.

The sweep prescribes this every time: sum `attributes.cost` over `generation` spans, then read the
counter. Done by hand it has always "matched to the microcent" -- and on 2026-09-01 23:32, with every
probe stopped, it did not: spans $43.587947 against a counter of $43.588034, sixty-nine calls apart.
Chasing that took five commands and the answer was mundane, which is exactly why it should not be
chased again:

  * ONE preflight per probe. `run_probe.sh` opens with a 10-prompt/2-completion call that costs
    $0.00000196 and never becomes a `generation` span. Forty-four probes, forty-four calls,
    $0.000086 -- the whole of the observed gap.
  * TWENTY-FIVE killed calls, all from the five probes that ran before streaming was fixed
    (remDL +11, remEE +9, remDL2 +3, remEE2 +1, remEEctl4 +1). They were billed nothing because they
    returned nothing.

Earlier sweeps matched exactly because probes were mid-flight and their unrecorded spend was larger
than $0.000086 in the other direction. The agreement was real and the precision was luck.

So this prints the gap DECOMPOSED, and its exit code is about the RESIDUE: what is left after the
named parts are removed. A residue is the only part that was ever worth an operator's attention.

Usage:  check_money.py [--bench-root DIR] [--port N] [--max-residue USD]
"""
from __future__ import annotations

import argparse
import collections
import datetime
import glob
import json
import os
import socket
import sys


def _meter_start(port: int) -> float | None:
    """When the running meter started. Spend older than this is not in its counter."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            argv = open(f"/proc/{pid}/cmdline", "rb").read().decode("utf-8", "replace")
        except OSError:
            continue
        if "meter/proxy.py" in argv and f"--port {port}" in argv.replace("\0", " "):
            try:
                return os.stat(f"/proc/{pid}").st_mtime
            except OSError:
                return None
    return None


def _counter(port: int) -> dict:
    """Read /healthz to EOF.

    Not `recv(600)`: the sweep's own one-shot form returned headers with no body under four
    concurrent probes on 2026-09-01, and `json.loads` then died on an empty string. A short read is
    the normal case for a socket, not an error.
    """
    s = socket.socket()
    s.settimeout(8)
    s.connect(("127.0.0.1", port))
    s.sendall(b"GET /healthz HTTP/1.0\r\n\r\n")
    buf = b""
    while True:
        try:
            chunk = s.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
    return json.loads(buf.decode().split("\r\n\r\n", 1)[1])


def _ts(row: dict) -> float | None:
    st = row.get("start")
    if isinstance(st, (int, float)):
        return float(st)
    if isinstance(st, str):
        try:
            return datetime.datetime.fromisoformat(st.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def spans_by_probe(root: str, since: float) -> tuple[dict, dict]:
    cost: dict[str, float] = collections.defaultdict(float)
    calls: dict[str, int] = collections.Counter()
    for f in glob.glob(os.path.join(root, "model-probes/*/runs/*/run/spans.jsonl")):
        probe = f.split("/model-probes/")[1].split("/")[0]
        for line in open(f):
            line = line.strip()
            if not line:
                continue
            try:
                j = json.loads(line)
            except ValueError:
                continue
            c = (j.get("attributes") or {}).get("cost")
            if not isinstance(c, (int, float)):
                continue
            t = _ts(j)
            if t is None or t < since:
                continue
            cost[probe] += c
            calls[probe] += 1
    return cost, calls


def meter_by_probe(root: str, since: float) -> tuple[dict, dict, dict, dict]:
    cost: dict[str, float] = collections.defaultdict(float)
    calls: dict[str, int] = collections.Counter()
    killed: dict[str, int] = collections.Counter()
    empty: dict[str, int] = collections.Counter()
    path = os.path.join(root, "meter", "meter.jsonl")
    if not os.path.exists(path):
        # FOUR, like the signature and like every other exit from this function. It returned THREE
        # and `main` unpacks four, so the one path that is meant to be graceful -- no meter log yet,
        # a fresh BENCH_ROOT, a mistyped `--bench-root` -- was the one that died with
        # `ValueError: not enough values to unpack (expected 4, got 3)`. Found 2026-09-02 by a test
        # that pointed the tool at a root that does not exist, which no earlier test had done.
        return cost, calls, killed, empty
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        try:
            j = json.loads(line)
        except ValueError:
            continue
        try:
            t = float(j.get("ts"))
        except (TypeError, ValueError):
            continue
        if t < since:
            continue
        arm = str(j.get("arm") or "?")
        calls[arm] += 1
        try:
            cost[arm] += float(j.get("cost") or 0.0)
        except (TypeError, ValueError):
            pass
        if str(j.get("status") or "") not in ("200", ""):
            killed[arm] += 1
        else:
            # AN EMPTY 200. Found 2026-09-02 by asking what the four surplus calls this tool could
            # not name actually were: status 200, streamed, `attempts: 2`, ZERO prompt and zero
            # completion tokens, latency 60249-60764 ms. A stream that opened, produced nothing for
            # a minute, and closed successfully -- on the RETRY, so the first attempt failed the
            # same way. It is invisible to every other instrument here: not a 504, not unstreamed,
            # costs nothing, and leaves no `generation` span. Four in 12,716 calls, all in probes
            # that ran on 2026-08-31. Named so the next one is not chased again from scratch.
            try:
                if (int(j.get("prompt_tokens") or 0) == 0
                        and int(j.get("completion_tokens") or 0) == 0):
                    empty[arm] += 1
            except (TypeError, ValueError):
                pass
    return cost, calls, killed, empty


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench-root", default=os.environ.get("BENCH_ROOT", "/var/tmp/looplab-bench"))
    ap.add_argument("--port", type=int, default=8801)
    ap.add_argument("--max-residue", type=float, default=0.01,
                    help="unexplained dollars tolerated before this exits non-zero")
    # THE COUNTER IS NOT PERSISTENT ACROSS RESTARTS, so the sum has to be cut at the moment the
    # RUNNING meter started -- taken from /proc by default. `--since` states it directly, which is
    # what an operator needs after a restart whose process is already gone, and what a test needs
    # because a stub server is not a `meter/proxy.py`.
    ap.add_argument("--since", type=float, default=None,
                    help="epoch seconds; spans older than this are not in the counter "
                         "(default: when the running meter process started)")
    a = ap.parse_args(argv)

    since = a.since if a.since is not None else _meter_start(a.port)
    if since is None:
        print(f"no meter on :{a.port} -- nothing to reconcile against", file=sys.stderr)
        return 2
    # SPANS FIRST, COUNTER SECOND -- the order this file's own header prescribes, and did not obey.
    #
    # MEASURED 2026-09-02 with four probes live: residue $-0.003329, the first non-zero one of the
    # campaign, about the price of one generation. `live = _counter(...)` ran BEFORE the spans were
    # read, so a call that COMPLETED between the two reads was in the span sum and not yet in the
    # counter snapshot -- spans exceeded the meter and the gap went negative. Re-running seconds
    # later gave $-0.000000, which is the signature of a race and not of a leak.
    #
    # In this order the counter can only have GAINED between the reads, so the gap is non-negative
    # by construction and the only thing left in it is what the named parts explain. Reading the
    # spans is the slow half (a glob over every probe tree), which is exactly why it must be first.
    s_cost, s_calls = spans_by_probe(a.bench_root, since)
    m_cost, m_calls, m_killed, m_empty = meter_by_probe(a.bench_root, since)
    live = _counter(a.port)

    spans_total = sum(s_cost.values())
    gap = live["cost_usd"] - spans_total
    probes = sorted(set(s_calls) | set(k for k in m_calls if k != "?"))
    surplus = {p: m_calls.get(p, 0) - s_calls.get(p, 0) for p in probes}
    preflight = sum(1 for p in probes if surplus.get(p, 0) >= 1)
    extra = {p: n - 1 for p, n in surplus.items() if n > 1}

    print(f"meter   ${live['cost_usd']:.6f}  over {live['calls']} calls")
    print(f"spans   ${spans_total:.6f}  over {sum(s_calls.values())} generations")
    print(f"gap     ${gap:+.6f}  over {live['calls'] - sum(s_calls.values())} calls")
    print(f"  named: {preflight} preflight call(s), one per probe, ~$0.000002 each")
    if extra:
        print(f"         {sum(extra.values())} further call(s) with no generation span: "
              + ", ".join(f"{p}+{n}" for p, n in sorted(extra.items())))
        k = sum(m_killed.get(p, 0) for p in extra)
        e = sum(m_empty.get(p, 0) for p in extra)
        print(f"         of those: {k} killed by the gateway, {e} EMPTY 200s "
              f"(streamed, zero tokens both ways, ~60 s)")
        unnamed = sum(extra.values()) - k - e
        if unnamed:
            print(f"         {unnamed} call(s) STILL UNNAMED -- neither killed nor empty")
    residue = gap - preflight * 0.00000196
    print(f"  RESIDUE ${residue:+.6f} after the named parts")
    if abs(residue) > a.max_residue:
        print(f"UNEXPLAINED: ${residue:+.6f} exceeds --max-residue ${a.max_residue:.4f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
