"""A plan step was bounded in turns and in seconds, and the run ends on money.

Measured 2026-08-31 across 7 AlgoTune probes. The most expensive SINGLE plan step, as a share of
what remained when it started:

    remPde   66 %   remPde2  49 %   accPde  32 %   remDL2  72 %
    remEE    18 %   remEE2    8 %   accEE    7 %

remPde's step ran 72 generations and was cut by the 1200 s wall at 1212 s: the wall worked, and 48 %
of the $1.00 run was already spent when it fired. The identical ceiling never bit on edge_expansion
(worst step 8-9 %), so seconds are not a proxy for dollars across tasks and bounding one does not
bound the other.

Two halves, and both are tested here: the loop must be able to stop for money at all, and the plan
step must ask it to with a ceiling that bites the runaway and spares the legitimate late step.
"""
import math

import pytest

from looplab.agents import tool_loop
from looplab.agents.loop_options import LOOP_OPTION_FIELDS
from looplab.adapters.repo_developer import LLMRepoDeveloper


class _Acct:
    def __init__(self, limit, spent):
        self.limit, self.spent = limit, spent


class _Dev:
    """Just enough of a developer for the two budget helpers, which read only `client.accountant`."""
    def __init__(self, limit=None, spent=0.0):
        if limit is None:
            self.client = None
        else:
            self.client = type("C", (), {"accountant": _Acct(limit, spent)})()


_ceiling = LLMRepoDeveloper._step_cost_ceiling


# --------------------------------------------------------------- the ceiling the developer asks for

def test_it_bites_the_step_that_ate_the_run():
    """remPde: $0.7331 left, one step took $0.4820 of it."""
    cap = _ceiling(_Dev(limit=1.00, spent=1.00 - 0.7331))
    assert cap < 0.4820, (
        f"ceiling {cap:.4f} would have let remPde's 66 %-of-remaining step through unchanged"
    )


@pytest.mark.parametrize("name,remaining,step", [
    ("remPde2", 0.6611, 0.3223),
    ("accPde", 0.8941, 0.2822),
    ("remDL2", 0.2322, 0.1679),     # a LAST step spending most of a small remainder: legitimate
    ("remEE", 0.4807, 0.0880),
    ("remEE2", 0.7875, 0.0647),
    ("accEE", 0.5192, 0.0359),
])
def test_it_spares_every_other_step_the_corpus_actually_ran(name, remaining, step):
    cap = _ceiling(_Dev(limit=1.00, spent=1.00 - remaining))
    assert step <= cap, (
        f"{name}'s step of ${step:.4f} would be cut by a ceiling of ${cap:.4f} -- this rule is meant "
        "to stop a runaway, not to shorten the six steps that behaved"
    )


def test_the_floor_is_what_protects_a_late_step():
    """Half-of-remaining ALONE would cut remDL2's last step; the fifth-of-the-run floor is why not."""
    dev = _Dev(limit=1.00, spent=1.00 - 0.2322)
    assert 0.5 * 0.2322 < 0.1679, "premise: half of remaining is below remDL2's step"
    assert _ceiling(dev) >= 0.1679, "the floor no longer protects a legitimate late step"


def test_no_accountant_means_no_ceiling_not_a_zero_one():
    assert _ceiling(_Dev()) == 0.0
    assert _ceiling(_Dev(limit=0.0)) == 0.0, "a run with no llm_budget_usd must be unbounded as before"
    assert _ceiling(_Dev(limit=float("inf"))) == 0.0, "a non-finite limit must not become a ceiling"


# ------------------------------------------------------------------------- the loop's own half

def test_the_loop_declares_a_money_cutoff():
    assert "cost" in tool_loop.LOOP_CUTOFF_KINDS, (
        "the loop can stop for money but does not name it, so `on_budget` observers cannot branch"
    )
    assert "cost_budget_usd" in LOOP_OPTION_FIELDS, (
        "cost_budget_usd is a config knob and must be a LoopOptions field -- see the registry-"
        "guarded-seam note in loop_options.py"
    )


def test_session_spend_is_measured_from_the_session_start_not_the_run_total():
    """The accountant is the RUN's; a session ceiling must not inherit what earlier phases spent."""
    client = type("C", (), {"accountant": _Acct(1.0, 0.60)})()
    at_start = tool_loop._accountant_spend(client)
    assert at_start == 0.60
    client.accountant.spent = 0.75
    assert tool_loop._session_spend(client, at_start) == pytest.approx(0.15), (
        "this session spent $0.15; reading the run total would cut it immediately"
    )


def test_a_missing_accountant_reads_as_unknown_not_as_zero():
    """0.0 is a real reading. Conflating it with 'no accountant' turns the ceiling into a no-op."""
    assert tool_loop._accountant_spend(object()) is None
    assert tool_loop._session_spend(object(), None) is None
    bad = type("C", (), {"accountant": type("A", (), {"spent": "not a number"})()})()
    assert tool_loop._accountant_spend(bad) is None, "a bad reading must never raise or count as 0"


