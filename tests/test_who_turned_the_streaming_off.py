"""§391. Один счётчик над двумя разными неисправностями не называет ни одной.

`unstreamed_exposure` считает вызовы, ушедшие без SSE, и его же докстринг объясняет, откуда они
берутся: срыв потока деградирует вызов до не-SSE, а прокси этого стенда меряет 300 с на ВЕСЬ запрос,
то есть убивает именно эту повторную попытку. Чего счёт не говорит — ЧЬЯ это доля, а ответов два, и
починки у них разные:

* плечо, которое ПРОСИЛО поток и всё равно послало часть вызовов без него, деградировал ОТКАТ —
  `Settings.llm_stream_stall_fallback = False` заведён ровно под такой стенд и здесь не выставлен;
* плечо, у которого в приборе нет `LOOPLAB_LLM_STREAM=1`, не просило вовсе — это ловушка `.env`,
  про которую постоянный список предупреждает капслоком.

Померено 2026-09-09 по леджеру: 1489 из 47884 вызовов (3.1 %) без потока, 24 срезаны на стене в
300 с — 2.00 ч стенных часов и $4.6625 на не-потоковых телах. Внутри этого `remEE`, `accEE`,
`accPde` шли на 100 % без потока (запуск), а `capB3` 24 %, `freeB3` 23 %, `oldCK9` 20 % и
`remDL14` 4 % просили поток и были деградированы (откат). С начала счётчика — 448 из 459 на плечах,
которые ПРОСИЛИ.
"""
from __future__ import annotations

import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import check_money  # noqa: E402


def _probe(root, name, instrument_line=None):
    d = root / name
    d.mkdir(parents=True)
    if instrument_line is not None:
        (d / "INSTRUMENT.txt").write_text(
            f"probe:          {name}\n{instrument_line}\nlane:           0-10,48-58\n",
            encoding="utf-8")
    return d


def test_an_arm_that_asked_for_streaming_was_degraded(tmp_path):
    _probe(tmp_path, "asked", "LOOPLAB_LLM_STREAM=1")
    assert check_money.stream_intent(str(tmp_path), "asked") is True


def test_an_arm_launched_without_it_never_asked(tmp_path):
    _probe(tmp_path, "nope", "LOOPLAB_LLM_STREAM=false")
    assert check_money.stream_intent(str(tmp_path), "nope") is False
    _probe(tmp_path, "zero", "LOOPLAB_LLM_STREAM=0")
    assert check_money.stream_intent(str(tmp_path), "zero") is False


def test_the_engine_default_spelling_is_not_a_claim_either_way(tmp_path):
    """Прибор пишет эту строку дословно, когда переменная не задана; это НЕ «просили»."""
    _probe(tmp_path, "dunno", "LOOPLAB_LLM_STREAM=(unset -> engine default)")
    assert check_money.stream_intent(str(tmp_path), "dunno") is None


def test_an_arm_with_no_instrument_is_unknown_not_innocent(tmp_path):
    _probe(tmp_path, "bare", None)
    assert check_money.stream_intent(str(tmp_path), "bare") is None
    assert check_money.stream_intent(str(tmp_path), "not-even-a-directory") is None


def test_the_split_puts_each_arm_under_its_own_fix(tmp_path):
    """Фикстура СПОРИТ с багом: три плеча с ОДИНАКОВЫМ числом непотоковых вызовов и разной виной."""
    import collections
    _probe(tmp_path, "asked", "LOOPLAB_LLM_STREAM=1")
    _probe(tmp_path, "nope", "LOOPLAB_LLM_STREAM=false")
    _probe(tmp_path, "bare", None)
    got = check_money.unstreamed_by_whose_doing(
        str(tmp_path), collections.Counter({"asked": 7, "nope": 7, "bare": 7}))
    assert got == {"degraded": {"asked": 7}, "never_asked": {"nope": 7}, "unknown": {"bare": 7}}, got


