"""Small discovery view derived from the same obligations as the full progress read.

This is an observed prefix with an independent last-read engine lock probe,
never a candidate permit or proof of agent connection. Continue and finish are
separate choices; an expansion-only gate must not force research on an agent
that is trying to finish.
"""
from __future__ import annotations

from looplab.harness.phases import CHECKPOINT_DECISION_PHASES
from looplab.harness.checkpoint_history import allowed_verdicts

_RUN = "/api/runs/{run_id}"


def _step(code, title, detail, reads, *, action=None, phase_id=None):
    return {"code": code, "title": title, "detail": detail,
            "owner": "external_agent", "reads": reads,
            "action": action, "phase_id": phase_id}


def next_step(progress: dict, language: str = "en") -> dict:
    if language not in ("en", "ru"):
        raise ValueError("unsupported progress language")
    step = _next_step(progress, language)
    text = lambda en, ru: ru if language == "ru" else en
    step["language"] = language
    execution = progress["execution"]
    alive = execution["engine_running"]
    label = (text("running", "работает") if alive is True else
             text("stopped", "остановлен") if alive is False else text("unknown", "неизвестно"))
    if step["code"] == "answer_checkpoint" and alive is False:
        step["detail"] = (text("This is a recorded question from a stopped engine. Inspect state and saved command receipts before choosing explicit recovery or cancellation. Resume may re-evaluate the interrupted attempt and supersede this question; refresh after resume before answering. ",
                               "Это записанный вопрос остановленного движка. Перед явным восстановлением или отменой прочитайте состояние и сохранённые квитанции команд. Возобновление может повторить оценку прерванной попытки и заменить этот вопрос; после возобновления обновите данные перед ответом. ")
                          + step["detail"])
        step["reads"].append(f"GET {_RUN}/command-receipt?expected_generation=TOKEN&command_id={{command_id}}")
    step["detail"] += text(f" Engine last observed: {label}. Agent connection: not measured.",
                           f" Последнее наблюдение движка: {label}. Подключение агента не измеряется.")
    if progress["complete"] and progress["finish_pending_nodes"]:
        counts = execution["recorded_node_counts"]
        step["detail"] += text(f" Recorded activity: {counts['evaluating']} admitted, "
                           f"{counts['queued']} queued, {counts['building']} building, "
                           f"{counts['pending']} untracked.",
                           f" Записанная активность: допущено к оценке {counts['evaluating']}, "
                           f"в очереди {counts['queued']}, готовится {counts['building']}, "
                           f"не отслеживается {counts['pending']}.")
    return step


