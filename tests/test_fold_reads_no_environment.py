"""The fold is a function of the LOG, not of the box replaying it (review 2026-09-22, EVT-02).

`replay.fold` re-sanitizes every research memo, literature row and report it folds, and that pass
ran through `core/redact.py::redact_persisted_text` -> `redact_env_values`, which walks
`os.environ`. So one `events.jsonl` folded to two different `RunState`s in two processes: the one
holding a secret whose VALUE appeared in a memo masked it, the other did not. Engine invariant 5 is
that the fold is deterministic, and a replay (`looplab replay`, a resume, the UI server, a reviewer
bundle) is exactly the operation that runs in a DIFFERENT process from the one that wrote the log.

Driven both ways, because the fix moves a screen and must not lose it:

* the FOLD — the same log under two environments dumps byte-identically, with the shapeless secret
  planted in every text field the advisory sanitizers reach (memo prose, claims, source URLs, the
  verifier block, the proposed-idea tree, the research record, literature, the report);
* the WRITER — the identity screen stays where the bytes become durable: a shapeless env secret a
  memo carried is masked in the `research_completed` row on disk.
"""
from __future__ import annotations

import pytest

from looplab.core.advisory_payloads import (sanitize_literature_items,
                                            sanitize_report_payload,
                                            sanitize_research_memo_payload)
from looplab.core.models import Event, ResearchMemo
from looplab.core.redact import bounded_redacted_tree, redact_persisted_text
from looplab.core.research_record import parse_literature
from looplab.core.source_identity import canonical_source_ref
from looplab.events.replay import FoldCursor, fold
from looplab.events.types import (EV_LITERATURE_RETRIEVED, EV_REPORT_GENERATED,
                                  EV_RESEARCH_COMPLETED, EV_RUN_STARTED)

from factories import make_engine

# SHAPELESS on purpose: no provider prefix, one letter case, 21 characters — so neither `_PATTERNS`
# nor the composition-screened entropy pass touches it, and ONLY the env identity screen could.
# A shaped value would be masked by the fold either way and prove nothing about the environment.
SECRET = "hunter2hunter2hunter2"
ENV_NAME = "MY_DB_PASSWORD"
MASK = "***REDACTED_ENV***"


def _log() -> list[Event]:
    literature = {"id": "lit-" + "a" * 24, "title": f"a paper on {SECRET}",
                  "abstract_sha256": "b" * 64, "abstract_chars": 3, "query": f"q {SECRET}",
                  "tool": "arxiv_search"}
    evidence = {"id": "ev-" + "c" * 24, "kind": "web", "tool": "web_fetch",
                "locator": f"web_fetch({SECRET})", "quote": f"the page said {SECRET}",
                "sha256": "d" * 64, "bytes": 30, "turn": 1}
    memo = {
        "summary": f"try the {SECRET} tokenizer",
        "reasoning": f"because {SECRET} worked before",
        "findings": [f"finding {SECRET}"],
        "claims": [{"statement": f"claim about {SECRET}", "node_ids": [0],
                    "urls": [f"https://example.org/{SECRET}/page"]}],
        "sources": [{"title": f"source {SECRET}", "url": f"https://example.org/{SECRET}",
                     "snippet": f"snippet {SECRET}"}],
        "recommended_directions": [f"direction {SECRET}"],
        "open_questions": [f"question {SECRET}"],
        "next_experiments": [f"experiment {SECRET}"],
        "proposed_ideas": [{"rationale": f"idea {SECRET}"}],
        "verification": {"method": "rubric", "verdicts": [
            {"statement": f"claim about {SECRET}", "verdict": "supported",
             "note": f"note {SECRET}"}]},
        "plan": {"plan": f"plan {SECRET}", "todos": [{"item": f"todo {SECRET}", "status": "done"}],
                 "updates": 1},
        "evidence": [evidence],
        "literature": [literature],
        "at_node": 0,
    }
    report = {"headline": f"best used {SECRET}", "summary": f"summary {SECRET}",
              "verdict": f"verdict {SECRET}", "champion_summary": f"champion {SECRET}",
              "caveats": [f"caveat {SECRET}"], "what_worked": [f"worked {SECRET}"]}
    rows = [
        (EV_RUN_STARTED, {"run_id": "r", "task_id": "t", "direction": "max"}),
        (EV_RESEARCH_COMPLETED, {"memo": memo, "at_node": 0, "trigger": "cadence"}),
        (EV_LITERATURE_RETRIEVED, {"at_node": 0, "items": [literature]}),
        (EV_REPORT_GENERATED, {"content": report, "at_node": 0, "trigger": "cadence"}),
    ]
    return [Event(seq=i, ts=float(i + 1), type=t, data=d) for i, (t, d) in enumerate(rows)]


