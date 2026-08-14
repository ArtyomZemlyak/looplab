"""B3 secret-leak redaction over persisted output tails."""
from __future__ import annotations

import json

import pytest

from looplab.core.redact import (redact_env_values, redact_output_tail, redact_persisted_text,
                                 redact_secrets, secret_env_values)


def test_redacts_openai_style_key():
    out = redact_secrets("error: key sk-abcdefABCDEF0123456789 failed")
    assert "sk-abcdefABCDEF0123456789" not in out and "sk-***" in out


def test_redacts_aws_and_github_and_bearer():
    assert "AKIA***" in redact_secrets("AKIAIOSFODNN7EXAMPLE")
    assert "gh***" in redact_secrets("token ghp_0123456789abcdefghijABCDEFGHIJ012345")
    assert "***" in redact_secrets("Authorization: Bearer abcdef0123456789ABCDEF")


def test_redacts_key_value_assignment():
    out = redact_secrets("API_KEY=supersecretvalue123")
    assert "supersecretvalue123" not in out


def test_redacts_credentials_inside_url_query_and_fragment():
    raw = "https://example.test/v1?mode=fast&token=short-secret#api_key=another-secret"
    out = redact_secrets(raw, entropy=False)
    assert "mode=fast" in out
    assert "short-secret" not in out and "another-secret" not in out
    assert "token=***" in out and "api_key=***" in out


def test_high_entropy_token_masked_but_words_kept():
    # a long random base64-ish token is masked; ordinary prose is left alone.
    out = redact_secrets("result ok; blob " + "aZ9k2Lp7qW3xYt5Rb8Nc1Vd6Mf0Gh4J", min_len=24)
    assert "***REDACTED***" in out
    assert redact_secrets("the quick brown fox jumps over the lazy dog") == \
        "the quick brown fox jumps over the lazy dog"


def test_empty_is_noop():
    assert redact_secrets("") == "" and redact_secrets(None) is None


def test_engine_redacts_persisted_tail(tmp_path):
    # End-to-end: a solution that prints a secret has it masked in the persisted stdout_tail.
    import anyio
    from looplab.core.models import Idea
    from looplab.engine.orchestrator import Engine
    from looplab.search.policy import GreedyTree
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.adapters.toytask import ToyTask
    from pathlib import Path

    task = ToyTask.load(Path(__file__).resolve().parents[1] / "examples" / "toy_task.json")

    class _LeakyDev:
        def implement(self, idea):
            return ("import json\nprint('leaking sk-abcdefABCDEF0123456789TOKEN')\n"
                    "print(json.dumps({'metric': 0.5}))\n")

    class _Stub:
        def propose(self, state, parent):
            return Idea(operator="draft", params={"x": 1.0, "y": 1.0})

    eng = Engine(tmp_path / "r", task=task, researcher=_Stub(), developer=_LeakyDev(),
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 redact_output=True)
    state = anyio.run(eng.run)
    tails = " ".join(n.stdout_tail for n in state.nodes.values())
    assert "sk-abcdefABCDEF0123456789TOKEN" not in tails and "sk-***" in tails


# --- redact: compound credential key names are masked --------------------------------------------

def test_redact_masks_compound_secret_keys():
    out = redact_secrets("env={'AWS_SECRET_ACCESS_KEY': 'abcdefabcdefabcd'}", entropy=False)
    assert "abcdefabcdefabcd" not in out and "***" in out
    out2 = redact_secrets("db_password=supersecretvalue", entropy=False)
    assert "supersecretvalue" not in out2


def test_redact_modern_key_prefixes():
    assert "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX" not in redact_secrets("key sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX")
    assert "***" in redact_secrets("token=github_pat_ABCDEFGHIJKLMNOPQRSTUV")
    assert "hf_ABCDEFGHIJKLMNOPQRSTUV" not in redact_secrets("hf_ABCDEFGHIJKLMNOPQRSTUV")


