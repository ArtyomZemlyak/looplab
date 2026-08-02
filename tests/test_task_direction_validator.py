"""Every task model rejects an unknown optimization direction (doc 25 RA-06).

A typo'd direction is an OBJECTIVE FLIP and nothing downstream can detect it: the search compares
candidates in the wrong sense, chases its worst one and reports it as best, with every number
involved genuinely computed — so no metric, gate or report looks wrong. The validator used to exist
on 2 of the 9 registered task models; the other seven stored `direction="mxa"` verbatim.
"""
from __future__ import annotations

import pytest

def test_every_registered_task_model_rejects_an_unknown_direction():
    """A typo'd direction is an OBJECTIVE FLIP, and nothing downstream can detect it.

    `direction="mxa"` used to be stored verbatim by seven of the nine task models, because the
    validator lived on only two of them. The search then compares candidates in whichever sense the
    consumer assumes, chases its worst one, and reports it as best — with every number involved
    genuinely computed, so no metric, gate or report looks wrong.

    Discovered by import rather than listed by hand: a NEW adapter that forgets the validator has to
    fail here, which a hardcoded list of today's nine would not catch.
    """
    import importlib
    import pkgutil

    import pydantic

    import looplab.adapters as adapters

    checked = []
    for module_info in pkgutil.iter_modules(adapters.__path__):
        module = importlib.import_module(f"looplab.adapters.{module_info.name}")
        for attribute in dir(module):
            model = getattr(module, attribute, None)
            if not (isinstance(model, type) and issubclass(model, pydantic.BaseModel)):
                continue
            if model.__module__ != module.__name__ or "direction" not in model.model_fields:
                continue
            checked.append(f"{module_info.name}.{attribute}")
            with pytest.raises(Exception):
                model.model_validate({"id": "i", "goal": "g", "direction": "mxa"})

    assert len(checked) >= 9, f"expected every task model to be discovered, found {checked}"


def test_the_shared_direction_validator_names_what_it_received_and_allows_opt_in_extras():
    from looplab.core.models import DIRECTIONS, validate_direction

    assert DIRECTIONS == ("min", "max")
    assert validate_direction("min") == "min" and validate_direction("max") == "max"
    # `mlebench_real` resolves "auto" from the grader before any comparison, so it opts that in
    # explicitly rather than every model loosening to accept it.
    assert validate_direction("auto", extra=("auto",)) == "auto"
    with pytest.raises(ValueError, match="'mxa'"):
        validate_direction("mxa")
    with pytest.raises(ValueError, match="'auto'"):
        validate_direction("auto")           # NOT allowed unless the model opted in
