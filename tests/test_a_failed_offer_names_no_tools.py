"""Guard: a failed offer names NOTHING (driven through the real block builder)."""
from looplab.agents.answered_by_context import answered_by_context, collect_inventory


class _Boom:
    """`specs()` raises; `inventory()` still answers for tools the failed offer never carried."""
    def specs(self):
        raise RuntimeError("provider blew up building the offer")

    def inventory(self):
        return {"cross_run_search": 41, "read_asset": 0}

    def execute(self, name, args):
        return ""


def test_a_failed_offer_publishes_no_rows():
    assert collect_inventory(_Boom()) == {"cross_run_search": 41, "read_asset": 0}, (
        "premise: the inventory hook answers, so the fallback has something to over-credit")
    assert answered_by_context(_Boom()) == "", (
        "an offer that raised carried nothing; naming a tool the model was not offered is the "
        "under-count defect this module exists to prevent")


def test_a_bare_provider_with_no_specs_hook_keeps_its_rows():
    class _Bare:
        def inventory(self):
            return {"cross_run_search": 41}

        def execute(self, name, args):
            return ""

    assert "cross_run_search=41" in answered_by_context(_Bare()), (
        "the no-specs()-at-all case is a different one and must keep its pre-existing behaviour")
