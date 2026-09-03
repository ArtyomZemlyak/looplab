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
import time
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


def _inflight_call_cost(root: str, since: float) -> float:
    """What ONE call in flight may cost: the p99 of the ledger's own prices.

    Not the median. The residue that prompted this was $0.019402 over three unnamed calls -- about
    $0.0065 each, between the corpus p90 ($0.00573) and p99 ($0.01155). A call caught mid-flight is
    not a typical call; it is whichever one happened to be running, and the expensive ones run
    longest and so are likeliest to be caught. Median (3 x $0.0028 = $0.0085) and mean
    (3 x $0.0033 = $0.0100) both fail to cover the case this was written for; p99 does, at
    3 x $0.0116 = $0.0347, and still leaves a real leak nowhere to hide -- a $0.75 gap with no
    unnamed calls has an allowance of one cent.
    """
    costs = []
    try:
        fh = open(os.path.join(root, "meter", "meter.jsonl"), encoding="utf-8", errors="replace")
    except OSError:
        return 0.0
    with fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
                if float(row.get("ts")) < since:
                    continue
                c = float(row.get("cost") or 0.0)
            except (ValueError, TypeError):
                continue
            if c > 0:
                costs.append(c)
    if not costs:
        return 0.0
    costs.sort()
    return costs[min(len(costs) - 1, int(0.99 * len(costs)))]


def unstreamed_exposure(ledger_path: str, since: float = 0.0) -> dict:
    """How many calls went out WITHOUT streaming, and how many of those nginx cut at 300 s.

    WHY THIS IS NOT A SETTLED QUESTION. Every probe is launched with `LOOPLAB_LLM_STREAM=1` and its
    INSTRUMENT.txt records it, and the standing brief reads "without streaming 28 % of discrete_log
    calls died five minutes at a time; with streaming, 0 of 28". That is true of the SETTING and not
    of the traffic: `core/llm.py:1629` degrades a call to non-streaming after a mid-stream stall
    (`use_stream = self.stream and self._stream_stalls < STREAM_STALL_DEGRADE_AFTER`), and the
    unstreamed retry is precisely what nginx's `proxy_read_timeout` measures end to end.

    MEASURED 2026-09-03: 1,201 of 26,770 ledger rows (4.5 %) went out unstreamed, 111 of them today
    under `LOOPLAB_LLM_STREAM=1`, and TWO of `oldCK9`'s 42 were cut at exactly 300.0 s. Streaming
    resumed afterwards -- 227 of the 269 calls after its first unstreamed one were streamed again --
    so this is the per-call fallback, not the client-lifetime disable in the same comment.
    """
    total = unstreamed = ceiling = 0
    by_arm: collections.Counter = collections.Counter()
    ceiling_arm: collections.Counter = collections.Counter()
    try:
        fh = open(ledger_path, encoding="utf-8", errors="replace")
    except OSError:
        return {"total": 0, "unstreamed": 0, "ceiling": 0,
                "by_arm": collections.Counter(), "ceiling_arm": collections.Counter()}
    with fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
                if float(row.get("ts")) < since:
                    continue
            except (ValueError, TypeError):
                continue
            total += 1
            if not row.get("stream"):
                unstreamed += 1
                by_arm[str(row.get("arm") or "?")] += 1
                if _failure_kind(row) == "nginx-300s":
                    ceiling += 1
                    ceiling_arm[str(row.get("arm") or "?")] += 1
    return {"total": total, "unstreamed": unstreamed, "ceiling": ceiling,
            "by_arm": by_arm, "ceiling_arm": ceiling_arm}


def endpoint_health(ledger_path: str, since: float = 0.0) -> dict:
    """The NEWEST ledger row per arm, so "is the endpoint answering right now" is one command.

    WHY. On 2026-09-03 at 16:33:19-16:33:21 four requests came back **401** — an auth status, the
    one failure where "transient" is the dangerous assumption, because a genuinely expired
    credential kills every probe at once. They were four DISTINCT requests (four `req_sha`, 15 ms
    apart, §122) on the two arms that happened to be calling, and 200s resumed forty seconds later.
    Establishing that took eyeballing the tail of the ledger by hand. This makes it a line.

    Returns `{arm: (ts, status)}` for the newest row of each arm plus `refusing`, the arms whose
    newest row is not a 200 — if that is EVERY live arm, the endpoint is down and the residue is
    not the interesting number.
    """
    newest: dict[str, tuple[float, str]] = {}
    try:
        fh = open(ledger_path, encoding="utf-8", errors="replace")
    except OSError:
        return {"newest": {}, "refusing": []}
    with fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
                ts = float(row.get("ts"))
            except (ValueError, TypeError):
                continue
            if ts < since:
                continue
            arm = str(row.get("arm") or "?")
            if arm not in newest or ts > newest[arm][0]:
                newest[arm] = (ts, str(row.get("status") or "?"))
    refusing = sorted(a for a, (_t, st) in newest.items() if st not in ("200", ""))
    return {"newest": newest, "refusing": refusing}


