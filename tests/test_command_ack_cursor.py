from types import SimpleNamespace

from looplab.engine.orchestrator import Engine


class _CountingEvents:
    def __init__(self, rows):
        self.rows = list(rows)
        self.reads = 0

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, index):
        self.reads += 1
        return self.rows[index]


class _Store:
    def __init__(self):
        self.appended = []

    def append(self, event_type, data):
        self.appended.append((event_type, data))


def _event(seq, event_type="node_created", data=None):
    return SimpleNamespace(seq=seq, type=event_type, data=data or {})


def test_command_ack_cursor_bootstraps_once_then_reads_only_the_delta():
    command_id = "cmd_" + "a" * 32
    rows = [_event(i) for i in range(50_000)]
    rows[-1] = _event(49_999, "hint", {"_command_id": command_id})
    events = _CountingEvents(rows)
    engine = object.__new__(Engine)
    engine.store = _Store()

    Engine._ack_commands(engine, events)
    assert events.reads <= 100_001  # first identity + two bootstrap passes
    assert engine.store.appended == [
        ("command_ack", {"command_id": command_id, "event_seq": 49_999})]

    events.reads = 0
    Engine._ack_commands(engine, events)
    assert events.reads == 1  # unchanged log: first-event identity check only
    assert len(engine.store.appended) == 1

    events.rows.append(_event(50_000))
    events.reads = 0
    Engine._ack_commands(engine, events)
    assert events.reads == 3  # first identity + two passes over exactly one appended row
    assert len(engine.store.appended) == 1


def test_command_ack_cursor_resets_when_eventstore_snapshot_is_rebuilt():
    first_id = "cmd_" + "a" * 32
    second_id = "cmd_" + "b" * 32
    events = _CountingEvents([
        _event(0, "run_started"),
        _event(1, "hint", {"_command_id": first_id}),
    ])
    engine = object.__new__(Engine)
    engine.store = _Store()
    Engine._ack_commands(engine, events)

    # EventStore reconstructs Event objects when a log is replaced or rewritten.  Even a same-size
    # replacement therefore changes the first object's identity and forces a full, safe bootstrap.
    events.rows = [
        _event(0, "run_started"),
        _event(1, "hint", {"_command_id": second_id}),
    ]
    events.reads = 0
    Engine._ack_commands(engine, events)

    assert events.reads == 5  # first identity + two passes over both replacement rows
    assert engine.store.appended[-1] == (
        "command_ack", {"command_id": second_id, "event_seq": 1})


def test_command_ack_bootstrap_honours_an_ack_later_in_the_same_snapshot():
    command_id = "cmd_" + "c" * 32
    events = _CountingEvents([
        _event(0, "run_started"),
        _event(1, "hint", {"_command_id": command_id}),
        _event(2, "command_ack", {"command_id": command_id, "event_seq": 1}),
    ])
    engine = object.__new__(Engine)
    engine.store = _Store()

    Engine._ack_commands(engine, events)

    assert engine.store.appended == []
