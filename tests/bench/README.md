# Research context CPU benchmark

Run `PYTHONPATH=. /path/to/looplab-python tests/bench/benchmark_research_context.py` from the source
root. It creates a temporary synthetic campaign, compares 15 full-state and briefing response sizes
and latencies, and times one ready-event publication, outbox read and supported briefing read using
FastAPI's in-process TestClient. GPU queue paths are redirected to a temporary fixture. This measures
neither network latency nor an agent's reaction or a real job start. Timings vary by machine; inspect
the reported values rather than treating them as a performance guarantee.