def test_benign_token_fields_not_overmasked():
    # Field NAMES that merely contain a credential substring ("token") but are benign diagnostics
    # must NOT be masked — operators rely on these in the persisted stdout tail.
    assert redact_secrets("tokenizer=gpt2") == "tokenizer=gpt2"
    assert redact_secrets("max_tokens: 1024") == "max_tokens: 1024"
    assert redact_secrets("usage: total_tokens=512") == "usage: total_tokens=512"


def test_real_secret_fields_still_redacted():
    # The broad key-name match is preserved: genuine secret fields are still masked.
    for s in ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIKDENGbPxRfiCY",
              "db_password=hunter2hunter2",
              "MY_API_KEY=abcd1234efgh"):
        masked = redact_secrets(s)
        secret = s.split("=", 1)[1]
        assert "***" in masked and secret not in masked


def test_short_quoted_and_authorization_credentials_are_fully_redacted():
    cases = [
        ("password=abc", "abc"),
        ("api_key=xyz", "xyz"),
        ("password: 'my secret'", "my secret"),
        ("Authorization: Basic dTpw", "dTpw"),
        ("Bearer abcdefghijklmnop+SECRET", "abcdefghijklmnop+SECRET"),
    ]
    for raw, secret in cases:
        persisted = redact_persisted_text(raw, max_chars=1_000, entropy=False)
        assert secret not in persisted and "***" in persisted


def test_fat_arrow_secret_assignment_is_fully_masked():
    # Regression: the assignment operator once accepted only `:`/`=`, so a fat-arrow (`secret => value`,
    # Ruby/JS/Perl hashrocket) let `[:=]` consume just the `=`; the value class then captured the lone
    # `>` and the real credential after the arrow leaked verbatim — a fail-OPEN in a security redactor.
    for raw in ("secret => hunter2tok", "api_key => abc123def", "password => letmein",
                "TOKEN =>topsecret", "access_key => 'quoted secret'"):
        out = redact_secrets(raw, entropy=False)
        leaked = raw.split("=>", 1)[1].strip().strip("'")
        assert leaked not in out and "***" in out, (raw, out)


def test_fat_arrow_does_not_overmask_non_secret_keys():
    # The masking callbacks gate on `is_secret_key_name`, so a non-secret fat-arrow is left intact and a
    # `>=` comparison is never treated as an assignment.
    assert redact_secrets("count => 42", entropy=False) == "count => 42"
    assert redact_secrets("width >= 1024", entropy=False) == "width >= 1024"
    assert redact_secrets("tokenizer => gpt2", entropy=False) == "tokenizer => gpt2"


def test_compound_key_scanner_handles_large_nonsecret_input_without_backtracking_blowup():
    raw = "ordinary_identifier=" + "x" * 100_000 + " password=z"
    persisted = redact_persisted_text(raw, max_chars=110_000, entropy=False)
    assert persisted.endswith("password=***") and "ordinary_identifier=" in persisted


# --- C2: the persisted tail is redacted on the DEFAULT path -------------------------------------
#
# Until 2026-08-14 the WHOLE tail redactor was gated on `redact_output`, which defaults to False —
# so `test_engine_redacts_persisted_tail` above proves only that the opt-in posture works, and the
# shipped default persisted `print(api_key)` verbatim into `events.jsonl`, the trace, the UI node
# detail and every export. These drive the split: shapes + the operator's own env VALUES always,
# entropy only when asked.

_ENV_SECRET = "hunter2-correct-horse"          # a real credential SHAPE recognises nothing here
_ENV_BENIGN = "/mnt/looplab-c2-fixture/data"   # same length class, non-secret NAME
_ENTROPY_BLOB = "aZ9k2Lp7qW3xYt5Rb8Nc1Vd6Mf0Gh4J"   # 31 chars, what a data hash looks like


