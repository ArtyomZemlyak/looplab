# The proxy's pairwise accuracy — unmeasurable here, and the risk the row named is not armed

*2026-09-18. Doc 52 row 31, marker `proxy-accuracy-never-run-on-the-corpus`. Scope: the
pre-execution scorer in `search/proxy.py`, whose `should_skip` can KILL a candidate before it is
evaluated. The instrument shipped 2026-09-07 (`search/proxy.py::pairwise_accuracy`,
`looplab proxy-accuracy <run_dir>`); this page is what it reads over the 161-probe corpus in
`runs-archive/model-probes`, and one correction to the row that asked for it.*

## 0. The reading

```
runs read: 161
runs where the proxy scored anything: 0
candidates scored (total): 0;  KILLED (proxy_skipped): 0
runs with a measurable pairwise accuracy: 0
```

Per run the instrument says what it should say rather than a number:

> proxy scored 0 candidate(s); 0 of them were evaluated and can be checked; 0 were KILLED
> (`proxy_skipped`), 3 node(s) carry no proxy score
> pairwise accuracy: NOT MEASURABLE — no pair of scored nodes came back with different metrics.
> This is not 0 %, and it is not evidence the proxy works.

## 1. The correction the row needs

The marker reads:

> the kill (`proxy_skipped`) is still armed on a scorer whose pairwise accuracy nobody has read on
> this box, which is the state the instrument exists to end

**It is not armed.** `Settings.proxy_scoring` is `False` and `Settings.proxy_kill_fraction` is
`0.0`; `search/proxy.py::should_skip` returns False on its first line when the fraction is zero;
`cli/__init__.py` builds a `ProxyScorer` only when one of the two is set; and none of the three
shipped profiles (`default`, `fast`, `thorough`) carries a proxy key at all. Checked, not assumed —
`PROFILES` was read for every profile, and the corpus confirms the consequence: not one candidate
scored in 161 runs.

So the state the row worried about — a kill deciding real evaluations on an unmeasured judge — has
never existed on this box. What is true is narrower and still worth keeping: the accuracy **cannot**
be measured until someone turns the knob on, because a scorer that never scored leaves nothing to
check.

## 2. What survives as a precondition

Whoever raises `proxy_kill_fraction` above zero owes this number FIRST, on the same runs, and the
instrument exists so that costs one command per run. Two properties of it matter when they do:

* **It is biased optimistic by construction, and says so.** A killed node has no realized metric, so
  the accuracy is computed over the candidates the proxy APPROVED. A judge that kills badly is
  judged only on what it let through.
* **"Not measurable" is not zero.** The report distinguishes the two in its own sentence, which is
  the distinction this page exists to preserve: reporting 0 % here would have read as "the proxy is
  wrong" when the truth is "the proxy never spoke".

## 3. Why it never spoke, beyond the flag

Even switched on, the abstention rule would rarely let it fire on this workload. `should_skip`
requires a warmup of evaluated metrics and refuses to kill a candidate outside the explored region
(`abstains`), and AlgoTune runs produce **2 to 4 evaluated nodes** — 21 runs of 2, 82 of 3, 16 of 4
across 119 `edge_expansion` runs, none above four (doc 56 §422). That is the same short-run regime
that made B1's quartile gate inert at a floor of four, and it is the thing to fix before believing
any loop-side mechanism that needs neighbours.

## 4. How to re-take it

```bash
for d in <runs-root>/*/run; do python -m looplab.cli proxy-accuracy "$d"; done
```

Read the first line before the second: `scored` and `KILLED` are what decide whether the accuracy on
the line below means anything.
