"""A probe that only inspects the environment is carried to later builds; one touching the repo is not.

Measured 2026-09-25 on MiniOneRec inf12: `run_probe` turns were 35% of the Developer's plan time
(188 of 534 LLM-minutes), 95 of 115 plan probes only inspected library source — the same
`inspect.getsource(GenerationMixin._beam_search)` 33 times over 4 builds.
"""
from __future__ import annotations

from looplab.agents.established import EstablishedContext

_LIB = "import inspect, transformers\nprint(inspect.getsource(transformers.GenerationMixin._beam_search)[:500])"
_OUT = "exit=0 | stdout:\ndef _beam_search(self, input_ids, ...):"


def _store(**kw):
    store = EstablishedContext(**kw)
    store.note_local_names({"service", "optimizations", "assets"})
    return store


def test_a_library_probe_is_carried_and_survives_a_new_build():
    store = _store()
    store.enter_workspace("build-1")
    hook = store.hook("plan")
    hook("run_probe", {"code": _LIB}, _OUT)
    store.enter_workspace("build-2")
    block = store.render_probes()
    assert "LIBRARY FACTS" in block and "_beam_search" in block and "run 1x" in block


def test_a_probe_naming_a_repo_module_or_reading_a_file_is_not_carried():
    store = _store()
    hook = store.hook("plan")
    hook("run_probe", {"code": "from service.latency_engine import Engine\nprint(Engine)"}, _OUT)
    hook("run_probe", {"code": "print(open('assets/x.json').read())"}, _OUT)
    hook("run_probe", {"code": "import optimizations.exp30 as m"}, _OUT)
    assert store.render_probes() == ""


def test_a_failed_probe_is_not_carried():
    store = _store()
    store.hook("plan")("run_probe", {"code": _LIB}, "exit=1 | Traceback ...")
    assert store.render_probes() == ""


def test_the_same_probe_counts_once_and_its_rerun_shows():
    store = _store()
    hook = store.hook("plan")
    hook("run_probe", {"code": _LIB}, _OUT)
    hook("run_probe", {"code": _LIB.replace("\n", "\n  ").strip()}, _OUT)
    hook("run_probe", {"code": _LIB}, _OUT)
    assert store.render_probes().count("--- probe (") == 1


def test_the_block_is_bounded_and_fenced_when_the_envelope_is_on():
    store = _store(evidence_envelope=True)
    hook = store.hook("plan")
    for i in range(40):
        hook("run_probe", {"code": f"import torch\nprint(torch.__version__, {i})"}, "exit=0 | " + "x" * 2000)
    block = store.render_probes()
    assert len(block.encode("utf-8")) <= 8192
    assert "UNTRUSTED" in block


def test_a_probe_leaves_the_file_ledger_alone():
    store = _store()
    store.hook("plan")("run_probe", {"code": _LIB}, _OUT)
    assert store.render() == "" and store.items() == []
