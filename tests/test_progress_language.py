"""Localization preserves live evidence, checkpoint authority and recovery reads."""
from copy import deepcopy

import httpx
import pytest

from looplab.harness.mcp_server import HarnessAPI
from looplab.harness.progress_summary import next_step
from looplab.serve.run_commands import run_generation_token
from tests.test_external_progress import _run


def _progress():
    return {"complete": True, "pending_checkpoint_count": 0, "pending_checkpoints": [],
            "finish_pending_nodes": [],
            "recorded_lifecycle": {"paused": False, "finished": False, "stop_requested": False},
            "execution": {"engine_running": True,
                          "recorded_node_counts": {"evaluating": 0, "queued": 0, "building": 0, "pending": 0}}}


def _pair(progress):
    before = deepcopy(progress)
    en, ru = next_step(progress), next_step(progress, "ru")
    assert progress == before, "a localized read must not mutate progress or answer a question"
    assert en["language"] == "en" and ru["language"] == "ru"
    assert en["title"] != ru["title"] and en["detail"] != ru["detail"]
    assert {k: v for k, v in en.items() if k not in ("title", "detail", "language")} == {
        k: v for k, v in ru.items() if k not in ("title", "detail", "language")}
    assert "Подключение агента не измеряется" in ru["detail"]
    return en, ru


@pytest.mark.parametrize("alive", [True, False, None])
@pytest.mark.parametrize("activity", [None, "evaluating", "queued", "building", "pending"])
def test_engine_observations_never_turn_reconnect_into_permission(alive, activity):
    progress = _progress()
    progress["execution"]["engine_running"] = alive
    if activity:
        progress["finish_pending_nodes"] = [3]
        progress["execution"]["recorded_node_counts"][activity] = 1
    en, ru = _pair(progress)
    if activity:
        assert en["code"] == "inspect_pending"
        assert "Финализация ожидает завершения текущей работы или явной отмены" in ru["detail"]
        assert "Записанная активность:" in ru["detail"]
        if alive is False:
            assert "не означает, что тренировка продолжается" in ru["detail"]
        if alive is None:
            assert "не доказывает, что тренировка идёт" in ru["detail"]
    elif alive is not True:
        assert "не подхватывает её автоматически" in ru["detail"]
        assert "/commands/{command_id}" not in " ".join(ru["reads"])
    else:
        assert "разные требования" in ru["detail"]
        assert ru["action"] is None


@pytest.mark.parametrize("lifecycle", ["paused", "finished", "stop_requested"])
def test_finished_and_paused_have_distinct_continuation_guidance(lifecycle):
    progress = _progress()
    progress["recorded_lifecycle"][lifecycle] = True
    _, ru = _pair(progress)
    assert "Состояние журнала не доказывает" in ru["detail"]
    if lifecycle == "finished":
        assert "возобновление или повторное завершение не предлагаются" in ru["detail"]
        assert any("/result-notices?" in ref for ref in ru["reads"])
    elif lifecycle == "paused":
        assert "явно выберите возобновление или завершение" in ru["detail"]
    else:
        assert "нужны ли завершение текущей работы и явная финализация" in ru["detail"]


@pytest.mark.parametrize("phase,kill", [("stage_check", False), ("deadline_grace", False),
    ("train_monitor", False), ("train_monitor", True), ("asha_live", False), ("asha_live", True)])
def test_checkpoint_translation_keeps_allowed_verdicts_and_stopped_engine_warning(phase, kill):
    progress = _progress()
    progress["execution"]["engine_running"] = False
    progress["recorded_lifecycle"]["paused"] = True
    progress["pending_checkpoint_count"] = 1
    question = {"phase_id": phase, "kill_enabled": kill}
    if phase == "asha_live" and not kill:
        question["stop_refusal"] = "objective_retargeted"
    progress["pending_checkpoints"] = [{"question": question}]
    en, ru = _pair(progress)
    assert ru["code"] == "answer_checkpoint"
    assert "Ответ не возобновляет поиск" in ru["detail"]
    assert "после возобновления обновите данные перед ответом" in ru["detail"]
    assert "command-receipt?" in " ".join(ru["reads"])
    verdicts = en["detail"].split("Allowed verdicts: ")[1].split(".")[0]
    assert f"Допустимые ответы: {verdicts}." in ru["detail"]
    if phase in ("train_monitor", "asha_live"):
        assert ("не разрешает досрочную остановку через abort" in ru["detail"]) == (not kill)
    if phase == "deadline_grace":
        assert "команда может продолжать работу" in ru["detail"]
        assert "автоматического таймаута этого ожидания нет" in ru["detail"]
        assert "после применения extend" in ru["detail"]
    if question.get("stop_refusal"):
        assert "эта кривая ASHA остаётся в шкале задачи" in ru["detail"]
    progress["complete"] = False
    _, damaged = _pair(progress)
    assert damaged["code"] == "inspect_sources"
    assert damaged["action"] is None and "Допустимые ответы" not in damaged["detail"]


def test_http_and_typed_mcp_read_one_locale_without_changing_events(tmp_path, monkeypatch):
    monkeypatch.setattr("looplab.engine.run_lifecycle.engine_liveness", lambda _: False)
    rd, store, client = _run(tmp_path)
    generation = run_generation_token(store.read_all())
    before = (rd / "events.jsonl").read_bytes()
    args = {"expected_generation": generation, "language": "ru"}
    full = client.get("/api/runs/demo/harness-progress", params=args)
    compact = client.get("/api/runs/demo/harness-progress", params={**args, "brief": True})
    assert full.status_code == compact.status_code == 200
    assert full.json()["next_step"] == compact.json()["next_step"]
    assert compact.json()["next_step"]["title"] == "Движок остановлен · проверьте ожидающий запуск"
    assert full.headers["cache-control"] == "no-store"
    assert client.get("/api/runs/demo/harness-progress", params={**args, "language": "de"}).status_code == 422
    assert client.get("/api/runs/demo/harness-progress", params={**args, "expected_generation": "0" * 64}).status_code == 409
    seen = []
    def handler(request):
        seen.append(request)
        reply = client.get(request.url.raw_path.decode())
        return httpx.Response(reply.status_code, json=reply.json())
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(handler))
    result = api.run_progress("demo", generation, language="ru")
    assert result["status"] == 200 and not result.get("code")
    assert result["body"]["next_step"] == compact.json()["next_step"]
    assert len(seen) == 1 and seen[0].method == "GET"
    assert seen[0].url.params["language"] == "ru"
    with pytest.raises(ValueError):
        api.run_progress("demo", generation, language="RU")
    assert len(seen) == 1 and (rd / "events.jsonl").read_bytes() == before


@pytest.mark.parametrize("stamp", [None, 0, [], "de", "en"])
def test_mcp_rejects_malformed_or_wrong_locale_stamp_without_retry(stamp):
    from tests.test_harness_connection import GEN, progress
    page = progress()
    page["next_step"]["language"] = stamp
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=page)
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(handler))
    result = api.run_progress("demo", GEN, language="ru")
    assert result["outcome"] == "unavailable" and "body" not in result
    assert result["reason"] == "invalid_progress" and len(seen) == 1