def test_secret_env_values_screens_by_name_and_by_value():
    env = {"C2FIXTURE_DB_PASSWORD": _ENV_SECRET,     # secret NAME
           "C2FIXTURE_DATA_ROOT": _ENV_BENIGN,       # benign NAME + benign VALUE
           "C2FIXTURE_DATABASE_URL": "postgres://u:pw123456@h/db",   # benign NAME, secret VALUE
           "C2FIXTURE_LLM_BASE_URL": "https://host.test/v1",         # a plain endpoint stays out
           "C2FIXTURE_API_KEY": "yes"}               # screened NAME, too short to be a credential
    values = secret_env_values(env)
    assert _ENV_SECRET in values
    assert "postgres://u:pw123456@h/db" in values
    assert _ENV_BENIGN not in values
    assert "https://host.test/v1" not in values
    assert "yes" not in values                       # the _MIN_SECRET_ENV_VALUE floor
    # longest first, so a value that PREFIXES another cannot mask it into a recognisable remainder
    assert values == sorted(values, key=lambda s: (-len(s), s))


def test_redact_env_values_masks_the_literal_and_leaves_the_benign_one():
    env = {"C2FIXTURE_DB_PASSWORD": _ENV_SECRET, "C2FIXTURE_DATA_ROOT": _ENV_BENIGN}
    out = redact_env_values(f"connect {_ENV_SECRET} from {_ENV_BENIGN}", env)
    assert _ENV_SECRET not in out and "***REDACTED_ENV***" in out
    assert _ENV_BENIGN in out


def test_redact_output_tail_gates_only_the_entropy_pass():
    env = {"C2FIXTURE_DB_PASSWORD": _ENV_SECRET}
    text = f"key sk-abcdefABCDEF0123456789 pw {_ENV_SECRET} blob {_ENTROPY_BLOB}"
    off = redact_output_tail(text, entropy=False, env=env)
    assert "sk-abcdefABCDEF0123456789" not in off and "sk-***" in off   # shape: always
    assert _ENV_SECRET not in off                                       # env value: always
    assert _ENTROPY_BLOB in off                                         # entropy: gated off
    on = redact_output_tail(text, entropy=True, env=env)
    assert _ENTROPY_BLOB not in on and "***REDACTED***" in on           # gated on
    assert redact_output_tail("", entropy=True, env=env) == ""


def _run_leaky_engine(tmp_path, printed, **engine_kw):
    """Drive a REAL run: a real subprocess sandbox evaluates real generated code that prints
    `printed`, and the DURABLE event log is read back off disk. Nothing here reads the folded
    in-memory state — a tail that is clean only in RAM is not the property under test."""
    import anyio
    from pathlib import Path
    from looplab.core.models import Idea
    from looplab.engine.orchestrator import Engine
    from looplab.search.policy import GreedyTree
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.adapters.toytask import ToyTask

    task = ToyTask.load(Path(__file__).resolve().parents[1] / "examples" / "toy_task.json")

    class _LeakyDev:
        def implement(self, idea):
            return ("import json\n"
                    + "".join(f"print({line!r})\n" for line in printed)
                    + "print(json.dumps({'metric': 0.5}))\n")

    class _Stub:
        def propose(self, state, parent):
            return Idea(operator="draft", params={"x": 1.0, "y": 1.0})

    run_dir = tmp_path / "r"
    eng = Engine(run_dir, task=task, researcher=_Stub(), developer=_LeakyDev(),
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 **engine_kw)
    anyio.run(eng.run)
    rows = [json.loads(l) for l in (run_dir / "events.jsonl").read_text().splitlines() if l.strip()]
    tails = [r.get("data", {}).get("stdout_tail", "") for r in rows
             if r.get("type") == "node_evaluated"]
    assert tails and any(t.strip() for t in tails), "no persisted stdout_tail to inspect"
    # Every CAPTURED-OUTPUT field in the whole log, not just this node's stdout tail: the six
    # `Engine._redact` call sites write to these four keys, and the point of a funnel is that none
    # of them can drift out of it.
    #
    # Deliberately NOT the raw file. `node_created.code` carries the generated SOURCE verbatim, and
    # a secret this test planted as a string LITERAL is in it — measured, on the first run of this
    # test. That is correct and must stay: the code is the record of what actually ran, redacting it
    # would corrupt the reproduction, and `docs/guide/configuration.md` states that code/artifacts
    # are outside the redaction boundary. C2 is about CAPTURED PROCESS OUTPUT, which is the channel
    # that carries bytes nobody chose to persist. Asserting over the raw file would silently be
    # asserting a property this repo has decided against.
    keys = ("stdout_tail", "stderr_tail", "error", "reason")
    captured = [str(r.get("data", {}).get(k, "")) for r in rows for k in keys]
    return " ".join(tails), " ".join(captured)


