"""Guard (driven): the two top-level depth spellings now refuse a double-setting."""
import pytest

from looplab.core.errors import ConfigRefusal
from looplab.core.llm import reasoning_body


@pytest.mark.parametrize("extra", [{"enable_thinking": True}, {"think": True}])
def test_a_top_level_depth_spelling_beside_llm_reasoning_is_refused(extra):
    with pytest.raises(ConfigRefusal) as excinfo:
        reasoning_body("qwen3-30b", "high", extra=extra)
    assert next(iter(extra)) in str(excinfo.value), "the refusal must name the field to edit"


def test_the_same_spellings_alone_still_reach_the_request():
    """`llm_reasoning` unset is not a clash — `extra` is the documented escape hatch."""
    assert reasoning_body("qwen3-30b", "", extra={"think": True}) == {"think": True}