def _failure_kind(row) -> str:
    """Which failure a non-200 ledger row is, by its own signature.

    `nginx-300s` is the one that matters and the one that is OURS: an UNSTREAMED request whose
    latency is the proxy_read_timeout to the millisecond. `upstream-503` is the gateway declining;
    the engine retries and the run continues. Anything else is reported by its status rather than
    guessed at.
    """
    status = str(row.get("status") or "?")
    try:
        latency = float(row.get("latency_ms") or 0.0) / 1000.0
    except (TypeError, ValueError):
        latency = 0.0
    streamed = bool(row.get("stream"))
    if status == "504" and not streamed and 295.0 <= latency <= 305.0:
        return "nginx-300s"
    if status == "503":
        return "upstream-503"
    return f"http-{status}"


def meter_by_probe(root: str, since: float) -> tuple[dict, dict, dict, dict]:
    cost: dict[str, float] = collections.defaultdict(float)
    calls: dict[str, int] = collections.Counter()
    killed: dict[str, int] = collections.Counter()
    status_kind: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    empty_queued: dict[str, int] = collections.Counter()
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
            # NOT ALL NON-200s ARE THE SAME FAILURE, and calling them all "killed by the gateway"
            # cost a sweep on 2026-09-03: six new non-200s appeared inside eight minutes and read
            # as the unstreamed-era catastrophe. They were 503s refused in 1.3-15.3 s with
            # streaming ON, from which the engine recovered; the 21 real kills are 504s at exactly
            # 300.0 s with `stream=False`, all from 2026-08-31. One line, two different worlds.
            killed[arm] += 1
            status_kind[arm][_failure_kind(j)] += 1
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
                    # THE MINUTE IS OURS, NOT THE STREAM'S. Measured 2026-09-03 over 26,004 ledger
                    # rows: 39 requests waited in THIS proxy's own RPM queue (`RateLimiter.acquire`,
                    # a 60 s sliding window at --rpm 45) and 23 of them came back an empty 200 --
                    # 59 %, against 0.0154 % of the 25,968 that did not wait. 23 of the 27 empty
                    # 200s in the whole corpus had queued. The old wording, "~60 s", read as a
                    # stream that hung for a minute; the minute is the queue in front of it.
                    try:
                        if float(j.get("queued_s") or 0.0) > 0.5:
                            empty_queued[arm] += 1
                    except (TypeError, ValueError):
                        pass
            except (TypeError, ValueError):
                pass
    killed["__by_kind__"] = dict(status_kind)
    empty["__queued__"] = dict(empty_queued)
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
    # AN ABANDONED ARM IS SUBTRACTED WHOLE, SO IT MUST NOT ALSO BE DECOMPOSED.
    # Measured 2026-09-03: two service calls to `svcCacheCheck` (a live cache test, no probe tree)
    # put the residue at $-0.000002 and printed "1 call STILL UNNAMED" on an otherwise clean sweep.
    # Both came from counting the same arm twice -- once here, as a probe owed a preflight call and
    # some unexplained extras, and once below, where its ENTIRE cost is removed. The dollar error is
    # one preflight estimate per abandoned arm and the attention error is a red line on a clean
    # ledger, which is the more expensive of the two (§112).
    abandoned = {p: c for p, c in m_cost.items()
                 if p != "?" and p not in s_calls and m_calls.get(p, 0) > 0}
    probes = sorted((set(s_calls) | set(k for k in m_calls if k != "?")) - set(abandoned))
    surplus = {p: m_calls.get(p, 0) - s_calls.get(p, 0) for p in probes}
    preflight = sum(1 for p in probes if surplus.get(p, 0) >= 1)
    extra = {p: n - 1 for p, n in surplus.items() if n > 1}

    print(f"meter   ${live['cost_usd']:.6f}  over {live['calls']} calls")
    print(f"spans   ${spans_total:.6f}  over {sum(s_calls.values())} generations")
    print(f"gap     ${gap:+.6f}  over {live['calls'] - sum(s_calls.values())} calls")
    unnamed_calls = 0
    print(f"  named: {preflight} preflight call(s), one per probe, ~$0.000002 each")
    if extra:
        print(f"         {sum(extra.values())} further call(s) with no generation span: "
              + ", ".join(f"{p}+{n}" for p, n in sorted(extra.items())))
        by_kind = m_killed.get("__by_kind__") or {}
        k = sum(m_killed.get(p, 0) for p in extra)
        kinds: collections.Counter = collections.Counter()
        for p in extra:
            kinds.update(by_kind.get(p) or {})
        e = sum(m_empty.get(p, 0) for p in extra if p != "__queued__")
        eq = sum((m_empty.get("__queued__") or {}).get(p, 0) for p in extra)
        named = ", ".join(f"{n} {kind}" for kind, n in sorted(kinds.items())) or "none"
        print(f"         of those: {k} non-200 ({named}), {e} EMPTY 200s "
              f"(streamed, zero tokens both ways"
              + (f"; {eq} of them after a >0.5 s wait in THIS proxy's RPM queue)" if eq
                 else ", none of them queued)"))
        unnamed = sum(extra.values()) - k - e
        unnamed_calls = unnamed
        if unnamed:
            print(f"         {unnamed} call(s) STILL UNNAMED -- neither killed nor empty")
    # AN ABANDONED PROBE IS ITS OWN CATEGORY, not an unexplained dollar.
    #
    # The standing brief carries this as a manual step -- "the counter also counts the ABANDONED
    # probe remDL ($0.1292); at reconciliation time you must add it to the live sum or you get a
    # false discrepancy" -- and a step an operator must remember is a step that gets forgotten.
    # Driven 2026-09-02: four probes were launched, found to be mis-designed three minutes in,
    # stopped and their trees removed. The meter kept their 78 calls and $0.057925, this tool
    # reported it as UNEXPLAINED and exited 1, and the money was never in doubt for a second.
    #
    # The signature is unambiguous and needs no list to maintain: an arm the METER knows and the
    # probe trees do not. A live probe always has a tree (`run_probe.sh` writes INSTRUMENT.txt
    # before the first call), so "meter rows, no tree" cannot be a running probe -- it is a probe
    # whose tree was deleted, i.e. one abandoned.
    if abandoned:
        print(f"         {sum(m_calls[p] for p in abandoned)} call(s) from "
              f"{len(abandoned)} ABANDONED probe(s) -- in the meter, no tree on disk: "
              + ", ".join(f"{p} ${c:.4f}" for p, c in sorted(abandoned.items())))
    health = endpoint_health(os.path.join(a.bench_root, "meter", "meter.jsonl"), since)
    if health["newest"]:
        newest_ts = max(t for t, _s in health["newest"].values())
        age = max(0.0, time.time() - newest_ts)
        # THE AGE IS NOT DECORATION. Driven the minute this line was added: `oldCK8b` showed as
        # "last call refused" for two and a half minutes while it was perfectly healthy -- it had
        # taken a 401, then gone into a node evaluation, which makes NO LLM calls for ~40 s at a
        # time. A refusal 3 s old and a refusal 150 s old are different facts and the bare name
        # cannot tell them apart.
        bad = [(x, time.time() - health["newest"][x][0], health["newest"][x][1])
               for x in health["refusing"] if x != "?"]
        print(f"  endpoint: newest ledger row {age:.0f} s ago; "
              + (("arms whose LAST call was refused: "
                  + ", ".join(f"{a} ({st}, {ag:.0f} s ago)" for a, ag, st in bad)) if bad
                 else "every arm's last call was a 200"))
    calls_by_arm = m_calls
    ex = unstreamed_exposure(os.path.join(a.bench_root, "meter", "meter.jsonl"), since)
    if ex["total"]:
        print(f"  streaming: {ex['unstreamed']} of {ex['total']} calls went out UNSTREAMED "
              f"({100 * ex['unstreamed'] / ex['total']:.1f} %)"
              + (f" -- {ex['ceiling']} of them cut at the 300 s nginx ceiling ("
                 + ", ".join(f"{a} x{n}" for a, n in ex["ceiling_arm"].most_common()) + ")"
                 if ex["ceiling"] else "; none cut at the 300 s ceiling"))
        # WHOSE, NOT HOW MANY. All four ceiling deaths of 2026-09-03 were `oldCK9`, whose 58 of 301
        # calls (19.3 %) went out unstreamed against 0.7 %, 1.8 % and 4.0 % for the three probes
        # launched beside it. A count hides a concentration, and a concentration is a different
        # fact: one run losing twenty minutes, not four runs losing five.
        worst = [f"{a} {n}/{by} ({100 * n / by:.0f} %)"
                 for a, n in ex["by_arm"].most_common(3)
                 for by in (calls_by_arm.get(a, 0) or 0,) if by]
        if worst:
            print(f"    unstreamed by arm: {', '.join(worst)}")
    residue = gap - preflight * 0.00000196 - sum(abandoned.values())
    print(f"  RESIDUE ${residue:+.6f} after the named parts")
    # A CALL IN FLIGHT IS NOT A LEAK. The meter writes its row when the upstream request completes;
    # the `generation` span is written by the engine afterwards. This tool reads the spans first and
    # the counter second, so any call that lands in between is in the counter and not in the spans,
    # and the residue is POSITIVE by its price. Measured 2026-09-03 with four probes live: residue
    # $+0.019402 with `3 call(s) STILL UNNAMED` beside it, and $+0.000000 on each of the next three
    # runs seconds later. Three calls at the corpus median price is $0.019 -- the residue WAS the
    # unnamed calls. So the tolerance is the unnamed count times the median call, not a flat cent:
    # a leak with no calls in flight still fires, and a live campaign stops crying wolf.
    inflight = _inflight_call_cost(a.bench_root, since) * max(0, unnamed_calls)
    allowance = max(a.max_residue, inflight)
    if inflight > a.max_residue:
        print(f"  (allowing ${inflight:.6f}: {unnamed_calls} unnamed call(s) at the p99 price -- "
              f"spans the engine has not written yet)")
    if abs(residue) > allowance:
        print(f"UNEXPLAINED: ${residue:+.6f} exceeds ${allowance:.6f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