def test_default_run_does_not_persist_a_shaped_secret_in_the_stdout_tail(tmp_path):
    # NOTE: no `redact_output=` — this is the SHIPPED default, which is the whole point.
    tail, captured = _run_leaky_engine(tmp_path, ["leaking sk-abcdefABCDEF0123456789TOKEN"])
    assert "sk-abcdefABCDEF0123456789TOKEN" not in tail and "sk-***" in tail
    assert "sk-abcdefABCDEF0123456789TOKEN" not in captured   # no captured-output field, not just this one


def test_default_run_does_not_persist_an_operator_env_secret(tmp_path, monkeypatch):
    """The class no regex can catch: this box's OWN `.env` value, with no credential shape at all.

    The sandbox already withholds `C2FIXTURE_DB_PASSWORD` from the child, so the bytes get into the
    output the way they really do — quoted by something that was configured with them (a traceback,
    a pytest failure diff, a library echoing its own DSN). The screen that catches it is the
    operator's environment, which is authenticated evidence: nothing the node prints can put a value
    on that list or take one off it.
    """
    monkeypatch.setenv("C2FIXTURE_DB_PASSWORD", _ENV_SECRET)
    monkeypatch.setenv("C2FIXTURE_DATA_ROOT", _ENV_BENIGN)
    tail, captured = _run_leaky_engine(
        tmp_path, [f"psycopg2.OperationalError: password {_ENV_SECRET} rejected",
                   f"loading from {_ENV_BENIGN}",
                   f"weights sha {_ENTROPY_BLOB}"])
    assert _ENV_SECRET not in tail and _ENV_SECRET not in captured
    assert "***REDACTED_ENV***" in tail
    # ...and the two things that must SURVIVE: a benign env value, and — at the default — a blob
    # that only the opt-in entropy pass may touch. An operator debugging a checkpoint mismatch
    # needs that hash, which is exactly why the entropy half stayed gated.
    assert _ENV_BENIGN in tail
    assert _ENTROPY_BLOB in tail


def test_opt_in_entropy_pass_still_reaches_the_persisted_tail(tmp_path):
    tail, _ = _run_leaky_engine(tmp_path, [f"weights sha {_ENTROPY_BLOB}"], redact_output=True)
    assert _ENTROPY_BLOB not in tail and "***REDACTED***" in tail


def test_the_entropy_pass_keeps_the_diagnostics_the_corpus_says_operators_need(tmp_path):
    """THE NEGATIVE CONTROL, driven end-to-end with the lossy pass ON — the posture the docs
    recommend for untrusted tiers, and the one where over-masking actually costs something.

    Every string here is a real shape lifted from `runs/`: the exact tokens this pass masked before
    `_entropy_candidate`/`_ENTROPY_TOKEN_CHARS`. If any one comes back as `***REDACTED***`, the
    operator has lost the filename out of their own traceback — which is what 13 of 13 entropy
    masks over the corpus's 744 persisted tails actually were.
    """
    survivors = [
        '  File "/home/jovyan/data/looplab/runs/rubertlite-dr-unified-v2/nodes/node_0/'
        'vectorsearch/training/collator.py", line 44, in __init__',
        "rubert-tiny-lite-dense-retrieval-best-config-kno-ad73cfa5.md:22: key pitfalls",
        "outputs/bigtest/query_embeddings_var",
        "CrossBatchMultipleNegativesRankingLoss",
        "vectorsearch/experiments/qwen3_hn_rubert-tiny-lite/final/model",
    ]
    tail, _ = _run_leaky_engine(tmp_path, survivors, redact_output=True)
    assert "***REDACTED***" not in tail
    for line in survivors:
        assert line in tail, line


