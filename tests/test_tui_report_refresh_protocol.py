"""The paid report-refresh protocol is its own reconciliation method (doc 25 SC-15).

`_reconcile_pending` used to run TWO distinct reconciliation protocols in one ~153-line loop body:
the paid report-refresh receipt protocol (identity = the row's own durable idempotency key, request
= `refresh_report`, and a 200 saying `ok` is not yet success — it must carry a valid event receipt)
and the generic command protocol (identity = a server-issued command id, request =
`get_run_command`, with a staged-replay path on 404). Reading either one meant skipping over the
other's branches, and each read as the other's special case.

Two properties are worth pinning after the split, and neither had a test before:

  * the paid protocol's UNRESOLVED verdicts still reach the caller. `_reconcile_pending`'s return
    value gates every later ordered action; the extraction turned two in-loop `unresolved = True`
    assignments into a return value, and a method that forgot to propagate one would silently let
    the TUI submit a second ordered action while a paid report's outcome was still unknown.
  * the two protocols stay separated. Re-inlining either one is what the finding is about, and
    nothing else in the suite notices — every existing reconciliation test drives `_reconcile_pending`
    from the outside and passes either way.
"""
from __future__ import annotations

import ast
import inspect
import io
import textwrap

import pytest
from rich.console import Console

from looplab.serve import tui

GENERATION = "a" * 64
KEY = "0f0a3d64-0f31-4e2e-9d0f-3f6a1c5b2d77"


def _refresh_tui(refresh):
    """A no-server TUI whose only API surface is `refresh_report`, plus a persist recorder."""

    class FakeApi:
        def __init__(self):
            self.calls = []

        def run_generation(self, _run_id):
            return GENERATION

        def refresh_report(self, run_id, **kwargs):
            self.calls.append((run_id, kwargs.get("expected_generation"),
                               kwargs.get("idempotency_key")))
            return refresh(len(self.calls))

    app = tui.Tui.__new__(tui.Tui)
    app.api = FakeApi()
    app.console = Console(file=io.StringIO(), force_terminal=False, color_system=None)
    app.persisted = []
    app._persist = lambda run_id, row: app.persisted.append(row)
    return app


def _pending_row():
    return {"role": "action", "action": {"type": "__refresh_report__", "label": "refresh"},
            "status": "pending",
            "report_refresh": {"expected_generation": GENERATION, "idempotency_key": KEY}}


# ------------------------------------------------------------------ unresolved reaches the caller

def test_a_transient_refresh_failure_still_gates_the_next_ordered_action():
    """The paid request could not be re-asked, so the report's outcome is UNKNOWN. Returning True
    here would let the TUI submit another ordered action on top of an in-flight paid report."""

    def refresh(_n):
        raise tui.ApiError("gateway timeout", status=503)

    app = _refresh_tui(refresh)
    history = [_pending_row()]

    assert app._reconcile_pending("demo", history) is False
    assert history[0]["status"] == "pending", "a transient outage must not resolve the row"
    assert app.persisted == [], "an unknown outcome must not be persisted as terminal"


@pytest.mark.parametrize("code", sorted(tui._REPORT_PENDING_CODES))
def test_every_pending_report_code_still_gates_the_next_ordered_action(code):
    """The server answered, but with a code that says the paid job's fate is undecided. Same
    consequence: the caller must keep the run gated, and nothing terminal may be persisted."""
    app = _refresh_tui(lambda _n: {"ok": False, "code": code, "error": "still deciding"})
    history = [_pending_row()]

    assert app._reconcile_pending("demo", history) is False
    assert history[0]["status"] == "pending"
    assert history[0]["report_code"] == code
    assert app.persisted == []


def test_a_valid_receipt_resolves_the_row_and_persists_it():
    app = _refresh_tui(lambda _n: {"ok": True, "seq": 7, "generation": GENERATION})
    history = [_pending_row()]

    assert app._reconcile_pending("demo", history) is True
    assert history[0]["status"] == "done"
    assert [row["status"] for row in app.persisted] == ["done"]
    assert app.api.calls == [("demo", GENERATION, KEY)], "the paid identity must be reused exactly"


def test_ok_without_a_valid_receipt_is_not_success_but_is_not_terminal_either():
    """`report_refresh_protocol_error` is itself a PENDING code: a 200 that says `ok` while carrying
    no usable receipt tells us nothing about whether the paid work landed."""
    app = _refresh_tui(lambda _n: {"ok": True, "seq": 7, "generation": "b" * 64})
    history = [_pending_row()]

    assert app._reconcile_pending("demo", history) is False
    assert history[0]["status"] == "pending"
    assert history[0]["report_code"] == "report_refresh_protocol_error"


