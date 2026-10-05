"""Russian copy for newly authored deterministic explanations, never persisted evidence.

Unknown text is an original diagnostic and stays intact. Captured values are substituted
verbatim: identifiers, numeric observations and user instructions must not be translated.
"""
import re

_RU = {
    "typed status observation is unavailable on this server":
        "На этом сервере недоступно подтверждённое чтение состояния.",
    "{0} changed run generation while it was being watched; the replacement was not followed automatically":
        "У наблюдаемой цели {0} сменилось поколение запуска. Монитор остановлен без перехода к новой цели.",
    "{0} changed experiment attempt while it was being watched; the replacement was not followed automatically":
        "У наблюдаемой цели {0} сменился номер попытки. Монитор остановлен без перехода к новой цели.",
    "{0} can no longer be reached": "Наблюдаемая цель больше недоступна: {0}.",
    "continuous work stopped after cycle {0}: {1}; automatic replay was refused because the previous cycle's outcome is not safely resumable":
        "Непрерывная работа остановлена после цикла {0}: {1}. Итог предыдущего цикла не позволяет безопасно продолжить автоматически.",
    "continuous work reached its {0}-cycle budget with checkpoint status {1}":
        "Непрерывная работа достигла лимита циклов ({0}). Состояние контрольной точки: {1}.",
    "continuous work completed": "Непрерывная работа завершена",
    "Narrative sections are model-authored advisory synthesis, not comparison outcomes.":
        "Текстовые разделы — пояснения модели, а не установленные результаты сравнения.",
    "{0} metric observation(s) lack a valid comparison measurement and are displayed without a cross-run rank.":
        "Наблюдений без подтверждённых условий сравнения: {0}. Они показаны без ранжирования запусков.",
    "Only {0} of {1} source run(s) were included in the bounded report evidence. The narrative and comparisons are incomplete.":
        "В отчёт включено запусков: {0} из {1}. Описание и сравнения неполны.",
    "{0} malformed input row(s) were excluded before report generation.":
        "До составления отчёта исключено некорректных строк: {0}.",
    "Some comparison detail was omitted by the bounded public report projection; the coverage receipt records the exact omitted counts.":
        "Часть деталей сравнения не вошла в ограниченный публичный отчёт; точное число пропусков записано в сведениях об охвате.",
    "No cross-run winner: the comparison population is incomplete ({0} of {1} source runs in bounded evidence).":
        "Лучший запуск не установлен: выборка сравнения неполна (в отчёте {0} из {1} исходных запусков).",
    "No cross-run winner is published: bounded public comparison detail was omitted.":
        "Лучший запуск не указан: часть деталей сравнения не вошла в публичный отчёт.",
    "No portfolio-wide winner is defined: exact comparison contracts form {0} independent cohort(s).":
        "Общий лучший запуск не установлен: протоколы сравнения образуют независимые группы ({0}).",
    "No winner in the exact comparison cohort: {0}.":
        "Лучший запуск в группе с одинаковым протоколом не установлен. Диагностика: {0}.",
    "Bounded evidence for {0}: {1} evidence runs · {2} with reports":
        "Охват отчёта «{0}»: запусков {1}, с отчётами {2}",
    "Generated without an LLM — only metrics/config, no synthesis.":
        "Составлено без модели: только метрики и конфигурация, без исследовательских выводов.",
    "No runs in {0}": "В разделе «{0}» нет запусков",
    "the watch reached its lifetime without being resolved":
        "Время действия монитора истекло до выполнения его условия.",
    "this watch was settled at startup": "Монитор остановлен при запуске сервера.",
    "monitor needs approval for an action; review its chat reply before re-arming":
        "Действию монитора нужно подтверждение. Прочитайте его ответ в чате перед повторным включением.",
    "monitor turn did not complete; review its outcome before re-arming":
        "Ход монитора не завершён. Проверьте итог перед повторным включением.",
    "reached its {0}-wake-up budget": "Исчерпан лимит срабатываний: {0}.",
    "the wake-up turn failed: {0}": "Ошибка при выполнении монитора. Диагностика: {0}.",
    "invalid monitor handoff: {0}": "Некорректное продолжение монитора. Диагностика: {0}.",
    "run {0} no longer exists, so this condition can never be met":
        "Запуск {0} больше не существует; условие монитора невозможно выполнить.",
    "{0} disappeared, so this exact condition can no longer be met":
        "Наблюдаемая цель исчезла: {0}. Условие больше невозможно выполнить.",
    "{0} never appeared in the {1} minute appearance window":
        "Наблюдаемая цель не появилась за {1} мин: {0}.",
    "continuous work reported a blocker: {0}": "Непрерывная работа приостановлена: {0}",
    "continuous work until it reports done or blocked":
        "Непрерывная работа до завершения или препятствия",
    "monitor completed": "Монитор завершён",
    "every {0}": "Каждые {0}",
    "in {0} (once)": "Через {0} (один раз)",
    "run {0} to reach {1}": "Ожидание состояния {1} у запуска {0}",
    "run {0} experiment {1} to reach {2}": "Ожидание состояния {2} у узла {1} запуска {0}",
    "run {0} experiment {1} stage {2} to reach {3}":
        "Ожидание состояния {3} на этапе {2} узла {1} запуска {0}",
}


def _pattern(key):
    parts = re.split(r"(\{\d+\})", key)
    indices = []
    pattern = ""
    for part in parts:
        if re.fullmatch(r"\{\d+\}", part):
            index = int(part[1:-1])
            pattern += rf"(?P<v{index}>.*?)" if index not in indices else rf"(?P=v{index})"
            indices.append(index)
        else:
            pattern += re.escape(part)
    return re.compile(pattern, re.DOTALL)


_PATTERNS = [(_pattern(key), value) for key, value in _RU.items() if "{0}" in key]


def authored_text(value, language):
    if language != "ru" or not isinstance(value, str):
        return value
    if value in _RU:
        return _RU[value]
    for pattern, translated in _PATTERNS:
        match = pattern.fullmatch(value)
        if match:
            return re.sub(r"\{(\d+)\}", lambda m: match.group(f"v{m[1]}"), translated)
    return value
