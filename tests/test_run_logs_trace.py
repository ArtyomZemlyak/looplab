"""The assistant can read what a node actually DID — its execution logs (stdout tail + full
error/stderr) and its agent trace (a linear, de-duplicated conversation) — plus the read-file
allowlist now covers log/line-delimited-data formats so a run's own `*.log`/`*.jsonl` are readable.

Offline: synthetic RunState + a hand-written events.jsonl/spans.jsonl (spans via the real Tracer),
no model needed. Complements test_run_tools.py (RunTools) and test_cross_run.py (SiblingRunTools)."""
from __future__ import annotations

import json
from pathlib import Path

from looplab.core import tracing
from looplab.core._pathsafe import readable
from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.tools.run_tools import RunTools
from looplab.tools.machine_runs_tools import MachineRunsTools
from looplab.tools.reposcout import RepoScoutTools
from looplab.core.tracing import JsonlSpanExporter, Tracer


# --------------------------------------------------------------------------- RunTools.read_logs
def _logs_st() -> RunState:
    st = RunState(goal="minimize loss", direction="min")
    st.nodes = {
        0: Node(id=0, operator="draft", idea=Idea(operator="draft", params={"x": 0.0}),
                metric=0.3, status=NodeStatus.evaluated, eval_seconds=12.0,
                stdout_tail="epoch 1 loss=0.5\nepoch 2 loss=0.3\n{\"metric\": 0.3}"),
        1: Node(id=1, operator="draft", idea=Idea(operator="draft", params={"x": 9.0}),
                status=NodeStatus.failed, error_reason="crash",
                error="Traceback (most recent call last):\n  ...\nValueError: boom-" + "x" * 500),
        2: Node(id=2, operator="draft", idea=Idea(operator="draft", params={"x": 1.0}),
                status=NodeStatus.pending),
    }
    return st


def test_read_logs_surfaces_stdout_and_full_error():
    rt = RunTools()
    rt.bind_state(_logs_st())
    assert "read_logs" in {f["function"]["name"] for f in rt.specs()}

    ok = rt.execute("read_logs", {"node_id": 0})
    assert "stdout (tail)" in ok and "epoch 1 loss=0.5" in ok and "eval=12s" in ok

    # The full error is shown — NOT the 300-char summary read_experiment truncates to.
    bad = rt.execute("read_logs", {"node_id": 1})
    assert "failure=crash" in bad and "ValueError: boom-" in bad
    assert bad.count("x") >= 500                       # whole stderr, not a 300-char slice
    summary = rt.execute("read_experiment", {"node_id": 1})
    assert len(summary) < len(bad)                     # read_experiment stays a short summary

    assert "no stdout or error" in rt.execute("read_logs", {"node_id": 2})
    assert "no experiment #99" in rt.execute("read_logs", {"node_id": 99})


def test_read_logs_clips_to_the_tail():
    """A huge stdout is clipped to its TAIL (where the error + final metric line live), flagged."""
    st = RunState(goal="g", direction="min")
    st.nodes = {0: Node(id=0, operator="draft", idea=Idea(operator="draft", params={}),
                        status=NodeStatus.evaluated,
                        stdout_tail="START\n" + "line\n" * 20000 + "FINAL metric=0.1")}
    rt = RunTools()
    rt.bind_state(st)
    out = rt.execute("read_logs", {"node_id": 0})
    assert "FINAL metric=0.1" in out                   # the tail is kept
    assert "START" not in out                           # the head was dropped
    assert "earlier chars truncated" in out