def test_a_row_without_a_durable_identity_fails_without_paying_again():
    app = _refresh_tui(lambda _n: pytest.fail("no paid request may be made without an identity"))
    row = _pending_row()
    row["report_refresh"] = {}
    history = [row]

    assert app._reconcile_pending("demo", history) is True
    assert history[0]["status"] == "failed"
    assert "no new paid work was submitted" in history[0]["error"]
    assert [persisted["status"] for persisted in app.persisted] == ["failed"]


def test_one_unresolved_paid_row_gates_the_whole_history():
    """`_reconcile_pending` folds every row's verdict into ONE boolean. A resolved row later in the
    history must not overwrite an earlier row's unknown outcome."""
    app = _refresh_tui(lambda n: ({"ok": False, "code": "job_timeout", "error": "slow"} if n == 1
                                  else {"ok": True, "seq": 3, "generation": GENERATION}))
    history = [_pending_row(), _pending_row()]

    assert app._reconcile_pending("demo", history) is False
    assert [turn["status"] for turn in history] == ["pending", "done"]


# ------------------------------------------------------------------ nothing is paid for un-staged

def test_a_paid_refresh_is_not_submitted_when_its_identity_cannot_be_staged():
    """The staging prologue shared by both submit paths, on the arm that only the paid one has.

    Its two command siblings are covered (`test_{apply_plan,control}_does_not_submit_when_durable_
    staging_fails`); the report-refresh arm was not, and it is the one where an un-staged submission
    costs money — with no row on disk, `_reconcile_report_refresh` has no identity to re-ask with,
    so the run can neither confirm nor re-request the report it just paid for.
    """
    app = _refresh_tui(lambda _n: pytest.fail("no paid work may be submitted before staging lands"))
    app._persist = lambda *_a, **_k: False
    history = []

    app._apply_plan("demo", history, [{"type": "__refresh_report__", "data": {},
                                       "label": "refresh"}])

    assert len(history) == 1, "the row is still appended so the operator sees the refusal"
    assert history[0]["status"] == "failed"
    assert "no paid work was submitted" in history[0]["error"]
    assert app.api.calls == []


# ------------------------------------------------------------------ the protocols stay separated

def _code_only(func) -> str:
    """The function's CODE, with its docstring and comments removed.

    Both docstrings here describe the *other* protocol on purpose — that is what makes the split
    readable — so a naive `inspect.getsource` scan matches its own explanation and the guard can
    never fail. `ast.unparse` drops comments, and the docstring is popped explicitly.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func))).body[0]
    if (tree.body and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)):
        tree.body.pop(0)
    return ast.unparse(tree)


def test_the_paid_protocol_is_not_inlined_back_into_the_generic_loop():
    """The finding itself. Every existing reconciliation test drives `_reconcile_pending` from the
    outside and passes whether the two protocols are separate or interleaved, so nothing else in the
    suite notices a re-merge."""
    loop = _code_only(tui.Tui._reconcile_pending)
    # NOT the bare word "refresh_report": the loop legitimately keeps the DISCRIMINATOR literal
    # `'__refresh_report__'` — deciding which protocol a row belongs to is the loop's job. What must
    # be gone is the protocol itself: the paid request, its receipt check, its pending-code table
    # and its intent key.
    for spelling in (".api.refresh_report", "_report_success", "_REPORT_PENDING_CODES",
                     "'report_refresh'"):
        assert spelling not in loop, (
            f"_reconcile_pending speaks the paid report protocol again ({spelling}); it must "
            "delegate to _reconcile_report_refresh and run the generic command protocol only")


def test_the_generic_command_protocol_did_not_leak_into_the_paid_method():
    """The other direction: the paid method must not start reconciling server-issued command ids."""
    paid = _code_only(tui.Tui._reconcile_report_refresh)
    for spelling in ("get_run_command", "_staged_replay", "_COMMAND_DONE", "_COMMAND_FAILED",
                     "_COMMAND_PENDING"):
        assert spelling not in paid, (
            f"_reconcile_report_refresh speaks the generic command protocol ({spelling})")


def test_the_paid_branch_is_the_only_thing_the_loop_says_about_report_refreshes():
    """A single delegation point. Two call sites would be the interleaving growing back one branch
    at a time."""
    assert _code_only(tui.Tui._reconcile_pending).count("_reconcile_report_refresh") == 1
