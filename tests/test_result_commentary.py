"""Automatic prose uses real receipt fences and scripted (never paid) clients."""
import json
import threading
import time

import pytest

from looplab.core.config import Settings, read_config_snapshot
from looplab.core.llm import CostAccountant
from looplab.events.eventstore import interprocess_lock
from looplab.serve.result_commentary import ResultCommentaryService, _FILE, _load
from looplab.serve.result_notices import snapshot
from test_result_notices import _run


class ScriptedClient:
    def __init__(self, reply="Результат получен; причинный эффект пока не установлен. Следующий шаг — повторить сравнение."):
        self.reply = reply
        self.calls = []
        self.accountant = CostAccountant()

    def complete_text(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        self.accountant.add(0.01, {"prompt_tokens": 10, "completion_tokens": 20})
        if isinstance(self.reply, Exception):
            raise self.reply
        if callable(self.reply):
            return self.reply()
        return self.reply


def setup_run(tmp_path, monkeypatch, **settings):
    rd, events, client, gen = _run(tmp_path, monkeypatch, external=False)
    (rd / "config.snapshot.json").write_text(Settings(backend="llm", output_language="ru", **settings).model_dump_json())
    srv = client.app.state.looplab
    model = ScriptedClient()
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda cfg, **kw: model)
    service = ResultCommentaryService(srv)
    service.started_at = 0
    return rd, events, client, gen, service, model


def terminal(events, node=0, score=0.25):
    events.append("node_evaluated", {"node_id": node, "metric": score})


def rows(service, rd, gen):
    return snapshot(service.srv, rd, gen)["items"]


def test_completion_publishes_one_short_russian_reply_and_accounts_once(tmp_path, monkeypatch):
    rd, events, client, gen, service, model = setup_run(tmp_path, monkeypatch)
    service.process_run(rd)
    assert not model.calls
    terminal(events)
    service.process_run(rd)
    first = rows(service, rd, gen)[0]
    assert first["commentary"] == model.reply and first["commentary_source"] == "assistant"
    assert first["score"] == 0.25 and first["commentary_status"] == "published"
    assert not (rd / "chat.jsonl").exists()
    assert "Russian" in model.calls[0][0][0]["content"]
    assert model.calls[0][1] == {"max_tokens": 400}
    for _ in range(3):
        service.process_run(rd)
        assert rows(service, rd, gen)[0] == first
    assert len(model.calls) == 1
    assert service.srv.state(rd).llm_cost["cost"] == pytest.approx(0.01)
    # The public POST stays external-only even for the owner.
    body = {"expected_generation": gen, "receipt_id": first["id"], "evidence_token": first["evidence_token"],
            "summary": "Other", "action_id": "forged"}
    assert client.post("/api/runs/demo/result-notices", json=body,
                       headers={"X-LoopLab-Token": "owner-secret"}).status_code == 409


@pytest.mark.parametrize("settings", [{"external_harness": True, "backend": "toy"}, {"backend": "toy"},
                                      {"assistant_result_commentary": False}])
def test_disabled_modes_never_create_paid_claim(tmp_path, monkeypatch, settings):
    rd, events, _, gen, service, model = setup_run(tmp_path, monkeypatch)
    cfg = Settings(backend="llm").model_copy(update=settings)
    (rd / "config.snapshot.json").write_text(cfg.model_dump_json())
    terminal(events)
    service.process_run(rd)
    assert not model.calls and not (rd / _FILE).exists()
    assert rows(service, rd, gen)[0]["commentary"] is None


def test_legacy_snapshots_and_finished_history_are_not_billed(tmp_path, monkeypatch):
    rd, events, _, _, service, model = setup_run(tmp_path, monkeypatch)
    cfg = Settings(backend="llm").model_dump()
    cfg.pop("assistant_result_commentary")
    (rd / "config.snapshot.json").write_text(json.dumps(cfg))
    assert read_config_snapshot(rd / "config.snapshot.json").assistant_result_commentary is False
    terminal(events)
    service.process_run(rd)
    assert not model.calls
    cfg["assistant_result_commentary"] = True
    (rd / "config.snapshot.json").write_text(json.dumps(cfg))
    events.append("run_finished", {"reason": "done"})
    service.started_at = time.time() + 1
    service.process_run(rd)
    assert not model.calls and not (rd / _FILE).exists()