def test_one_log_folds_identically_whatever_the_replaying_process_holds(monkeypatch):
    """THE DEFECT, driven: before the fix the second fold masked the planted value and the two
    dumps differed. MUTATION: drop `env=_FOLD_REDACTION_ENV` from any one of the three advisory
    handlers in `replay.py` and the dumps differ again."""
    events = _log()
    monkeypatch.delenv(ENV_NAME, raising=False)
    plain = fold(events)
    monkeypatch.setenv(ENV_NAME, SECRET)
    holding = fold(events)
    assert holding.model_dump_json() == plain.model_dump_json(), (
        "the folded RunState depends on the replaying process's environment (invariant 5)")

    # NOT VACUOUS: the planted value really reaches every sanitizer path the fold runs, so there
    # was something for an environment-reading screen to change on each of them.
    memo = holding.research[0]
    reached = {
        "summary": memo["summary"],
        "claim url (canonical_source_ref)": memo["claims"][0]["urls"][0],
        "source url": memo["sources"][0]["url"],
        "verifier note (_verification)": memo["verification"]["verdicts"][0]["note"],
        "proposed idea (_tree)": memo["proposed_ideas"][0]["rationale"],
        "plan (sanitize_research_plan)": memo["plan"]["plan"],
        "evidence quote (sanitize_evidence_items)": memo["evidence"][0]["quote"],
        "literature (sanitize_literature_items)": holding.literature[0]["title"],
        "report headline (sanitize_report_payload)": holding.report["headline"],
    }
    missing = {where: text for where, text in reached.items() if SECRET not in text}
    assert not missing, f"the probe did not reach these fold paths: {missing}"


def test_the_incremental_cursor_is_environment_free_too(monkeypatch):
    """`FoldCursor` is the other entry to the same handlers (the server's live state) and is
    appended to across calls — so a cursor fed under one environment and snapshotted under another
    must still equal a fresh fold."""
    events = _log()
    monkeypatch.setenv(ENV_NAME, SECRET)
    cursor = FoldCursor()
    cursor.extend(events[:2])
    monkeypatch.delenv(ENV_NAME, raising=False)
    cursor.extend(events[2:])
    assert cursor.snapshot().model_dump_json() == fold(events).model_dump_json()


def test_the_writer_still_masks_a_shapeless_env_secret_in_the_durable_row(tmp_path, monkeypatch):
    """The screen MOVED OUT of the fold; it did not go away. The research recorder sanitizes with
    its own process's environment at the moment the bytes become durable, so the row on disk —
    which every later fold, export and bundle reads — never carries the value. MUTATION: make the
    writer pass `env={}` and the raw log carries the secret."""
    monkeypatch.setenv(ENV_NAME, SECRET)
    engine = make_engine(tmp_path / "run")
    memo = ResearchMemo(
        summary=f"try the {SECRET} tokenizer", at_node=0, trigger="cadence",
        literature=parse_literature(f"1. A paper on {SECRET}\n   An abstract.", query="q"))
    engine._record_deep_research(memo, trigger="cadence", manual=False)

    raw = (tmp_path / "run" / "events.jsonl").read_text(encoding="utf-8")
    assert SECRET not in raw, "a shapeless env secret reached the durable research rows"
    rows = [e for e in engine.store.read_all()
            if e.type in {EV_RESEARCH_COMPLETED, EV_LITERATURE_RETRIEVED}]
    assert [e.type for e in rows] == [EV_RESEARCH_COMPLETED, EV_LITERATURE_RETRIEVED]
    assert MASK in rows[0].data["memo"]["summary"]
    assert MASK in rows[1].data["items"][0]["title"]
    # ...and what a later fold shows is the writer's masked text, in any environment.
    monkeypatch.delenv(ENV_NAME, raising=False)
    assert MASK in fold(engine.store.read_all()).research[-1]["summary"]


def test_env_names_whose_secrets_are_screened_at_every_entry_point(monkeypatch):
    """The truth table of the new parameter. `None` is this process (every writer and display
    surface, unchanged); a mapping is exactly that mapping, INSTEAD of the process; `{}` is none —
    the fold's choice. Shapes stay masked in every row: only the identity screen is movable."""
    text = f"x {SECRET} y sk-{'A' * 24}"
    monkeypatch.setenv(ENV_NAME, SECRET)
    assert SECRET not in redact_persisted_text(text, max_chars=4000)
    assert SECRET in redact_persisted_text(text, max_chars=4000, env={})
    assert "sk-AAAA" not in redact_persisted_text(text, max_chars=4000, env={}), (
        "env={} must switch off the identity screen ONLY — a known credential shape stays masked")
    monkeypatch.delenv(ENV_NAME, raising=False)
    assert SECRET not in redact_persisted_text(text, max_chars=4000,
                                               env={"OTHER_TOKEN": SECRET})
    assert SECRET in redact_persisted_text(text, max_chars=4000)

    monkeypatch.setenv(ENV_NAME, SECRET)
    for env, masked in ((None, True), ({}, False)):
        found = [
            sanitize_research_memo_payload({"summary": text}, env=env)["summary"],
            sanitize_report_payload({"headline": text}, env=env)["headline"],
            sanitize_literature_items([{"id": "lit-" + "a" * 24, "title": text}],
                                      env=env)[0]["title"],
            canonical_source_ref(f"https://example.org/{SECRET}", env=env).display_url,
            repr(bounded_redacted_tree({"k": text}, [4000], [64], env=env)),
        ]
        for value in found:
            assert (SECRET not in value) is masked, (env, value)


@pytest.mark.parametrize("helper", ["_text", "_source_url", "_tree", "_verification"])
def test_the_private_sanitizers_refuse_to_default_whose_environment(helper):
    """`env` is a REQUIRED keyword on the helpers the fold reaches through, so a new call site that
    forgets it is a TypeError on the first test that runs it — not a silent `os.environ` read put
    back into the fold. A default here is exactly how the defect would return."""
    import inspect

    from looplab.core import advisory_payloads

    param = inspect.signature(getattr(advisory_payloads, helper)).parameters["env"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty, f"{helper} defaults `env`"
