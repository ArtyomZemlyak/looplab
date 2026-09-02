"""A skill card is a SHARED artifact, so the candidate code distilled into it is redacted.

`node_created.code` is deliberately outside `Engine._redact` — the code is the record of what ran,
and inside its own run that is right. A skill card is the opposite kind of object: it leaves the run,
lands in the shared skills directory, and is mounted into every later Researcher's toolset. A
hard-coded token in a winning snippet therefore shipped, verbatim, to every future run on the box.
This was the one cross-run sink written from unredacted candidate source.

REDACTED AT THE WRITE, not the read. `use_skill` reads these files back verbatim and so does anyone
with a shell; redacting on the way out would leave the secret on disk and hide it from one reader.

`entropy=False` deliberately, and the reason is what the entropy pass was measured on: zero false
positives across 1,652 persisted LOG TAILS. A skill body is not a log tail — it is CODE, where a
long mixed-case alphanumeric literal is as likely to be a model id, a constant or a checksum as a
credential. The two authenticated rungs carry no such ambiguity.
"""
from __future__ import annotations

import pytest

from looplab.engine.memory import (_MAX_SKILL_BODY_CHARS, redacted_skill_body, write_auto_skill)


def test_a_credential_shaped_literal_never_reaches_the_card():
    """MUTATION: write `body` straight through -> the token ships to every later run."""
    out = redacted_skill_body('client = OpenAI(api_key="sk-abcdefghijklmnopqrstuvwxyz012345")')

    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in out
    assert "OpenAI(" in out, "the technique must survive; only the secret goes"


def test_this_boxs_own_env_values_are_masked(monkeypatch):
    """The authenticated half. A regex knows credential SHAPES; only the operator's environment
    knows that this particular string is this box's password."""
    monkeypatch.setenv("POSTGRES_PASSWORD", "hunter2hunter2")

    out = redacted_skill_body('conn = connect(password="hunter2hunter2")')

    assert "hunter2hunter2" not in out


def test_ordinary_code_is_untouched():
    """The regression this could most easily cause. A skill whose code has been mangled teaches a
    technique that no longer runs.

    MUTATION: turn the entropy pass on -> a long mixed-case identifier or model id can be masked,
    and the card silently stops being usable.
    """
    body = ("from transformers import AutoModel\\n"
            "m = AutoModel.from_pretrained('intfloat/multilingual-e5-small')\\n"
            "loss = NLLCosLoss(temperature=0.05)\\n"
            "digest = 'a033f538e8ba7cf2b1d4e6079c2a1f30'\\n")

    assert redacted_skill_body(body).strip() == body.strip()


def test_an_oversized_body_is_clipped_AND_SAYS_SO():
    """A silently shortened code snippet is a skill that teaches a technique with its ending
    removed, which is worse than one that admits it was cut.

    MUTATION: drop the disclosure -> the card looks complete and is not.
    """
    body = "x = 1\\n" * (_MAX_SKILL_BODY_CHARS // 2)

    out = redacted_skill_body(body)

    assert len(body) > _MAX_SKILL_BODY_CHARS
    assert "clipped" in out and str(len(body)) in out


def test_a_body_within_the_cap_carries_no_notice():
    assert "clipped" not in redacted_skill_body("x = 1\\n" * 10)


def test_the_written_card_carries_the_redacted_body(tmp_path):
    """End to end through the real writer, because the redaction has to be on the path that
    actually persists — a helper nothing calls is the defect with an extra step."""
    path = write_auto_skill(
        tmp_path, "use a cosine loss with in-batch negatives",
        'TOKEN = "sk-abcdefghijklmnopqrstuvwxyz012345"\\nloss = NLLCosLoss()\\n',
        ["retrieval", "contrastive"], "repo_task")

    assert path is not None, "the writer refused the card entirely"
    text = path.read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in text
    assert "NLLCosLoss()" in text, "the technique must survive into the card"
