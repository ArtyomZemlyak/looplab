"""A FAILED CANARY'S OWN ACCOUNT (doc 69 §3.5, 69.7; `Settings.canary_failure_account`).

The failure text of a failed eval canary — the repair prompt, the triage judge's `err`,
`node_repaired.error_in`, the terminal's `error` — was the last 500 characters of the canary
result's stderr: the canary header (what a canary is, how it failed, where its logs are), the
canary's stderr and an engine footer run together. A traceback of a few lines cut the header off,
the footer spent a fifth of the window, and stdout was never read at all. `minionerec-backbones-v10`
node 0 was triaged for 44 min and 7.0 M tokens over the wrapper's own text while a one-line
`EADDRINUSE` sat in the canary's log.

ON, the text is `RunResult.canary_account`: the header whole, then the tails of the canary's OWN
stdout and stderr, each labelled — every stream redacted WHOLE before it is cut. OFF is the
historical tail, byte for byte, and a pre-field snapshot resumes OFF. Everything below the pure
rule drives the REAL `Engine._evaluate` over a real command eval (the fixtures of
`tests/test_eval_canary.py`).
"""
from __future__ import annotations

from looplab.core.config import (LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings,
                                 settings_from_snapshot)
from looplab.core.redact import redact_output_tail
from looplab.engine.eval_canary import CANARY_ACCOUNT_TAIL_CHARS, canary_failure_result
from looplab.engine.options import EngineOptions
from looplab.runtime.sandbox import RunResult
from tests.test_eval_canary import _Dev, _Researcher, _engine, _evaluate, _seed, _terminals

_HEADER = "[eval canary] The canary preflight FAILED — the canary exited 1."
_LOGS = "Canary logs: /run/canary/node_0"


def _failed(stdout: str, stderr: str, **kw) -> RunResult:
    res = RunResult(exit_code=1, stdout=stdout, stderr=stderr, metric=None, timed_out=False)
    return canary_failure_result(res, detail="the canary exited 1", log_dir="/run/canary/node_0",
                                 env_names=["LOOPLAB_CANARY"], **kw)


_TOKEN = "sk-live-A9fQ2xLm7ZpR4tVw8YbN1cJdKe"


def _straddling_stream() -> str:
    """`export TOKEN=<token>` then a newline and filler, sized so the tail cut lands ELEVEN
    characters into the token — `tests/test_scored_output_evidence.py`'s straddle, for this window."""
    prefix = "export TOKEN="
    filler = CANARY_ACCOUNT_TAIL_CHARS - (len(_TOKEN) - 11) - 1
    return prefix + _TOKEN + "\n" + "y" * filler


def _failure_text(err: str) -> str:
    """The failure text the judge was handed, without the kind line the triage call puts first."""
    return err.split("\n", 1)[1] if err.startswith("[failure kind:") else err


# ----------------------------------------------------------------------------------- the rule
def test_the_account_is_the_header_then_both_streams_labelled():
    out = _failed("step 1\nOSError: [Errno 98] Address already in use\n", "Traceback: boom\n")
    account = out.canary_account
    assert account.startswith(_HEADER) and _LOGS in account.split("\n", 1)[0]
    stdout_at = account.index("[the canary's stdout]\n")
    stderr_at = account.index("[the canary's stderr]\n")
    assert stdout_at < account.index("Address already in use") < stderr_at
    assert account.endswith("Traceback: boom")
    # The historical fields are untouched: the account rides BESIDE them.
    assert out.stderr.startswith(_HEADER) and out.stdout.startswith("step 1")


def test_each_stream_is_cut_to_its_tail_and_says_how_much_it_shows():
    long_out = "o" * 4000 + "LAST-STDOUT"
    out = _failed(long_out, "e" * 9000 + "LAST-STDERR").canary_account
    shown = CANARY_ACCOUNT_TAIL_CHARS
    assert f"[the canary's stdout, its last {shown:,} of {len(long_out):,} characters]" in out
    assert f"[the canary's stderr, its last {shown:,} of {9011:,} characters]" in out
    assert "LAST-STDOUT" in out and out.endswith("LAST-STDERR")
    assert len(out) < 4000, "two tails and the header stay under the priced 4,000 characters"


def test_an_empty_stream_is_said_not_dropped():
    out = _failed("", "   \n").canary_account
    assert "[the canary's stdout was empty]" in out and "[the canary's stderr was empty]" in out


def test_the_expired_header_leads_the_account_too():
    out = _failed("x", "y", expired=True).canary_account
    assert out.startswith("[eval canary] The canary preflight TIMED OUT")


def test_no_result_is_two_empty_streams():
    out = canary_failure_result(None, detail="the canary produced no result", log_dir="/l",
                                env_names=["LOOPLAB_CANARY"]).canary_account
    assert "[the canary's stdout was empty]" in out and "[the canary's stderr was empty]" in out


