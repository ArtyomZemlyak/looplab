"""A language choice changes prose prompt input, never evidence, tools or accounting."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace

import pytest

from looplab.core.config import Settings
from looplab.core.llm import make_llm_client_for
from looplab.core.output_language import (LanguageClient, current_output_language,
                                         language_client, language_messages, language_scope)
from looplab.core.prose_locale import authored_text
from looplab.core.models import RunState
from looplab.serve.report import generate_report
from looplab.serve.scope_report import generate_scope_report


class RecordingClient:
    def __init__(self):
        self.calls = []
        self.accountant = object()

    def record(self, messages, *args, **kwargs):
        self.calls.append((messages, args, kwargs))
        return "Ответ"

    chat = complete_text = complete_tool = record

    def complete_text_stream(self, messages, *args, **kwargs):
        self.record(messages, *args, **kwargs)
        yield "От"
        yield "вет"


@pytest.mark.parametrize("method", ["chat", "complete_text", "complete_tool", "complete_text_stream"])
def test_each_model_protocol_preserves_arguments_and_original_messages(method):
    raw = RecordingClient()
    client = LanguageClient(raw, "ru")
    messages = [{"role": "system", "content": "Keep protocol."},
                {"role": "user", "content": "train.py --metric loss; score=0.125"}]
    before = deepcopy(messages)
    schema = {"enum": ["failed", "evaluated"], "title": "Result"}
    result = getattr(client, method)(messages, schema, temperature=0.2)
    if method == "complete_text_stream":
        assert list(result) == ["От", "вет"]
    assert messages == before
    sent, args, kwargs = raw.calls[0]
    assert "ALL human-readable prose in Russian" in sent[0]["content"]
    assert "schema keys, enum values" in sent[0]["content"]
    assert sent[1] is messages[1]
    assert args == (schema,) and args[0] is schema
    assert kwargs == {"temperature": 0.2}
    assert client.accountant is raw.accountant
    client.cancel_check = "cancel"
    assert raw.cancel_check == "cancel"
    assert len(raw.calls) == 1


def test_auto_has_identical_factory_and_message_behavior():
    raw = RecordingClient()
    settings = Settings()
    assert language_client(raw, settings) is raw
    messages = [{"role": "user", "content": "Unchanged"}]
    assert language_messages(messages, "auto") is messages
    assert make_llm_client_for(settings, factory=lambda *_a, **_k: raw) is raw


@pytest.mark.parametrize("role", ["researcher", "developer", "report", "strategist", "assistant"])
def test_role_factory_localizes_new_structured_prose(role):
    raw = RecordingClient()
    settings = Settings(output_language="ru")
    client = make_llm_client_for(settings, role=role, factory=lambda *_a, **_k: raw)
    client.complete_tool([{"role": "user", "content": "English task"}], {"type": "object"})
    assert "Russian" in raw.calls[-1][0][0]["content"]


def test_override_inherits_nested_auto_and_resets_after_failure_and_across_threads():
    raw = RecordingClient()
    settings = SimpleNamespace(output_language="auto")
    with language_scope("ru"):
        with language_scope("auto"):
            assert isinstance(language_client(raw, settings), LanguageClient)
            assert current_output_language() == "ru"
        with ThreadPoolExecutor(1) as executor:
            assert executor.submit(current_output_language).result() == "auto"
        with pytest.raises(RuntimeError):
            with language_scope("en"):
                raise RuntimeError()
        assert current_output_language() == "ru"
    assert current_output_language() == "auto"


@pytest.mark.parametrize("invalid", [None, "fr", "ru; execute", ["ru"], 1])
def test_invalid_language_refused(invalid):
    with pytest.raises(ValueError):
        language_messages([], invalid)


def test_new_offline_reports_are_russian_without_rewriting_numeric_authority():
    state = RunState(run_id="r", task_id="t", goal="English task", direction="min")
    report = generate_report(state, None, output_language="ru")
    assert "Нет оценённых узлов" in report["headline"]
    assert "ошибка модели" in report["verdict"]
    assert report["at_node"] == 0 and state.goal == "English task"
    scope = generate_scope_report({"label": "Project RAW"}, [], None, output_language="ru")
    assert scope["headline"] == "В разделе «Project RAW» нет запусков"
    assert "Общий лучший запуск не установлен" in scope["verdict"]
    assert scope["coverage"]["source_runs"] == 0 and scope["comparison_groups"] == []


def test_authored_notice_keeps_the_original_target_and_user_diagnostic():
    assert authored_text("run RAW-id no longer exists, so this condition can never be met", "ru") == (
        "Запуск RAW-id больше не существует; условие монитора невозможно выполнить.")
    assert authored_text("continuous work reported a blocker: USER TEXT {0} $&", "ru") == (
        "Непрерывная работа приостановлена: USER TEXT {0} $&")
    assert authored_text("Original traceback", "ru") == "Original traceback"


def test_stream_binds_the_language_of_the_invocation_before_consumption():
    raw = RecordingClient()
    client = LanguageClient(raw, "ru")
    with language_scope("en"):
        stream = client.complete_text_stream([{"role": "user", "content": "Task"}])
    assert list(stream) == ["От", "вет"]
    assert "prose in English" in raw.calls[0][0][0]["content"]


def test_monitor_terminal_notice_uses_russian_but_preserves_its_instruction_and_status():
    from looplab.serve.assistant_watch import WatchService
    notes = []
    watch = WatchService(None, observe_run=lambda _: None, run_turn_fn=lambda *_: pytest.fail(),
                         append_turn=lambda sid, turn: notes.append((sid, turn)),
                         language_fn=lambda _: "ru")
    record = {"session": "chat-raw", "id": "watch-raw", "status": "expired",
              "waiting_for": "every 30 min", "instruction": "USER instruction /raw/path"}
    watch._notice(record, "reached its 12-wake-up budget")
    sid, notice = notes[0]
    assert sid == "chat-raw" and "Исчерпан лимит срабатываний: 12" in notice["content"]
    assert "USER instruction /raw/path" in notice["content"]
    assert notice["watch"]["status"] == "expired" and notice["applied"] == []


def test_toy_language_does_not_change_optimizer_type_trajectory_or_scoring_code():
    from looplab.adapters.toytask import ToyTask
    from looplab.agents.factory import make_roles
    from looplab.agents.toy_roles import ToyResearcher
    task = ToyTask()
    en, en_dev = make_roles(task, Settings(backend="toy"))
    ru, ru_dev = make_roles(task, Settings(backend="toy", output_language="ru"))
    state = RunState(task_id="t", direction="min")
    a, b = en.propose(state, None), ru.propose(state, None)
    assert type(ru) is ToyResearcher and a.params == b.params
    assert b.rationale == "случайная начальная точка"
    assert en_dev.implement(a) == ru_dev.implement(b)


def test_language_preference_in_external_contract_adds_no_new_gate():
    from looplab.adapters.toytask import ToyTask
    from looplab.harness.obligations import run_obligations
    en = run_obligations(ToyTask(), Settings(backend="toy", external_harness=True), generation="g")
    ru = run_obligations(ToyTask(), Settings(backend="toy", external_harness=True, output_language="ru"), generation="g")
    assert ru.pop("output_language")["language"] == "ru"
    en.pop("output_language")
    assert ru == en


def test_owner_prose_language_does_not_change_the_runs_routing_or_experiment_snapshot(tmp_path):
    import json
    from looplab.serve.llm_context import llm_settings
    snapshot = Settings(output_language="en", llm_model="run-model", max_nodes=7)
    (tmp_path / "config.snapshot.json").write_text(json.dumps(snapshot.model_dump(mode="json")), encoding="utf-8")
    class Store:
        def load_ui_settings(self):
            return {"output_language": "ru"}
        def resolve_settings(self, overrides):
            return Settings(**overrides)
        def resolve_snapshot_settings(self, config):
            return Settings(**config)
    settings = llm_settings(Store(), tmp_path)
    assert settings.output_language == "ru"
    assert settings.llm_model == "run-model" and settings.max_nodes == 7
    assert snapshot.output_language == "en"


def test_offline_report_does_not_claim_no_evaluations_when_only_excluded_results_exist():
    from looplab.core.models import Idea, Node, NodeStatus
    state = RunState(run_id="r", task_id="t", direction="min")
    state.nodes[1] = Node(id=1, operator="draft", idea=Idea(operator="draft", params={}), metric=0.125,
                          status=NodeStatus.evaluated, feasible=False)
    report = generate_report(state, None, output_language="ru")
    assert "Нет допустимого лучшего узла" in report["headline"]
    assert "оценено: 1" in report["summary"]
    assert state.nodes[1].metric == 0.125


def test_language_messages_is_idempotent_so_a_wrapped_client_never_doubles_the_directive():
    once = language_messages([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}], "ru")
    assert language_messages(once, "ru") == once
    assert once[0]["content"].count("[LoopLab output language") == 1
