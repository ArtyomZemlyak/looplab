"""B3 secret-leak redaction over persisted output tails."""
from __future__ import annotations

from looplab.trust.redact import redact_persisted_text, redact_secrets


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