def test_each_stream_is_redacted_whole_before_it_is_cut():
    """`evaluate._redacted_tail`'s order: a secret STRADDLING the cut must reach the redactor whole.
    Cut first, `sk-live-A9f…` loses its prefix and the 23-character remainder matches no rule — it
    survives even the funnel `_eval_failure_text` applies afterwards. MUTATION: cut before
    redacting -> the fragment is in the account."""
    stream = _straddling_stream()
    assert stream[-CANARY_ACCOUNT_TAIL_CHARS:].startswith("Q2xLm7ZpR4tVw8YbN1cJdKe\n")
    red = lambda text: redact_output_tail(text, entropy=False)  # noqa: E731 — the engine's funnel
    fragment = "Q2xLm7ZpR4tVw8YbN1cJdKe"
    assert fragment not in _failed(stream, "", redact=red).canary_account
    assert fragment not in _failed("", stream, redact=red).canary_account
    assert fragment in red(_failed(stream, "").canary_account), "the order is what protects it"


# ----------------------------------------------------------------------------------- settings
def test_on_for_new_runs_off_for_a_pre_field_snapshot_and_off_at_every_constructor():
    """MUTATION: drop the LEGACY row -> the pre-field snapshot reads ON."""
    assert Settings().canary_failure_account is True
    assert EngineOptions().canary_failure_account is False
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["canary_failure_account"] is False
    legacy = Settings().masked_snapshot()
    for field in LEGACY_CONFIG_SNAPSHOT_DEFAULTS:
        legacy.pop(field, None)
    legacy.pop("config_snapshot_schema", None)
    assert settings_from_snapshot(legacy).canary_failure_account is False
    assert settings_from_snapshot(Settings().masked_snapshot()).canary_failure_account is True
    assert EngineOptions.from_settings(Settings()).canary_failure_account is True


# ------------------------------------------------------------------ driven through the real engine
def _addr_in_use_script() -> str:
    """The node-0 shape: the canary says what went wrong on STDOUT, then its stderr fills with a
    long wrapper traceback — so a 500-character stderr tail holds neither the header nor the cause.
    The full eval would score; the tests below only ever reach the canary."""
    return ("import os, sys\n"
            "if os.environ.get('LOOPLAB_CANARY') == '1':\n"
            "    print('rank 0: OSError: [Errno 98] Address already in use (port 29500)')\n"
            "    sys.stderr.write('torch.distributed.elastic.ChildFailedError: ' + 'x' * 2000 + '\\n')\n"
            "    sys.exit(1)\n"
            "print('METRIC: 0.5')\n")


class _Judge(_Researcher):
    def triage_crash(self, node, error, attempt, **kw):
        self.triaged.append(error)
        return {"action": "abandon", "rationale": "stop"}


def _repair_texts(tmp_path, *, on: bool):
    code = _addr_in_use_script()
    judge = _Judge()
    eng = _engine(tmp_path / ("on" if on else "off"), _Dev(code), researcher=judge,
                  canary_failure_account=on)
    _seed(eng, code)
    evs = _evaluate(eng)
    (term,) = _terminals(evs)
    return judge.triaged, term


def test_the_judge_and_the_record_read_the_canary_s_own_account(tmp_path):
    """MUTATION: read `res.stderr[-500:]` for a canary too (drop the account branch) -> the cause,
    which only stdout carried, never reaches the judge."""
    triaged, term = _repair_texts(tmp_path, on=True)
    assert triaged, "the failed canary was triaged"
    err = _failure_text(triaged[0])
    assert err.startswith("[eval canary] The canary preflight FAILED"), err[:200]
    assert "Canary logs:" in err
    assert "Address already in use" in err
    assert "[the canary's stdout]" in err and "[the canary's stderr, its last" in err
    assert term.type == "node_failed" and "Address already in use" in term.data["error"]


def test_off_is_the_historical_tail_and_it_misses_the_cause(tmp_path):
    """The defect, driven: OFF, the judge reads 500 characters of wrapper traceback and footer —
    no header, no log path, and not the one line that says what went wrong."""
    triaged, term = _repair_texts(tmp_path, on=False)
    err = _failure_text(triaged[0])
    assert "Address already in use" not in err and "Canary logs:" not in err
    assert err.endswith("the full evaluation was not started)"), err[-120:]
    assert len(err) <= 500


def test_a_straddling_secret_on_the_canary_s_stdout_never_reaches_the_judge(tmp_path):
    """The engine path passes its own funnel (`Engine._redact`) into the account. MUTATION: build
    the canary's result without `redact=` -> the token's tail reaches the triage text."""
    code = ("import os, sys\n"
            "if os.environ.get('LOOPLAB_CANARY') == '1':\n"
            f"    sys.stdout.write({_straddling_stream()!r})\n"
            "    sys.exit(1)\n"
            "print('METRIC: 0.5')\n")
    judge = _Judge()
    eng = _engine(tmp_path / "run", _Dev(code), researcher=judge, canary_failure_account=True)
    _seed(eng, code)
    _evaluate(eng)
    assert judge.triaged and "Q2xLm7ZpR4tVw8YbN1cJdKe" not in judge.triaged[0]
    # Masked WHOLE, the stream shrinks under the tail window, so its head survives: the redactor
    # saw the token before any cut did.
    assert "export TOKEN=***" in judge.triaged[0]
