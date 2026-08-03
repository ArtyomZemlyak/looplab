"""Process execution: sandboxes, command evaluation, environment preparation.

That sentence is now TRUE of every module here (doc 25 RA-08). Three that had landed by history
rather than by concern moved out: the proxy scorer (a pure search policy over folded `RunState`) to
`search/`, the champion-notebook renderer to `events/` beside the other exporters, and the
jupyter-server-proxy launch spec to `serve/`. With the proxy went the package's ONLY import of
`events` — which existed solely to reach a math function, now `core/numeric.py` (doc 25 XP-12) — so
`runtime` depends on nothing above `core`.
"""
