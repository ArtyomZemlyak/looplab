"""§397. `check_snapshot_refusals` — единственная проверка списка, чью тревогу никто не дёргал.

Аудит: подменить проверку на безусловное «держится» и посмотреть, покраснеет ли хоть один тест,
который её называет. Двадцать из двадцати одной краснеют. Эта — нет: её не называл НИ ОДИН тест, и
поломай её кто-нибудь, обход продолжал бы докладывать «оба отказа проверены».

У неё вдобавок ОБРАТНАЯ полярность (утверждение списка — «НЕ ПРОВЕРЕНО мной», поэтому `False`
значит «уже проверено»), так что молчаливая поломка выглядела бы как зелёная галочка на пункте,
который никто не проверял.

Здесь проверка натравливается на ПОДСТАВНОЙ `snapshot.sh`, нарушающий по одному правилу за раз.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks"
sys.path.insert(0, str(BENCH))

import sweep_claims  # noqa: E402

# Честный скрипт: отказ 1 на пропавшем назначении, 3 на занятом замке, ничего не пишет.
HONEST = r'''#!/usr/bin/env bash
set -u
dest="${SNAPSHOT_DEST:-}"
if [ ! -f "$dest/.persistent-store-id" ]; then
  echo "FATAL: $dest/.persistent-store-id is missing" >&2
  exit 1
fi
lock="$dest/.snapshot.lock"
exec 9>"$lock"
if ! flock -w "${SNAPSHOT_LOCK_WAIT_S:-1}" 9; then
  echo "busy" >&2
  exit 3
fi
exit 0
'''


def _bench(tmp_path, body: str) -> Path:
    root = tmp_path / "bench"
    d = root / "looplab" / "benchmarks"
    d.mkdir(parents=True)
    script = d / "snapshot.sh"
    script.write_text(body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return root


def test_the_honest_script_reports_the_claim_as_driven(tmp_path):
    """`False` здесь значит «утверждение списка устарело», то есть отказы проверены."""
    still_unchecked, detail = sweep_claims.check_snapshot_refusals(str(_bench(tmp_path, HONEST)))
    assert still_unchecked is False, detail
    assert "vanished destination -> exit 1" in detail, detail
    assert "held lock -> exit 3" in detail, detail


def test_a_vanished_destination_reported_as_success_is_caught(tmp_path):
    """Ровно та поломка 2026-08-29: пустой бэкап под кодом 0."""
    body = HONEST.replace('  exit 1\n', '  exit 0\n')
    still_unchecked, detail = sweep_claims.check_snapshot_refusals(str(_bench(tmp_path, body)))
    assert still_unchecked is True, detail
    assert "a vanished destination exited 0" in detail, detail


def test_a_held_lock_that_does_not_exit_three_is_caught(tmp_path):
    """«Занято» обязано отличаться от «сломано», иначе таймер запишет отпечаток вместо повтора."""
    body = HONEST.replace('  exit 3\n', '  exit 7\n')
    still_unchecked, detail = sweep_claims.check_snapshot_refusals(str(_bench(tmp_path, body)))
    assert still_unchecked is True, detail
    assert "a held lock exited 7, not 3" in detail, detail


def test_a_refused_run_that_still_wrote_something_is_caught(tmp_path):
    """Отказ, оставивший каталог, — это наполовину записанный снимок, худший из исходов."""
    body = HONEST.replace('  echo "busy" >&2\n', '  mkdir -p "$dest/20260909-000000"\n  echo "busy" >&2\n')
    still_unchecked, detail = sweep_claims.check_snapshot_refusals(str(_bench(tmp_path, body)))
    assert still_unchecked is True, detail
    assert "still wrote" in detail and "20260909-000000" in detail, detail


def test_no_script_is_refused_rather_than_passed(tmp_path):
    root = tmp_path / "bare"
    (root / "looplab" / "benchmarks").mkdir(parents=True)
    still_unchecked, detail = sweep_claims.check_snapshot_refusals(str(root))
    assert still_unchecked is False and "not on this box" in detail, detail


def test_the_check_is_named_by_this_file():
    """Якорь аудита §397: имя проверки должно встречаться в тесте, иначе она снова осиротеет."""
    assert "check_snapshot_refusals" in Path(__file__).read_text(encoding="utf-8")