# --- the entropy pass's COMPOSITION screen (the half that made a default flip safe) --------------
#
# Every FALSE-POSITIVE string below is lifted verbatim from `runs/` on this box, where the entropy
# pass masked it before this screen existed: 13 of 13 changed tails were paths, and 162,472
# `***REDACTED***` markers are already durable in the corpus's `spans.jsonl` from the ~30 ALWAYS-ON
# `redact_persisted_text` boundaries, whose sampled contents are traceback paths and memory-note
# filenames. This is not a hypothetical over-match; it is what shipped.

_CORPUS_MUST_SURVIVE = [
    "/home/jovyan/data/looplab/runs/rubertlite-dr-unified-v2/nodes/node_0/"
    "vectorsearch/training/collator",
    "rubert-tiny-lite-dense-retrieval-best-config-kno-ad73cfa5",
    "4-gradient-scaling-for-infonce-at-low-temperatur-e9b812",
    "outputs/bigtest/query_embeddings_var",
    "CrossBatchMultipleNegativesRankingLoss",          # CamelCase identifier: no digit
    "vectorsearch/experiments/qwen3_hn_rubert-tiny-lite/final/model",
    "11/site-packages/pytorch_lightning/trainer/configuration_validator",
]

_CORPUS_MUST_MASK = [
    "aZ9k2Lp7qW3xYt5Rb8Nc1Vd6Mf0Gh4J",                 # base64-ish, both cases + digits
    "dGhpcyBpcyBhIHRlc3Qgc2VjcmV0IHZhbHVlIDEyMzQ1Ng==",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0",
]


@pytest.mark.parametrize("text", _CORPUS_MUST_SURVIVE)
def test_entropy_pass_leaves_real_corpus_diagnostics_intact(text):
    assert redact_secrets(f'  File "{text}.py", line 44, in __init__', entropy=True) == \
        f'  File "{text}.py", line 44, in __init__'


@pytest.mark.parametrize("token", _CORPUS_MUST_MASK)
def test_entropy_pass_still_masks_an_unknown_shape_credential(token):
    out = redact_secrets(f"connecting with {token} now", entropy=True)
    assert token not in out and "***REDACTED***" in out


def test_a_credential_embedded_in_a_path_is_masked_without_eating_the_path():
    # The screen is per-token, so a real secret sitting inside a path still goes — and the
    # directories around it survive, which is the whole difference from the old behaviour.
    out = redact_secrets("/data/models/aZ9k2Lp7qW3xYt5Rb8Nc1Vd6Mf0Gh4J/weights.bin", entropy=True)
    assert "aZ9k2Lp7qW3xYt5Rb8Nc1Vd6Mf0Gh4J" not in out
    assert out.startswith("/data/models/") and out.endswith("/weights.bin")


# --- invariant #6: a default flip must not change how an ALREADY-RECORDED run replays ------------

def test_an_already_recorded_run_replays_byte_identically_whatever_the_live_default(tmp_path):
    """A run RECORDED under the old posture must REPLAY exactly as recorded.

    Driven, not asserted about the fold's source: a real run writes its tail with the entropy pass
    off (what every log in `runs/` on this box carries), and the same file is then folded while the
    LIVE default is on. The fold is pure and re-redacts nothing, so a default flip can neither
    rewrite a run's history nor retroactively sanitize it — which is what invariant #6 owes an
    operator who resumes a six-week-old run.
    """
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold

    tail, _ = _run_leaky_engine(tmp_path, [f"weights sha {_ENTROPY_BLOB}"], redact_output=False)
    assert _ENTROPY_BLOB in tail                     # recorded raw, as an old run would have

    log = tmp_path / "r" / "events.jsonl"
    on_disk = log.read_text(encoding="utf-8")
    replayed = fold(EventStore(log).read_all())
    assert any(_ENTROPY_BLOB in (n.stdout_tail or "") for n in replayed.nodes.values())
    assert log.read_text(encoding="utf-8") == on_disk   # replay wrote nothing back


