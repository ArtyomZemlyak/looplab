"""The line that says what died reaches the Developer, instead of sitting outside the window.

`_eval_failure_text` hands the repair path — the Developer's prompt, the durable
`node_repaired.error_in`, the judge's history rows and the terminal's `error` — the LAST 500
CHARACTERS of stderr. Measured on `runs/e5small-dr-unified-v4` node 4, `torch.OutOfMemoryError` is
952 characters from EOF and 329 of that window is a tqdm bar's trailing whitespace; the three
`tests/test_torch_oom_is_an_oom.py` corpus entries put it 1,659 / 12,991 / 14,192 characters out. So
the Developer was asked to fix an out-of-memory failure without the allocation size, the device or
the free memory — all of which its own process printed.

A PUSH, NOT A CLASSIFICATION. `engine/failure_diagnosis.py` deleted the OOM marker scan because it
was TEXT WITH THE LAST WORD. This mints no reason, moves no gate and reaches no vocabulary, so an
exception shape the pattern misses costs context and can never cost a wrong answer — the asymmetry
that makes a text rule admissible here and inadmissible for a classifier.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from looplab.core.models import FAILURE_REASONS
from looplab.engine.failure_diagnosis import (
    _HEADLINE_LINES, _HEADLINE_TOTAL, failure_headline)

_OOM = ("torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 1.12 GiB. GPU 0 has a total "
        "capacity of 139.80 GiB of which 691.06 MiB is free.")


def _corpus():
    import test_torch_oom_is_an_oom as oom

    return oom._CORPUS


def test_the_allocation_numbers_surface_on_every_corpus_entry():
    """THE DEFECT, on the three real logs. MUTATION: remove the push -> none of these numbers is in
    the 500-char slice the Developer is handed, on any of them."""
    for name, stderr in _corpus().items():
        headline = failure_headline(stderr)
        assert "Tried to allocate" in headline, name
        assert "GiB" in headline, name


def test_a_LAUNCHER_wrapper_does_not_win():
    """"The last exception line" is the obvious rule and the corpus refutes it: `torchrun` prints
    every rank's traceback and then its own `ChildFailedError:` with an EMPTY message body, and
    `subprocess.CalledProcessError` reports a child's exit status after the child has said why.

    MUTATION: take the last match -> two of the three corpus entries answer
    `ChildFailedError:` and the third `CalledProcessError`, i.e. the push carries nothing.
    """
    stderr = ("Traceback (most recent call last):\n"
              f"{_OOM}\n"
              "torch.distributed.elastic.multiprocessing.errors.ChildFailedError: \n")
    assert failure_headline(stderr).startswith("torch.OutOfMemoryError")


def test_the_wrapper_still_RIDES_when_it_fits():
    """It is not worthless — "this died under torchrun across two ranks" is real context — it is just
    not the headline."""
    stderr = f"{_OOM}\nsubprocess.CalledProcessError: exit 1\n"
    headline = failure_headline(stderr)
    assert headline.startswith("torch.OutOfMemoryError") and "CalledProcessError" in headline


def test_a_RANK_TAGGED_line_is_found():
    """`torchrun` prefixes every line with `[rank0]: `, which put the OOM outside a left-margin
    anchor entirely — 0 of the 2 DDP corpus entries matched before this.

    MUTATION: drop the bracket clause -> both DDP entries answer with the launcher line only.
    """
    assert failure_headline(f"[rank0]: {_OOM}\n").startswith("torch.OutOfMemoryError")
    assert failure_headline(f"    {_OOM}\n").startswith("torch.OutOfMemoryError"), "indented too"


def test_prose_mentioning_an_error_name_is_not_a_headline():
    """The anchor is what keeps this from matching narration. MUTATION: drop the `^` -> a retry
    message becomes the thing the Developer is told killed the node."""
    assert failure_headline("warning: retrying after ValueError in the loader\n") == ""
    assert failure_headline("no exception here at all\n") == ""


def test_the_push_is_BOUNDED():
    """It is PREPENDED to a 500-character tail, so a headline that crowds out the tail has traded one
    missing fact for another."""
    giant = "SomeError: " + "x" * 5000
    assert len(failure_headline(f"{giant}\n")) <= _HEADLINE_TOTAL
    many = "\n".join(f"Error{i}: {'y' * 300}" for i in range(20))
    assert len(failure_headline(many)) <= _HEADLINE_TOTAL
    assert failure_headline(many).count(" | ") < _HEADLINE_LINES


def test_it_is_total_on_junk():
    for junk in (None, "", 123, [], "\n\n\n"):
        assert failure_headline(junk) == ""


def test_it_MINTS_NO_REASON():
    """The licence for a text rule here. MUTATION: let this decide a `reason` -> it becomes the
    marker scan `failure_diagnosis.py` deleted, with the same words in its docstring."""
    import ast
    import inspect

    from looplab.engine import failure_diagnosis as mod

    tree = ast.parse(inspect.getsource(mod.failure_headline))
    literals = {node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert not (literals & set(FAILURE_REASONS)), "the headline names a failure reason"


def test_it_reaches_the_DEVELOPER_and_not_the_diagnostician():
    """THE SPLIT, and it is the whole design. `_eval_failure_text` feeds TWO roles that are not in
    the same position: the DIAGNOSTICIAN holds `repair_log_tools` and can PULL the line out of the
    stage log, while the DEVELOPER gets that string and nothing else. docs/44 measured the
    diagnostician's prompt as byte-identical and argued the trade from ~8.8 provider calls per
    failure — `tests/test_diagnosis_record.py` pins it as an equality, not a budget — so pushing
    into `_eval_failure_text` would break a measured contract to hand a fact to a reader that could
    already fetch it.

    MUTATION: prepend in `_eval_failure_text` instead -> `test_diagnosis_record.py::
    test_the_allocator_message_is_recoverable_from_the_record_and_the_prompt_did_not_grow` goes red,
    which is exactly how this split was found.
    """
    from factories import make_engine

    engine = make_engine(Path("/tmp") / "headline-split")
    tail = "  " * 300
    context = engine._repair_error_context("crash", tail, headline=_OOM)
    assert context.startswith(f"[{_OOM}]"), context[:120]
    assert "Tried to allocate" in context


def test_a_headline_ALREADY_in_the_tail_is_not_duplicated():
    """A short stderr whose exception is inside the 500-char window must be byte-identical to what
    it was. MUTATION: prepend unconditionally -> every such failure gains a duplicated line."""
    from factories import make_engine

    engine = make_engine(Path("/tmp") / "headline-dup")
    tail = f"Traceback...\n{_OOM}\n"
    context = engine._repair_error_context("crash", tail, headline=_OOM)
    assert context.count("Tried to allocate") == 1, context


def test_no_headline_is_byte_identical():
    """The default is empty, so every caller that does not pass one — and every reason arm that
    predates this — is unchanged."""
    from factories import make_engine

    engine = make_engine(Path("/tmp") / "headline-none")
    assert (engine._repair_error_context("crash", "boom")
            == engine._repair_error_context("crash", "boom", headline=""))


def test_the_eval_path_passes_it():
    """AST over the real call site, because a `headline=` that is never passed is the state this
    fixed, and it looks identical from inside `_repair_error_context`."""
    import ast

    from looplab.engine import evaluate as evaluate_mod

    tree = ast.parse(Path(evaluate_mod.__file__).read_text(encoding="utf-8"))
    passed = [kw for node in ast.walk(tree)
              if isinstance(node, ast.Call)
              and getattr(node.func, "attr", "") == "_repair_error_context"
              for kw in node.keywords if kw.arg == "headline"]
    assert passed, "the eval path no longer pushes the headline"
    assert any(isinstance(kw.value, ast.Call)
               and getattr(kw.value.func, "id", "") == "failure_headline" for kw in passed)


def test_the_headline_is_REDACTED_before_it_reaches_the_provider_or_the_log():
    """C2, on the eighth persisted channel — and on the widest read in the engine.

    This string is prepended to the repair prompt (so it reaches the provider and lands verbatim in
    `spans.jsonl`, which records whole prompts) and rides the durable `node_repaired.error_in`. The
    500-character tail one line away at its call site has gone through `Engine._redact` for exactly
    that reason; this reads SIXTY-FOUR THOUSAND. Measured over the preserved stage/console logs, a
    500-char window carries 0 masks while a 64 KB read carries 384 — including a real `password`.

    MUTATION: drop the `redact` argument at `evaluate.py`'s call site -> the credential is in the
    prompt and on the row."""
    stderr = ("progress " * 200 + "\n"
              "ValueError: could not connect to postgres://svc:hunter2hunter2@db:5432/app\n")
    assert "hunter2hunter2" in failure_headline(stderr), "unredacted, it is right there"
    masked = failure_headline(stderr, lambda t: t.replace("hunter2hunter2", "***"))
    assert "hunter2hunter2" not in masked and "***" in masked


def test_it_redacts_the_WINDOW_and_then_extracts():
    """The C2 ordering `failure_diagnosis._screened` states one function over: masking after a cut
    can be truncated away, and the extraction IS a cut. A redactor handed only the already-selected
    line never sees the bytes around it, and a secret split by the selection lands verbatim."""
    seen: list[int] = []
    failure_headline("noise\nValueError: boom\n", lambda t: (seen.append(len(t)) or t))
    assert seen and seen[0] > len("ValueError: boom"), (
        "the redactor must be handed the whole window, not the extracted line")


def test_a_REDACTOR_THAT_RAISES_yields_no_headline_rather_than_the_raw_text():
    """It must never fail OPEN. Losing the headline costs context on a failure path; leaking it
    costs a credential in a provider prompt and on a durable row."""
    def _boom(_t):
        raise RuntimeError("redactor exploded")

    assert failure_headline("ValueError: secret=abc\n", _boom) == ""


def test_the_engine_passes_its_own_redactor():
    """Tier 3 is not enough for "did it run", so this drives the ARGUMENT: `Engine._redact` is the
    one funnel every persisted tail goes through, and the headline is now one of them."""
    from tests._source_scan import called_names          # noqa: F401 - availability is the point
    import inspect

    from looplab.engine import evaluate
    src = inspect.getsource(evaluate)
    idx = src.index("headline=failure_headline(")
    assert "self._redact" in src[idx:idx + 160], (
        "the call site must hand the headline the engine's redactor")