def test_read_logs_error_tail_survives_the_agent_result_cap():
    """The shared tool loop HEAD-truncates every tool result to 4000 chars. A Python traceback puts
    the exception line at the BOTTOM, so read_logs must stay under that cap (and keep the error's
    tail) — else the one line that says WHY the node failed gets silently cut off upstream."""
    st = RunState(goal="g", direction="min")
    st.nodes = {0: Node(id=0, operator="draft", idea=Idea(operator="draft", params={}),
                        status=NodeStatus.failed, error_reason="crash",
                        stdout_tail="epoch 1 loss=0.5\n",
                        error="Traceback (most recent call last):\n" + "  frame\n" * 5000
                              + "ValueError: the real reason it died")}
    rt = RunTools()
    rt.bind_state(st)
    out = rt.execute("read_logs", {"node_id": 0})
    assert len(out) <= 4000                             # fits the agent-layer result cap, no silent cut
    assert "ValueError: the real reason it died" in out  # the exception line (error tail) is preserved


# ----------------------------------------------- MachineRunsTools.read_run_logs / read_run_trace
def _make_run(tmp_path: Path) -> Path:
    """A minimal on-disk run: events.jsonl (fold → one evaluated node with a stdout tail) + a real
    spans.jsonl written by the Tracer (one generation + one tool under a create_node trace)."""
    rd = tmp_path / "demo"
    rd.mkdir()
    events = [
        {"type": "run_started", "data": {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"}},
        {"type": "node_created", "data": {"node_id": 0, "operator": "draft",
                                          "idea": {"operator": "draft", "params": {"x": 1.0}}}},
        {"type": "node_evaluated", "data": {"node_id": 0, "metric": 0.3, "eval_seconds": 12.0,
                                            "stdout_tail": "epoch1 loss=0.5\n{\"metric\": 0.3}"}},
    ]
    # Persist through EventStore so each record carries a dense, advancing `seq`. The strict fail-closed
    # reader (5f011a2) treats seq-less hand-written envelopes (all default seq 0) as a truncated tail —
    # only run_started would survive the boundary, the fold would see zero nodes, and read_run_logs would
    # answer "(no experiment #0)". EventStore stamps the seqs for us, so the whole prefix folds.
    from looplab.events.eventstore import EventStore
    store = EventStore(rd / "events.jsonl")
    for e in events:
        store.append(e["type"], e["data"])

    tracing.set_llm_capture(True)
    t = Tracer(JsonlSpanExporter(rd / "spans.jsonl"), run_id="demo")
    with t.span("create_node", new_trace=True, node_id=0):
        with tracing.generation(op="chat", model="m",
                                messages=[{"role": "system", "content": "You are a developer"},
                                          {"role": "user", "content": "write the solution"}]) as g:
            g.output("My plan: call read_experiment then write code.").usage(
                {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        with tracing.tool("read_experiment", {"node_id": 0}) as to:
            to.output("metric=0.3")
    return rd.parent


def test_read_run_logs_reads_stdout_from_disk(tmp_path):
    root = _make_run(tmp_path)
    rts = MachineRunsTools(root)
    names = {f["function"]["name"] for f in rts.specs()}
    assert {"read_run_logs", "read_run_trace"} <= names

    out = rts.execute("read_run_logs", {"run_id": "demo", "node_id": 0})
    assert "run demo" in out and "epoch1 loss=0.5" in out and "eval=12s" in out
    assert "no such run" in rts.execute("read_run_logs", {"run_id": "nope", "node_id": 0})


def test_read_run_trace_is_a_linear_conversation(tmp_path):
    root = _make_run(tmp_path)
    rts = MachineRunsTools(root)
    tr = rts.execute("read_run_trace", {"run_id": "demo", "node_id": 0})
    assert "node #0" in tr and "stage: create_node" in tr
    assert "You are a developer" in tr                  # the request prompt is shown once
    assert "My plan: call read_experiment" in tr        # the generation output
    assert "read_experiment" in tr and "metric=0.3" in tr  # the tool call + its result

    # stage filter: a non-matching label yields an explicit empty note, not a crash.
    assert "no trace stages" in rts.execute(
        "read_run_trace", {"run_id": "demo", "node_id": 0, "stage": "repair"})


def test_read_run_trace_uses_bounded_node_offsets_not_a_whole_file_load(tmp_path, monkeypatch):
    """The assistant's trace reader shares the web route's indexed per-node memory bound."""
    from looplab.events import traceview

    root = _make_run(tmp_path)

    def whole_file_load_forbidden(_path):
        raise AssertionError("read_run_trace must not materialize every span")

    monkeypatch.setattr(traceview, "load_spans", whole_file_load_forbidden)
    out = MachineRunsTools(root).execute(
        "read_run_trace", {"run_id": "demo", "node_id": 0})

    assert "My plan: call read_experiment" in out
    assert "read_experiment" in out and "metric=0.3" in out


def test_read_run_trace_without_spans_is_graceful(tmp_path):
    root = _make_run(tmp_path)
    (root / "demo" / "spans.jsonl").unlink()
    rts = MachineRunsTools(root)
    assert "no spans.jsonl" in rts.execute("read_run_trace", {"run_id": "demo", "node_id": 0})


def test_read_run_trace_quarantines_malformed_span_shape(tmp_path):
    """A JSON-valid row with recoverable malformed attributes becomes an empty safe observation.

    The reader must neither crash nor mislabel the whole trace file as unreadable merely because one
    complete line has an old/custom shape.
    """
    root = _make_run(tmp_path)
    (root / "demo" / "spans.jsonl").write_text(
        json.dumps({"span_id": "s1", "trace_id": "tr", "parent_id": None, "attributes": None}) + "\n",
        encoding="utf-8")
    rts = MachineRunsTools(root)
    out = rts.execute("read_run_trace", {"run_id": "demo", "node_id": 0})
    assert isinstance(out, str) and "no trace stages recorded" in out


# --------------------------------------------------- read-file allowlist covers logs + jsonl data
def test_readable_allowlist_covers_logs_and_jsonl():
    for name in ("eval.log", "spans.jsonl", "events.jsonl", "run.ndjson", "job.out", "job.err"):
        assert readable(Path(name)), name
    # still blocks the truly-unknown/binary
    assert not readable(Path("model.bin"))


def test_reposcout_reads_a_log_but_refuses_secrets(tmp_path):
    (tmp_path / "eval.log").write_text("epoch 1 loss=0.5\nepoch 2 loss=0.3\n", encoding="utf-8")
    (tmp_path / "spans.jsonl").write_text('{"span_id": "a"}\n', encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=xyz", encoding="utf-8")
    sc = RepoScoutTools([tmp_path])
    assert "epoch 1 loss=0.5" in sc.execute("read_file", {"path": str(tmp_path / "eval.log")})
    assert '"span_id"' in sc.execute("read_file", {"path": str(tmp_path / "spans.jsonl")})
    assert "refused" in sc.execute("read_file", {"path": str(tmp_path / ".env")})


# ------------------------------------ the assistant's trace window can be AIMED, not only widened
def _make_long_run(tmp_path: Path, episodes: int = 6) -> Path:
    """A run whose node recorded several episodes — the shape a bounded tail cannot show.

    `runs/rubert-dr-0804` node 1 is the measured original: 14,507 spans over 3 h 50 m across 2,345
    inline repairs, all of them generation 0 (inline repair does not bump `Node.attempt`), of which
    the default window reaches the last 7.6 minutes. Six episodes and a small window is the same
    fact at test size.
    """
    rd = tmp_path / "long"
    rd.mkdir()
    from looplab.events.eventstore import EventStore
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "long", "task_id": "t", "goal": "g", "direction": "min"})
    store.append("node_created", {"node_id": 0, "operator": "draft",
                                  "idea": {"operator": "draft", "params": {"x": 1.0}}})
    tracing.set_llm_capture(True)
    t = Tracer(JsonlSpanExporter(rd / "spans.jsonl"), run_id="long")
    for i in range(episodes):
        with t.span(f"repair_{i}", new_trace=True, node_id=0):
            with tracing.generation(op="chat", model="m",
                                    messages=[{"role": "user", "content": f"fix attempt {i}"}]) as g:
                g.output(f"episode-{i}-output")
    return rd.parent


def test_the_assistant_can_map_a_nodes_episodes_and_seek_its_trace_window(tmp_path, monkeypatch):
    """F6's seekable window reached the two HTTP routes and NOT this surface — which is the one an
    operator actually asks "what happened in this node".

    The window is the newest N spans of one `(node_id, generation)`, so without an anchor every
    earlier episode is unreachable at any parameter, and the docstring's claim that the assistant
    "reads exactly what the human sees" stopped being true the day `?before=` shipped.
    """
    from looplab.events import traceview

    root = _make_long_run(tmp_path)
    monkeypatch.setattr(traceview, "TRACE_CONVERSATION_SPAN_CAP", 2)
    rts = MachineRunsTools(root)

    tail = rts.execute("read_run_trace", {"run_id": "long", "node_id": 0})
    assert "episode-5-output" in tail, "the tail is what an unanchored read shows"
    assert "episode-0-output" not in tail
    # …and it SAYS what it left out, beside the control that reaches it. A bounded window that does
    # not is read as the whole node.
    assert "outside this window" in tail and "read_run_trace_episodes" in tail

    mapped = rts.execute("read_run_trace_episodes", {"run_id": "long", "node_id": 0})
    assert "6 episode(s)" in mapped
    anchors = [line.split("anchor=")[1].strip() for line in mapped.splitlines() if "anchor=" in line]
    assert len(anchors) == 6, mapped
    assert "repair_0" in mapped and "repair_5" in mapped

    seeked = rts.execute("read_run_trace",
                         {"run_id": "long", "node_id": 0, "before": anchors[0]})
    assert "episode-0-output" in seeked, "the window MOVED to the anchored episode"
    assert "episode-5-output" not in seeked
    assert anchors[0] in seeked, "the answer echoes where the window was placed"


def test_an_anchor_this_run_does_not_know_is_refused_not_degraded_to_the_tail(tmp_path):
    """The routes' rule (`_settle_window_anchor`), for the same reason: answering with the NEWEST
    spans under an older episode's label is worse than answering nothing — and worse still for a
    reader that cannot see the label."""
    root = _make_long_run(tmp_path)
    out = MachineRunsTools(root).execute(
        "read_run_trace", {"run_id": "long", "node_id": 0, "before": "not-a-span-id"})

    assert "episode-5-output" not in out, "a refused anchor must not silently become the tail"
    assert "read_run_trace_episodes" in out, "the refusal names the move that works"


def test_the_episode_map_shows_both_ends_and_counts_what_it_elided(tmp_path):
    """A map is what a long node is READ THROUGH, so it may not be a tail either: the first
    episodes are where a bug first showed and the last are where it died. Both ends, the middle
    counted, and every row reachable by ordinal."""
    root = _make_long_run(tmp_path, episodes=70)
    rts = MachineRunsTools(root)

    mapped = rts.execute("read_run_trace_episodes", {"run_id": "long", "node_id": 0})
    assert "70 episode(s)" in mapped
    assert "repair_0 " in mapped and "repair_69 " in mapped
    assert "20 episode(s) elided" in mapped

    paged = rts.execute("read_run_trace_episodes",
                        {"run_id": "long", "node_id": 0, "from_index": 30, "limit": 5})
    assert "repair_0 " not in paged and "repair_69 " not in paged
    assert len([line for line in paged.splitlines() if "anchor=" in line]) == 5


def test_the_new_trace_tools_stay_soft_failing_reads(tmp_path):
    """Every tool here answers a SENTENCE, never an exception: `drive_tool_loop` does not guard
    `tools.execute`, so one odd shape would end the whole assistant turn."""
    rts = MachineRunsTools(tmp_path)
    for name in ("read_run_trace", "read_run_trace_episodes"):
        assert "no such run" in rts.execute(name, {"run_id": "nope", "node_id": 0})
    names = {f["function"]["name"] for f in rts.specs()}
    assert "read_run_trace_episodes" in names