def test_active_history_skipped_but_next_completion_and_run_are_explained(tmp_path, monkeypatch):
    rd, events, _, gen, service, model = setup_run(tmp_path, monkeypatch)
    terminal(events)
    service.started_at = time.time() + 1
    service.process_run(rd)
    assert not model.calls
    events.append("node_created", {"node_id": 1, "operator": "draft", "idea": {"operator": "draft"}, "code": "x"})
    terminal(events, 1, 0.2)
    service.process_run(rd)
    assert len(model.calls) == 1
    assert rows(service, rd, gen)[0]["commentary"] is None
    events.append("run_finished", {"reason": "done"})
    monkeypatch.setattr("looplab.serve.result_notices._engine_liveness", lambda rd: False)
    service.process_run(rd)
    assert len(model.calls) == 2
    assert rows(service, rd, gen)[-1]["kind"] == "run"
    assert rows(service, rd, gen)[-1]["commentary"] == model.reply


@pytest.mark.parametrize("reply", [RuntimeError("SECRET"), "", "x" * 701, None])
def test_bad_provider_reply_visible_and_never_retried(tmp_path, monkeypatch, reply):
    rd, events, _, gen, service, model = setup_run(tmp_path, monkeypatch)
    model.reply = reply
    terminal(events)
    service.process_run(rd)
    service.process_run(rd)
    notice = rows(service, rd, gen)[0]
    assert notice["commentary"] is None and notice["commentary_status"] == "failed"
    assert len(model.calls) == 1
    assert "SECRET" not in (rd / _FILE).read_text()


def test_persisted_reply_recovered_after_publication_failure_without_model(tmp_path, monkeypatch):
    rd, events, _, gen, service, model = setup_run(tmp_path, monkeypatch)
    terminal(events)
    import looplab.serve.result_notices as notices
    original = notices.publish_internal
    def fail(*args):
        raise OSError("disk")
    monkeypatch.setattr(notices, "publish_internal", fail)
    with pytest.raises(OSError):
        service.process_run(rd)
    assert _load(rd).jobs[rows(service, rd, gen)[0]["evidence_token"]].status == "ready"
    monkeypatch.setattr(notices, "publish_internal", original)
    restarted = ResultCommentaryService(service.srv)
    restarted.process_run(rd)
    assert rows(service, rd, gen)[0]["commentary"] == model.reply
    assert len(model.calls) == 1


def test_uncertain_claim_after_crash_does_not_repeat_paid_call(tmp_path, monkeypatch):
    rd, events, _, gen, service, model = setup_run(tmp_path, monkeypatch)
    terminal(events)
    import looplab.serve.result_commentary as module
    save = module._save
    def crash_before_reply_save(rd, store):
        if any(job.status == "ready" for job in store.jobs.values()):
            raise OSError("reply not durable")
        save(rd, store)
    monkeypatch.setattr(module, "_save", crash_before_reply_save)
    service.process_run(rd)
    monkeypatch.setattr(module, "_save", save)
    ResultCommentaryService(service.srv).process_run(rd)
    assert rows(service, rd, gen)[0]["commentary_status"] == "interrupted"
    assert len(model.calls) == 1


def test_current_evidence_invalidates_reply_before_publication(tmp_path, monkeypatch):
    rd, events, _, gen, service, model = setup_run(tmp_path, monkeypatch)
    terminal(events)
    def changed():
        events.append("node_abort", {"node_id": 0})
        return "An obsolete interpretation"
    model.reply = changed
    service.process_run(rd)
    notice = rows(service, rd, gen)[0]
    assert notice["score"] is None and notice["commentary"] is None
    assert next(iter(_load(rd).jobs.values())).status == "superseded"


def test_sibling_worker_cannot_duplicate_inflight_call_and_reads_stay_pure(tmp_path, monkeypatch):
    rd, events, client, gen, service, model = setup_run(tmp_path, monkeypatch)
    terminal(events)
    entered, release = threading.Event(), threading.Event()
    def blocked():
        entered.set()
        assert release.wait(5)
        return "Measured outcome received. Repeat before concluding."
    model.reply = blocked
    thread = threading.Thread(target=service.process_run, args=(rd,))
    thread.start()
    try:
        assert entered.wait(5)
        assert rows(service, rd, gen)[0]["commentary_status"] == "generating"
        ResultCommentaryService(service.srv).process_run(rd)
        assert len(model.calls) == 1
        assert client.get("/api/runs/demo/result-notices", params={"expected_generation": gen},
                          headers={"X-LoopLab-Token": "owner-secret"}).status_code == 200
        # A blocked commentary provider does not hold evaluation or the event writer.
        events.append("node_created", {"node_id": 1, "operator": "draft", "idea": {"operator": "draft"}, "code": "x"})
        terminal(events, 1)
        assert len(rows(service, rd, gen)) == 2
        assert service.srv.commands.run_generation(rd) == gen
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert rows(service, rd, gen)[0]["commentary_source"] == "assistant"


