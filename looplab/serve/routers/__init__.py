"""Route modules for the UI server (BACKLOG §4: the split of `server.py::make_app`).

Each module exposes `build_router(srv: AppState) -> APIRouter`; handlers are verbatim moves of the
former `make_app` closures, now closing over `srv` instead of `make_app`'s locals. `make_app`
(still the sole public factory, in `serve/server.py`) includes these routers in an order-documented
list — registration order is load-bearing for the overlapping patterns (`GET /api/{kind}` and the
SPA catch-all must come last)."""