def test_the_ceiling_is_off_by_default_for_every_other_session():
    """Only the plan step passes one; nothing else may acquire a money ceiling by accident."""
    # Asserted on the VALUE, not on the source text. The first version of this test looked for the
    # substring "cost_budget or 0.0", which is still present inside "cost_budget or 0.05" -- mutation
    # on 2026-08-31 flipped the default to $0.05 and the whole file stayed green.
    opts = LLMRepoDeveloper._session_opts(_Dev(limit=1.0, spent=0.0))
    assert float(dict(opts).get("cost_budget_usd", -1)) == 0.0, (
        f"a session that asked for no money ceiling got {dict(opts).get('cost_budget_usd')!r} -- "
        "sessions never measured eating a run would start being cut"
    )
    step = LLMRepoDeveloper._session_opts(_Dev(limit=1.0, spent=0.0), cost_budget=0.4)
    assert float(dict(step)["cost_budget_usd"]) == 0.4, "the plan step's ceiling is not passed through"


# ---------------------------------------------------------- and it must actually FIRE, end to end
#
# The eleven tests above check the helpers and the constants. None of them drives the loop, and a
# ceiling that is declared, computed, passed and never checked would pass every one of them. That is
# the hole mutation finds, so it is closed here rather than found later.

class _SpendingClient:
    """A scripted client whose accountant grows with every turn, like a real one."""

    def __init__(self, per_turn, start=0.0, limit=1.0):
        self.accountant = _Acct(limit, start)
        self.per_turn = per_turn
        self.turns = 0

    def chat(self, messages, tools, tool_choice="auto"):
        self.turns += 1
        self.accountant.spent += self.per_turn
        return {"content": "", "tool_calls": [
            {"id": f"c{self.turns}", "function": {"name": "work", "arguments": "{}"}}]}

    def complete_text(self, messages):
        return "SUMMARY"


class _WorkTool:
    def specs(self):
        return [{"type": "function", "function": {"name": "work", "parameters": {}}}]

    def execute(self, name, args):
        return "did some work"


_EMIT = {"type": "function", "function": {"name": "emit", "parameters": {}}}


def _drive(client, **kw):
    seen = []
    tool_loop.drive_tool_loop(
        client, _WorkTool(), [{"role": "user", "content": "go"}], _EMIT,
        max_turns=50, stuck_detection=False,
        finalize=lambda a: "emitted", fallback=lambda m: "fallback",
        on_budget=lambda p: seen.append(p), **kw)
    return seen


def test_the_loop_stops_when_the_session_has_spent_its_ceiling():
    client = _SpendingClient(per_turn=0.05)
    seen = _drive(client, cost_budget_usd=0.20)
    kinds = [p.get("kind") for p in seen]
    assert "cost" in kinds, f"the loop never reported a money cutoff: {seen}"
    assert client.turns <= 7, (
        f"ran {client.turns} turns at $0.05 each against a $0.20 ceiling -- it did not stop"
    )


def test_it_counts_only_this_session_not_what_the_run_already_spent():
    """The accountant arrives carrying $0.60 of an earlier phase; the ceiling is $0.20 for US."""
    client = _SpendingClient(per_turn=0.05, start=0.60)
    seen = _drive(client, cost_budget_usd=0.20)
    assert client.turns >= 4, (
        f"stopped after {client.turns} turns -- it read the RUN total, so a session that had not "
        "spent anything was cut before its first turn"
    )
    assert "cost" in [p.get("kind") for p in seen]


def test_a_zero_ceiling_does_not_bound_anything():
    client = _SpendingClient(per_turn=0.50)
    seen = _drive(client, cost_budget_usd=0.0)
    assert "cost" not in [p.get("kind") for p in seen], \
        "0.0 must mean OFF, or every session that never asked for a ceiling acquires one"
    assert client.turns == 50, f"the loop stopped early for money it was never given: {client.turns}"


def test_a_client_with_no_accountant_is_never_cut_for_money():
    class _Plain(_SpendingClient):
        """No accountant at all -- and its `chat` must not touch one either."""
        def __init__(self):
            super().__init__(per_turn=0.0)
            del self.accountant

        def chat(self, messages, tools, tool_choice="auto"):
            self.turns += 1
            return {"content": "", "tool_calls": [
                {"id": f"c{self.turns}", "function": {"name": "work", "arguments": "{}"}}]}

    client = _Plain()
    seen = _drive(client, cost_budget_usd=0.01)
    assert "cost" not in [p.get("kind") for p in seen], \
        "a missing accountant read as $0 spent and cut the session on its first turn"


def test_the_cutoff_says_how_much_it_was():
    client = _SpendingClient(per_turn=0.05)
    seen = _drive(client, cost_budget_usd=0.20)
    cost = [p for p in seen if p.get("kind") == "cost"][0]
    assert "detail" in cost and "$" in cost["detail"], (
        f"the money cutoff carries no figure, so the row cannot say what it cost: {cost}"
    )