def test_unavailable_job_journal_preserves_facts_and_refuses_paid_work(tmp_path, monkeypatch):
    rd, events, _, gen, service, model = setup_run(tmp_path, monkeypatch)
    terminal(events)
    (rd / _FILE).write_text('{"broken"')
    with pytest.raises(ValueError):
        service.process_run(rd)
    notice = rows(service, rd, gen)[0]
    assert notice["score"] == 0.25 and notice["commentary_status"] == "unavailable"
    assert not model.calls


def test_real_asgi_lifespan_runs_scheduler_without_chat_or_get(tmp_path, monkeypatch):
    rd, events, client, gen, service, model = setup_run(tmp_path, monkeypatch)
    scheduler = client.app.state.looplab.result_commentary
    scheduler.interval = 0.01
    with client:
        terminal(events)
        deadline = time.monotonic() + 5
        while not (rd / "result_commentary.jsonl").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (rd / "result_commentary.jsonl").exists()
        assert len(model.calls) == 1
    assert scheduler.stop_event.is_set()
    assert not scheduler.thread.is_alive()


def test_required_os_lock_blocks_paid_work(tmp_path, monkeypatch):
    rd, events, _, _, service, model = setup_run(tmp_path, monkeypatch)
    terminal(events)
    with interprocess_lock(rd / (_FILE + ".lock"), required=True):
        service.tick()
        assert not model.calls
    service.process_run(rd)
    assert len(model.calls) == 1


def test_failed_strict_claim_never_calls_provider(tmp_path, monkeypatch):
    rd, events, _, _, service, model = setup_run(tmp_path, monkeypatch)
    terminal(events)
    def denied(*args):
        raise OSError("sync unavailable")
    monkeypatch.setattr("looplab.serve.result_commentary.strict_atomic_write_bytes", denied)
    with pytest.raises(OSError):
        service.process_run(rd)
    assert not model.calls


def test_failed_node_and_new_attempt_have_separate_comments(tmp_path, monkeypatch):
    rd, events, _, gen, service, model = setup_run(tmp_path, monkeypatch)
    events.append("node_failed", {"node_id": 0, "reason": "crash", "error": "bad program"})
    service.process_run(rd)
    first = rows(service, rd, gen)[0]
    assert first["score"] is None and first["commentary"]
    events.append("node_reset", {"node_id": 0})
    events.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 0.2})
    service.process_run(rd)
    current = rows(service, rd, gen)[0]
    assert current["attempt"] == 1 and current["evidence_token"] != first["evidence_token"]
    assert current["commentary"]
    assert len(model.calls) == 2


def test_exhausted_existing_run_budget_blocks_provider_and_does_not_duplicate_prior_cost(tmp_path, monkeypatch):
    rd, events, _, gen, service, model = setup_run(tmp_path, monkeypatch, llm_budget_usd=0.01)
    model.accountant = CostAccountant(limit=0.01)
    events.append("llm_usage", {"cost": 0.02})
    terminal(events)
    service.process_run(rd)
    assert not model.calls
    # Nothing was billed, so the claim is released rather than failed forever...
    assert rows(service, rd, gen)[0]["commentary_status"] != "failed"
    assert service.srv.state(rd).llm_cost["cost"] == pytest.approx(0.02)
    service.process_run(rd)
    assert not model.calls, "an unchanged run is not retried on every tick"
    # ...and the explanation is bought once the operator gives the run headroom again.
    model.accountant = CostAccountant()
    (rd / "config.snapshot.json").write_text(
        Settings(backend="llm", output_language="ru", llm_budget_usd=1.0).model_dump_json())
    service.process_run(rd)
    assert len(model.calls) == 1
    assert rows(service, rd, gen)[0]["commentary_status"] == "published"


def test_durable_reply_restores_missing_presentation_journal_without_payment(tmp_path, monkeypatch):
    rd, events, _, gen, service, model = setup_run(tmp_path, monkeypatch)
    terminal(events)
    service.process_run(rd)
    (rd / "result_commentary.jsonl").unlink()
    assert rows(service, rd, gen)[0]["commentary_status"] == "ready"
    ResultCommentaryService(service.srv).process_run(rd)
    assert rows(service, rd, gen)[0]["commentary"] == model.reply
    assert len(model.calls) == 1