def test_the_report_names_the_switch_rather_than_the_percentage():
    """§342: цифра без имени починки — сигнализация, которую нельзя прочитать.

    Проверяется САМА СЕНТЕНЦИЯ, а не упоминание в файле: первая версия грепала модуль на
    `llm_stream_stall_fallback=False`, а эта строка есть ещё и в докстринге `stream_intent` — и
    мутация, выкинувшая её из ПЕЧАТИ, пришла зелёной (§358 дословно).
    """
    lines = check_money.whose_doing_lines({"degraded": {"a": 5, "a2": 4, "a3": 2},
                                           "never_asked": {"b": 3}})
    assert any("llm_stream_stall_fallback=False" in ln and "a x5" in ln for ln in lines), lines
    assert any("the launch, not the fallback" in ln and "b x3" in ln for ln in lines), lines
    # EVERY arm up to the limit, in order -- naming only the worst one hides the spread, and the
    # mutation that cut the list to `[:1]` came back green while a single-arm fixture was all there was.
    degraded = next(ln for ln in lines if "DEGRADED" in ln)
    assert "a x5, a2 x4, a3 x2" in degraded, degraded
    assert degraded.startswith("11 of them"), degraded
    assert check_money.whose_doing_lines({"degraded": {"a": 1, "b": 1, "c": 1, "d": 1, "e": 1}},
                                         limit=2)[0].count(" x") == 2


def test_an_empty_group_says_nothing_at_all():
    """Пустая группа не должна печатать «0 of them on arm(s) that…» — это сигнал о ничём."""
    assert check_money.whose_doing_lines({"degraded": {}, "never_asked": {}, "unknown": {"c": 1}}) == []
    only = check_money.whose_doing_lines({"degraded": {"a": 1}, "never_asked": {}})
    assert len(only) == 1 and "a x1" in only[0], only


def test_the_live_probes_still_record_what_they_asked_for():
    """Якорь: если прибор перестанет писать строку, разбор молча схлопнется в «unknown»."""
    import os
    root = "/var/tmp/looplab-bench/model-probes"
    if not os.path.isdir(root):
        import pytest
        pytest.skip("no probe trees on this box")
    named = [p for p in os.listdir(root)
             if os.path.exists(os.path.join(root, p, "INSTRUMENT.txt"))]
    if not named:
        import pytest
        pytest.skip("no instruments yet")
    known = [p for p in named if check_money.stream_intent(root, p) is not None]
    assert len(known) >= 0.8 * len(named), f"only {len(known)} of {len(named)} instruments say"


# --- and the same claim through the tool the operator actually runs ------------------------------

def test_the_report_itself_carries_the_split(tmp_path):
    """Мутация «печать перестала звать сентенцию» была ЗЕЛЁНОЙ: я проверял функцию, а не вызов.

    Это тот же дефект, что и в проверке текста файла двумя тестами выше, только на этаж выше:
    правило держится, а путь до оператора — нет. Здесь `check_money.py` запускается целиком, с
    поддельным счётчиком на сокете, и в его СТДАУТЕ ищется строка.
    """
    import json
    import socket
    import subprocess
    import sys as _s
    import threading

    root = tmp_path / "bench"
    for probe, stream_line, cost in (("asked", "LOOPLAB_LLM_STREAM=1", 0.5),
                                     ("nope", "LOOPLAB_LLM_STREAM=false", 0.5)):
        run = root / "model-probes" / probe / "runs" / "t" / "run"
        run.mkdir(parents=True)
        (run / "spans.jsonl").write_text(json.dumps(
            {"name": "generation", "start": 2000.0, "duration_s": 1.0,
             "attributes": {"cost": cost}}) + "\n", encoding="utf-8")
        (root / "model-probes" / probe / "INSTRUMENT.txt").write_text(
            f"probe:          {probe}\n{stream_line}\n", encoding="utf-8")
    (root / "meter").mkdir(parents=True, exist_ok=True)
    rows = [{"ts": "3000", "arm": "asked", "cost": 0.5, "status": "200", "stream": False},
            {"ts": "3000", "arm": "nope", "cost": 0.5, "status": "200", "stream": False}]
    (root / "meter" / "meter.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        try:
            conn, _ = srv.accept()
            conn.recv(4096)
            body = json.dumps({"cost_usd": 1.0, "calls": 2}).encode()
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body))
            conn.sendall(body)
            conn.close()
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()
    got = subprocess.run(
        [_s.executable, str(BENCH / "check_money.py"), "--bench-root", str(root),
         "--port", str(port), "--since", "0"],
        capture_output=True, text=True, timeout=300)
    srv.close()
    out = got.stdout
    assert "llm_stream_stall_fallback=False" in out, out
    assert "asked x1" in out, out
    assert "never asked for streaming" in out and "nope x1" in out, out