def _next_step(progress: dict, language: str = "en") -> dict:
    # Wording is selected inside the same branches; locale creates no second
    # decision tree and changes neither verdict authority nor read/action refs.
    text = lambda en, ru: ru if language == "ru" else en
    if not progress["complete"]:
        return _step("inspect_sources", text("Check incomplete sources", "Проверьте неполные источники"),
                     text("A missing receipt is not proof that no action occurred. Inspect source_health and ask the operator to recover damaged journals before trusting missing receipts.",
                          "Отсутствие квитанции не доказывает, что действие не выполнялось. Проверьте source_health и попросите оператора восстановить повреждённые журналы перед выводами об отсутствующих квитанциях."),
                     [f"GET {_RUN}/harness-progress?expected_generation=TOKEN", f"GET {_RUN}/events"])
    if progress["pending_checkpoint_count"]:
        q = progress["pending_checkpoints"][0]["question"]
        title = {"stage_check": text("Review the completed stage", "Проверьте завершённый этап"),
                 "train_monitor": text("Answer the training monitor", "Ответьте на вопрос о тренировке"),
                 "deadline_grace": text("Decide whether to extend the deadline", "Решите, продлевать ли время выполнения")}.get(
                     q["phase_id"], text("Answer the evaluation question", "Ответьте на вопрос оценки"))
        detail = text("Evaluation has an unanswered checkpoint. Read live state, the full question and its allowed verdicts; evaluator completion alone does not settle the node.",
                      "У оценки есть вопрос без ответа. Прочитайте актуальное состояние, полный вопрос и допустимые ответы; завершение процесса оценки само по себе не завершает эксперимент.")
        detail += text(" Allowed verdicts: ", " Допустимые ответы: ") + ", ".join(allowed_verdicts(q)) + "."
        if q["phase_id"] in ("train_monitor", "asha_live") and not q["kill_enabled"]:
            detail += text(" This checkpoint does not grant abort authority.", " Этот вопрос не разрешает досрочную остановку через abort.")
        if q.get("stop_refusal") == "objective_retargeted":
            detail += text(" The objective was retargeted; this ASHA curve remains on the task scale.",
                           " Целевая метрика изменена; эта кривая ASHA остаётся в шкале задачи.")
        if q["phase_id"] == "deadline_grace":
            detail += text(" While waiting for a verdict, the command may keep running; this wait has no automatic timeout. The runtime caps one extension starting after extend is consumed.",
                           " Пока ожидается ответ, команда может продолжать работу; автоматического таймаута этого ожидания нет. Среда выполнения ограничивает одно продление, отсчитываемое после применения extend.")
        if progress["recorded_lifecycle"]["paused"]:
            detail = text("Run is paused for new work; its recorded in-flight evaluation still has this checkpoint. Answering does not resume search. ",
                          "Новая работа приостановлена; у уже начатой оценки остаётся этот вопрос. Ответ не возобновляет поиск. ") + detail
        return _step("answer_checkpoint", title, detail,
                     [f"GET {_RUN}/state?observe_only=true", f"GET {_RUN}/harness-checkpoints?expected_generation=TOKEN"],
                     action=f"POST {_RUN}/harness-checkpoints",
                     phase_id=CHECKPOINT_DECISION_PHASES.get(q["phase_id"]))
    lifecycle = progress["recorded_lifecycle"]
    if any(lifecycle.values()):
        title = (text("Inspect recorded finish", "Проверьте завершённый запуск") if lifecycle["finished"] else
                 text("Inspect stop request", "Проверьте запрос остановки") if lifecycle["stop_requested"] else text("Run is paused", "Запуск на паузе"))
        detail = (text("The run has a recorded finish. Read result notices and the final report; no resume or further finalization is suggested.",
                       "В журнале записано завершение запуска. Прочитайте краткие итоги и финальный отчёт; возобновление или повторное завершение не предлагаются.")
                  if lifecycle["finished"] else
                  text("A stop was requested. Inspect state and command receipts to determine whether settlement and explicit finalization remain due.",
                       "Запрошена остановка. Проверьте состояние и квитанции команд, чтобы выяснить, нужны ли завершение текущей работы и явная финализация.")
                  if lifecycle["stop_requested"] else
                  text("The run is paused. Read state and command receipts, then explicitly choose resume or finalization if required.",
                       "Запуск на паузе. Прочитайте состояние и квитанции команд, затем явно выберите возобновление или завершение, если это требуется."))
        reads = [f"GET {_RUN}/state?observe_only=true", f"GET {_RUN}/events",
                 f"GET {_RUN}/command-receipt?expected_generation=TOKEN&command_id={{command_id}}"]
        if lifecycle["finished"]:
            reads.append(f"GET {_RUN}/result-notices?expected_generation=TOKEN")
        return _step("inspect_lifecycle", title,
                     detail + text(" Journal state does not certify engine or agent liveness.",
                                   " Состояние журнала не доказывает, что движок или агент работает."), reads)
    if progress["finish_pending_nodes"]:
        execution = progress["execution"]
        counts = execution["recorded_node_counts"]
        alive = execution["engine_running"]
        if alive is False:
            title = text("Engine stopped · inspect submitted experiments", "Движок остановлен · проверьте отправленные эксперименты")
            detail = text("No live engine owner was observed. Recorded evaluation starts do not mean training continues. Inspect state, checkpoints and original command receipts before choosing explicit recovery.",
                          "Работающий владелец движка не обнаружен. Запись о начале оценки не означает, что тренировка продолжается. Перед явным восстановлением проверьте состояние, вопросы оценки и исходные квитанции команд.")
        elif alive is None:
            title = text("Engine status unknown · inspect submitted experiments", "Состояние движка неизвестно · проверьте отправленные эксперименты")
            detail = text("The engine lock probe is inconclusive. Recorded node activity does not prove live training. Inspect state and checkpoints before choosing recovery.",
                          "Проверка блокировки движка не дала однозначного результата. Записанная активность не доказывает, что тренировка идёт. Перед восстановлением проверьте состояние и вопросы оценки.")
        else:
            title = (text("Inspect evaluations already started", "Проверьте уже начатые оценки") if counts["evaluating"] else
                     text("Submitted experiments are awaiting evaluation", "Отправленные эксперименты ожидают оценки") if counts["queued"] else
                     text("Inspect submitted experiments", "Проверьте отправленные эксперименты"))
            detail = text("LoopLab owns evaluation; poll checkpoints for questions and results. A live engine does not prove the agent is connected.",
                          "LoopLab выполняет оценку; опрашивайте checkpoints для вопросов и результатов. Работающий движок не доказывает подключение агента.")
        return _step("inspect_pending", title,
                     detail + text(" Finalization waits for settlement or explicit cancellation; further proposals have separate gates.",
                                   " Финализация ожидает завершения текущей работы или явной отмены; для новых предложений действуют отдельные требования."),
                     [f"GET {_RUN}/state?observe_only=true", f"GET {_RUN}/harness-checkpoints?expected_generation=TOKEN",
                      f"GET {_RUN}/command-receipt?expected_generation=TOKEN&command_id={{command_id}}"])
    # A dead owner between candidates leaves no pending node or recorded pause.
    # That absence cannot make the next proposal actionable. Keep an inconclusive
    # probe distinct from death and let recovery choose resume explicitly.
    alive = progress["execution"]["engine_running"]
    if alive is not True:
        title = (text("Engine stopped · inspect idle run", "Движок остановлен · проверьте ожидающий запуск") if alive is False else
                 text("Engine status unknown · inspect idle run", "Состояние движка неизвестно · проверьте ожидающий запуск"))
        detail = (text("No live engine owner was observed.", "Работающий владелец движка не обнаружен.") if alive is False else
                  text("The engine lock probe is inconclusive; this does not prove engine death.",
                       "Проверка блокировки движка не дала однозначного результата; это не доказывает гибель движка."))
        return _step("inspect_lifecycle", title,
                     detail + text(" No unsettled experiment is recorded. Inspect state and original command receipts before submitting another candidate or choosing explicit resume or finalization. Reading or reconnecting starts no work; no internal agent takeover is automatic.",
                                   " В журнале нет незавершённого эксперимента. Перед новым кандидатом, явным возобновлением или завершением прочитайте состояние и исходные квитанции команд. Чтение и переподключение не запускают работу; внутренний агент не подхватывает её автоматически."),
                     [f"GET {_RUN}/state?observe_only=true", f"GET {_RUN}/events",
                      f"GET {_RUN}/command-receipt?expected_generation=TOKEN&command_id={{command_id}}"],
                     phase_id="recovery")
    return _step("choose_direction", text("Choose the next experiment or finish", "Выберите следующий эксперимент или завершение"),
                 text("LoopLab waits for an explicit external decision between experiments; MCP disconnection does not trigger an internal agent takeover. Use measured evidence to choose. The two paths below have different obligations; policy advice does not submit a candidate.",
                      "Между экспериментами LoopLab ждёт явного решения внешнего агента; отключение MCP не включает внутреннего агента. Выбирайте по измеренным доказательствам. Для продолжения и завершения действуют разные требования; совет стратегии не отправляет кандидата."),
                 [f"GET {_RUN}/state?observe_only=true", f"GET {_RUN}/harness-contract"])