@pytest.mark.parametrize("raw", ['{"version":1,"version":1}', '{"version":true}', '[' * 1200])
def test_ambiguous_job_json_cannot_authorize_a_second_call(tmp_path, monkeypatch, raw):
    rd, events, _, gen, service, model = setup_run(tmp_path, monkeypatch)
    terminal(events)
    (rd / _FILE).write_text(raw)
    service.tick()
    assert not model.calls
    assert rows(service, rd, gen)[0]["commentary_status"] == "unavailable"


def test_idle_runs_do_not_replay_or_write_leases_until_their_sources_change(tmp_path, monkeypatch):
    rd, events, _, _, service, model = setup_run(tmp_path, monkeypatch)
    import looplab.serve.result_notices as notices
    original = notices._receipts
    calls = []
    def spy(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr(notices, "_receipts", spy)
    service.process_run(rd)
    assert len(calls) == 1
    service.process_run(rd)
    assert len(calls) == 1 and not model.calls
    terminal(events)
    service.process_run(rd)
    assert len(model.calls) == 1 and len(calls) > 1


def test_completion_arriving_during_idle_scan_is_not_marked_as_already_handled(tmp_path, monkeypatch):
    rd, events, _, _, service, model = setup_run(tmp_path, monkeypatch)
    import looplab.serve.result_notices as notices
    original = notices._receipts
    arrived = []
    def race(*args):
        result = original(*args)
        if not arrived:
            arrived.append(1)
            terminal(events)
        return result
    monkeypatch.setattr(notices, "_receipts", race)
    service.process_run(rd)
    assert not model.calls
    service.process_run(rd)
    assert len(model.calls) == 1


def test_a_store_from_another_generation_does_not_buy_explanations_of_history(tmp_path, monkeypatch):
    from looplab.serve.result_commentary import _Store, _save
    rd, events, _, gen, service, model = setup_run(tmp_path, monkeypatch)
    terminal(events)
    _save(rd, _Store(generation="0" * 64, after_seq=-1))   # left by a replaced/restored run
    service.started_at = time.time() + 1
    service.process_run(rd)
    assert not model.calls
    assert _load(rd).generation != "0" * 64


def test_a_store_from_another_generation_does_not_buy_a_finished_runs_explanation(tmp_path, monkeypatch):
    from looplab.serve.result_commentary import _Store, _save
    rd, events, _, gen, service, model = setup_run(tmp_path, monkeypatch)
    terminal(events)
    events.append("run_finished", {"reason": "done"})
    monkeypatch.setattr("looplab.serve.result_notices._engine_liveness", lambda rd: False)
    _save(rd, _Store(generation="0" * 64, after_seq=-1))
    service.started_at = time.time() + 1
    service.process_run(rd)
    assert not model.calls, "the run-level receipt of a run finished before this server is history"


def _fill_presentation_ledger(rd):
    """Valid rows of another generation, as close to the 2 MiB cap as rows allow (< 300 bytes
    left): too little for any reply row, so `_publish` answers 413."""
    cap, rows, size, n = 2 * 1024 * 1024, [], 0, 0
    while True:
        row = {"generation": "0" * 64, "action_id": f"x{n}", "receipt_id": "run",
               "evidence_token": "1" * 64, "summary": ""}
        base = len(json.dumps(row)) + 1
        if size + base > cap:
            break
        row["summary"] = "s" * min(700, cap - size - base)
        rows.append(json.dumps(row) + "\n")
        size += len(rows[-1])
        n += 1
    assert cap - size < 300
    (rd / "result_commentary.jsonl").write_text("".join(rows))


def test_a_full_presentation_ledger_buys_no_reply_it_could_never_publish(tmp_path, monkeypatch):
    rd, events, _, gen, service, model = setup_run(tmp_path, monkeypatch)
    _fill_presentation_ledger(rd)
    terminal(events)
    for _ in range(3):
        service.process_run(rd)
    assert not model.calls
    assert rd in service.idle, "a full ledger is not re-read on every tick"


def test_a_publish_refused_for_a_full_ledger_fails_the_job_instead_of_stalling(tmp_path, monkeypatch):
    rd, events, _, gen, service, model = setup_run(tmp_path, monkeypatch)
    monkeypatch.setattr("looplab.serve.result_notices.ledger_has_room", lambda rd: True)
    _fill_presentation_ledger(rd)
    terminal(events)
    service.process_run(rd)
    service.process_run(rd)
    assert len(model.calls) == 1
    assert _load(rd).jobs and all(job.status == "failed" for job in _load(rd).jobs.values())
    assert rd in service.idle
