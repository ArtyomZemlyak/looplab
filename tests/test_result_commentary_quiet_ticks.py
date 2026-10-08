"""A live run's no-work tick is cheap: no activity lease, no read of the whole log, no fold.

`ResultCommentaryService` ticks every 2 s, and a live run's `events.jsonl` changes on almost every
tick, so the exact-stamp `idle` skip never fired on one: every tick took a `ui_llm` activity lease
(an `.activity_*.json` file that makes delete/reset answer 409), read and folded the log two or three
times, ran `log_integrity` and hashed every node's measurement before learning there was nothing to
explain. These drive the worker the way the scheduler does and COUNT what each tick paid for.
"""
import looplab.serve.result_notices as notices
from looplab.serve.result_commentary import _FILE, _load
from test_result_commentary import rows, setup_run, terminal


class _Counts:
    def __init__(self, monkeypatch, srv):
        self.lease = self.fold = self.read = self.integrity = self.state = 0
        commands = srv.commands
        lease, fold, read, integrity = commands.run_activity, notices.fold, srv.events, srv.log_integrity
        state = srv.state

        def counted_lease(*args, **kwargs):
            self.lease += 1
            return lease(*args, **kwargs)

        def counted_fold(*args, **kwargs):
            self.fold += 1
            return fold(*args, **kwargs)

        def counted_read(*args, **kwargs):
            self.read += 1
            return read(*args, **kwargs)

        def counted_integrity(*args, **kwargs):
            self.integrity += 1
            return integrity(*args, **kwargs)

        def counted_state(*args, **kwargs):
            self.state += 1
            return state(*args, **kwargs)

        monkeypatch.setattr(commands, "run_activity", counted_lease)
        monkeypatch.setattr(notices, "fold", counted_fold)
        monkeypatch.setattr(srv, "events", counted_read)
        monkeypatch.setattr(srv, "log_integrity", counted_integrity)
        # `srv.state` is a fold too; the worker reads the receipts' own fold instead.
        monkeypatch.setattr(srv, "state", counted_state)

    def reset(self):
        self.lease = self.fold = self.read = self.integrity = self.state = 0

    def total(self):
        return self.lease + self.fold + self.read + self.integrity + self.state


def _neutral(events, node=1):
    """What a live run writes between completions: diagnostic phase rows and the cost ledger."""
    events.append("phase_progress", {"phase": "build", "detail": "working"})
    events.append("agent_phase_started", {"node_id": node, "phase": "plan"})
    events.append("llm_usage", {"cost": 0.0, "usage": {"prompt_tokens": 1, "completion_tokens": 1}})


def _settle(service, rd, model):
    """Explain the first completion and run the worker until a pass ends with nothing to do."""
    for _ in range(3):
        service.process_run(rd)
    assert len(model.calls) == 1


def test_an_active_run_with_only_non_terminal_rows_takes_no_lease_and_folds_nothing(tmp_path, monkeypatch):
    rd, events, _, gen, service, model = setup_run(tmp_path, monkeypatch)
    terminal(events)
    _settle(service, rd, model)
    counts = _Counts(monkeypatch, service.srv)
    for _ in range(5):
        _neutral(events)
        service.process_run(rd)
    assert counts.total() == 0, vars(counts)
    assert len(model.calls) == 1

    # A completion behind the neutral rows is not slept through: the fenced pass runs (with its
    # leases), and the worker's own fold is the receipts' fold — the only other is
    # `publish_internal`'s fresh fence; `srv.state` is not re-folded for the history or the context.
    events.append("node_created", {"node_id": 1, "parent_ids": [0], "operator": "draft",
                                   "idea": {"operator": "draft"}, "code": "print(2)"})
    terminal(events, node=1, score=0.5)
    service.process_run(rd)
    assert len(model.calls) == 2
    assert counts.lease >= 1 and counts.fold == 2 and counts.state == 0, vars(counts)
    assert _load(rd).jobs and all(job.status == "published" for job in _load(rd).jobs.values())


def test_a_non_neutral_row_runs_the_full_pass_with_a_single_fold(tmp_path, monkeypatch):
    rd, events, _, gen, service, model = setup_run(tmp_path, monkeypatch)
    terminal(events)
    _settle(service, rd, model)
    counts = _Counts(monkeypatch, service.srv)
    # A folded row the allow-list does not name — even one that buys nothing — is decided by the
    # fenced pass, never by the cheap scan; and that pass folds exactly once.
    events.append("node_created", {"node_id": 1, "parent_ids": [0], "operator": "draft",
                                   "idea": {"operator": "draft"}, "code": "print(2)"})
    service.process_run(rd)
    assert counts.lease == 1 and counts.fold == 1 and counts.state == 0, vars(counts)
    counts.reset()
    _neutral(events)
    service.process_run(rd)
    assert counts.total() == 0, vars(counts)
    assert len(model.calls) == 1


def test_a_changed_job_store_or_ledger_or_replaced_log_is_never_answered_by_the_scan(tmp_path, monkeypatch):
    rd, events, _, gen, service, model = setup_run(tmp_path, monkeypatch)
    terminal(events)
    _settle(service, rd, model)
    counts = _Counts(monkeypatch, service.srv)
    # The presentation journal lost: the durable reply is restored, which only the full pass can do.
    (rd / "result_commentary.jsonl").unlink()
    _neutral(events)
    service.process_run(rd)
    assert counts.lease == 1
    assert rows(service, rd, gen)[0]["commentary"] == model.reply
    assert len(model.calls) == 1 and (rd / _FILE).exists()


def test_an_undecodable_appended_row_falls_back_to_the_fenced_pass(tmp_path, monkeypatch):
    rd, events, _, gen, service, model = setup_run(tmp_path, monkeypatch)
    terminal(events)
    _settle(service, rd, model)
    counts = _Counts(monkeypatch, service.srv)
    with open(rd / "events.jsonl", "ab") as fh:
        fh.write(b'{"not": "an event"}\n')
    service.tick()  # the fenced pass refuses the damaged log (a contained 503); the scan must not decide
    assert counts.lease == 1, "the scan accepted a row it could not decode"