def brief(progress: dict) -> dict:
    """Omit observations, reports and history payloads before MCP's response cap.

    Counts, source health and pagination receipts survive the projection. Read the
    full endpoint for history and checkpoints for authoritative verdict authority.
    """
    keys = ("generation", "run_uid", "event_seq", "at_node", "evidence_revision",
            "complete", "source_health", "recorded_lifecycle", "execution", "next_step",
            "candidate_blockers_if_expanding", "candidate_decisions_per_idea",
            "candidate_requirements", "finish_reviews_due", "finish_report_due",
            "pending_checkpoint_count")
    result = {key: progress[key] for key in keys}
    if "agent_activity" in progress:
        result["agent_activity"] = progress["agent_activity"]
    nodes = progress["finish_pending_nodes"]
    result.update(finish_pending_nodes=nodes[:20], finish_pending_node_count=len(nodes),
                  finish_pending_nodes_truncated=len(nodes) > 20)
    result["pending_checkpoints"] = [{key: row["question"][key] for key in
        ("checkpoint_id", "node_id", "node_generation", "claim_seq", "phase_id")}
        for row in progress["pending_checkpoints"][:20]]
    result["pending_checkpoints_truncated"] = progress["pending_checkpoint_count"] > 20
    result["history"] = {kind: {key: page[key] for key in
        ("total", "offset", "limit", "has_more")} for kind, page in progress["history"].items()}
    preview = progress["policy_preview"]
    result["policy_preview"] = {key: preview[key] for key in
        ("policy", "policy_source", "total_actions", "meaning")}
    result["details"] = {"history": f"GET {_RUN}/harness-progress?expected_generation=TOKEN&offset=0&limit=20",
                         "questions": f"GET {_RUN}/harness-checkpoints?expected_generation=TOKEN",
                         "phase": "MCP phases / phase_info before each decision"}
    result["refresh"] = "Refresh after events or responses; use the current /state generation. No automatic retry, resume or candidate submission."
    return result