def test_a_run_resumes_with_the_redaction_posture_it_recorded(tmp_path):
    """Invariant #6's other half: `resume` restores Settings from the run's OWN
    `config.snapshot.json`, which records this field EXPLICITLY rather than omitting it as a
    default — so whatever the shipped default is or later becomes, a settled run re-enters with the
    posture it was started under."""
    from looplab.cli import load_run_settings
    from looplab.core.config import Settings

    snapshot = json.loads(Settings(redact_output=False).model_dump_json())
    assert snapshot["redact_output"] is False        # the field is RECORDED, not omitted-as-default
    (tmp_path / "config.snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")

    restored = load_run_settings(tmp_path, strict=True)
    assert restored.redact_output is False

    # ...and it stays the RECORDED value even when the live default is the other one, which is the
    # property invariant #6 actually owes: a flip of this field must never reach a settled run.
    flipped = json.loads(Settings(redact_output=True).model_dump_json())
    (tmp_path / "config.snapshot.json").write_text(json.dumps(flipped), encoding="utf-8")
    assert load_run_settings(tmp_path, strict=True).redact_output is True


# --- the SEVENTH persisted output channel, found by the C2 sweep ---------------------------------

def test_the_crash_triage_rationale_is_redacted_like_its_two_sibling_verdicts(tmp_path):
    """`node_failed.triage_rationale` is LLM text about a crash, written to the durable log, and it
    was the one judgement of its kind with no screen — `train_monitor` and `asha_monitor` redact
    their `reason` for exactly this reason. Driven: a real failing eval, a judge that returns a
    credential in its rationale, and the row read back off disk."""
    import anyio
    from pathlib import Path
    from looplab.core.models import Idea
    from looplab.engine.orchestrator import Engine
    from looplab.search.policy import GreedyTree
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.adapters.toytask import ToyTask

    task = ToyTask.load(Path(__file__).resolve().parents[1] / "examples" / "toy_task.json")
    secret = "sk-abcdefABCDEF0123456789TRIAGE"

    class _CrashingDev:
        def implement(self, idea):
            return "raise SystemExit('boom')\n"

        def repair(self, idea, code, error):
            return "raise SystemExit('boom')\n"

    class _Stub:
        def propose(self, state, parent):
            return Idea(operator="draft", params={"x": 1.0, "y": 1.0})

    run_dir = tmp_path / "r"
    eng = Engine(run_dir, task=task, researcher=_Stub(), developer=_CrashingDev(),
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1))
    eng._triage_crash = lambda *a, **kw: {
        "action": "abandon",
        "rationale": f"the eval reads {secret} from the environment and the endpoint refuses it",
    }
    anyio.run(eng.run)

    rows = [json.loads(l) for l in (run_dir / "events.jsonl").read_text().splitlines() if l.strip()]
    # BOTH rows the judge's own words land on: the terminal's `triage_rationale` and, when the
    # verdict was "repair", `node_repaired.rationale`. The sweep found them one after the other.
    rationales = [r["data"].get("triage_rationale", "") for r in rows if r.get("type") == "node_failed"]
    rationales += [r["data"].get("rationale", "") for r in rows if r.get("type") == "node_repaired"]
    assert rationales and any(r.strip() for r in rationales), "no triage rationale was persisted"
    joined = " ".join(rationales)
    assert secret not in joined and "sk-***" in joined
    assert "from the environment" in joined            # the diagnosis itself survives
    # ...and nothing else in the whole log kept it either.
    assert secret not in (run_dir / "events.jsonl").read_text()
