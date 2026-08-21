#!/usr/bin/env python3
"""Measure an OpenAI-compatible endpoint the way a campaign will actually use it.

Two questions decide whether an endpoint can carry a benchmark, and neither is answered by a
vendor's catalogue (docs/50 5a: published `uptime_last_30m` did not predict availability -- a
provider at 99.0 % returned 502 and one at 99.5 % hung for 300 s):

  1. **Speed**, as tokens per second on a prompt of realistic shape -- not on "say hi".
  2. **Stability under the campaign's own concurrency.** The campaign runs one lane per task; on
     this box that is up to 20 agent loops calling the same endpoint at once. An endpoint that is
     fast at concurrency 1 and rate-limits at 20 fails the campaign, not the probe.

Both arms are pinned to one model, so this is run BEFORE the campaign to choose it, and the numbers
are recorded with the campaign as the state of the endpoint at that time.

    python probe_endpoint.py --base-url URL --api-key KEY --models a,b --sequential 6 --concurrent 20
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

PROMPT = (
    "You are optimising a numerical Python routine. Write a `Solver` class with a `solve(self, "
    "problem: dict) -> list` method that computes the convex hull of the 2-D points in "
    "`problem['points']` and returns the hull vertex indices in counter-clockwise order. Use numpy. "
    "Explain your complexity in one sentence after the code."
)


def one_call(base_url: str, api_key: str, model: str, max_tokens: int, timeout: float,
             nonce: str = "") -> dict:
    """One completion. `nonce` defeats a caching gateway.

    Measured 2026-08-20 on llm-core-olap.samokat.ru: the SAME prompt at temperature 0 came back in
    0.0 s with 400 completion tokens -- 28,886 tok/s, i.e. a cache hit, not a generation. A probe
    without a nonce measures the cache and reports an endpoint that does not exist.
    """
    prompt = f"[probe {nonce}] {PROMPT}" if nonce else PROMPT
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key,
                 "Accept-Encoding": "identity"},
        method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    t0 = time.time()
    try:
        with opener.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        dt = time.time() - t0
        usage = data.get("usage") or {}
        out = int(usage.get("completion_tokens") or 0)
        return {"ok": True, "s": dt, "out": out, "in": int(usage.get("prompt_tokens") or 0),
                "tps": out / dt if dt else 0.0,
                "finish": (data.get("choices") or [{}])[0].get("finish_reason")}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "s": time.time() - t0, "err": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "s": time.time() - t0, "err": f"{type(exc).__name__}"}


def summarise(label: str, rows: list[dict]) -> dict:
    ok = [r for r in rows if r.get("ok")]
    lat = sorted(r["s"] for r in ok)
    tps = [r["tps"] for r in ok if r.get("tps")]
    errs: dict[str, int] = {}
    for r in rows:
        if not r.get("ok"):
            errs[r.get("err", "?")] = errs.get(r.get("err", "?"), 0) + 1
    return {
        "label": label, "n": len(rows), "ok": len(ok),
        "median_s": round(statistics.median(lat), 1) if lat else None,
        "min_s": round(lat[0], 1) if lat else None,
        "max_s": round(lat[-1], 1) if lat else None,
        "median_tps": round(statistics.median(tps), 1) if tps else None,
        "mean_out": round(statistics.mean([r["out"] for r in ok]), 0) if ok else None,
        "errors": errs or None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("LOOPLAB_LLM_BASE_URL", ""))
    ap.add_argument("--api-key", default=os.environ.get("LOOPLAB_LLM_API_KEY", ""))
    ap.add_argument("--models", required=True, help="comma-separated model ids")
    ap.add_argument("--sequential", type=int, default=6)
    ap.add_argument("--concurrent", type=int, default=0, help="fan-out size for the burst test")
    ap.add_argument("--max-tokens", type=int, default=400)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--json-out", default="")
    ap.add_argument("--no-nonce", action="store_true",
                    help="send the identical prompt every time -- measures the gateway CACHE")
    args = ap.parse_args()

    if not args.base_url:
        print("--base-url required", file=sys.stderr)
        return 2

    results = []
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        seq = []
        for i in range(args.sequential):
            nonce = "" if args.no_nonce else f"{model}-seq-{i}-{time.time():.6f}"
            r = one_call(args.base_url, args.api_key, model, args.max_tokens, args.timeout, nonce)
            seq.append(r)
            print(f"  {model} seq {i+1}/{args.sequential}: "
                  f"{'ok' if r['ok'] else r.get('err')} {r['s']:.1f}s "
                  f"{r.get('out', 0)} tok {r.get('tps', 0):.0f} tok/s", flush=True)
        results.append(summarise(f"{model} seq", seq))

        if args.concurrent:
            t0 = time.time()
            with ThreadPoolExecutor(args.concurrent) as ex:
                burst = list(ex.map(
                    lambda i: one_call(args.base_url, args.api_key, model, args.max_tokens,
                                       args.timeout,
                                       "" if args.no_nonce
                                       else f"{model}-burst-{i}-{time.time():.6f}"),
                    range(args.concurrent)))
            wall = time.time() - t0
            row = summarise(f"{model} burst x{args.concurrent}", burst)
            row["wall_s"] = round(wall, 1)
            results.append(row)
            print(f"  {model} burst x{args.concurrent}: {row['ok']}/{args.concurrent} ok in "
                  f"{wall:.1f}s", flush=True)

    print()
    head = f"{'case':<34}{'ok':>7}{'med s':>8}{'min':>7}{'max':>7}{'tok/s':>8}{'out':>7}  errors"
    print(head)
    print("-" * len(head))
    for r in results:
        print(f"{r['label']:<34}{r['ok']}/{r['n']:<5}{str(r['median_s']):>8}{str(r['min_s']):>7}"
              f"{str(r['max_s']):>7}{str(r['median_tps']):>8}{str(r['mean_out']):>7}  "
              f"{r['errors'] or ''}")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"base_url": args.base_url, "at": time.time(), "rows": results}, fh, indent=1)
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
