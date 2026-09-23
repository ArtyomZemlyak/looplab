"""A catch-everything handler the session ADDED, that keeps no record of what it caught, is bounced once.

Measured 2026-09-23 on a MiniOneRec inference run: a node guarded a new prefix-cached prefill with
`except BaseException:` and printed "verification raised". The guarded code died on `cache.key_cache`
(removed in transformers 5), the path switched itself off, and the node scored 1.004 with every list
byte-identical to its parent -- indistinguishable, on its row, from an idea that does not help. The
Developer has no shell; the cause came back only by re-running the node by hand with the handler
patched to print its traceback.
"""
from __future__ import annotations

import sys
from pathlib import Path

from looplab.core.models import Idea
from looplab.engine.repair_verify import silent_broad_fallbacks


def _bounce(new: str, old: str | None = None, path: str = "svc/engine.py") -> str:
    return silent_broad_fallbacks({path: new}, before=lambda _p: old)


# ------------------------------------------------------------ what counts as silent

def test_the_measured_case_is_bounced_and_named_by_line():
    new = (
        "def enable(self):\n"
        "    try:\n"
        "        self.cache = full_cache.key_cache\n"
        "    except BaseException:\n"
        "        # Any failure disables the path; baseline behavior is preserved.\n"
        "        print('RECO_PREFIX_CACHED_PREFILL disabled: verification raised', flush=True)\n"
        "        self.enabled = False\n")
    out = _bounce(new, old="def enable(self):\n    self.enabled = False\n")
    assert "svc/engine.py:4" in out and "except BaseException:" in out
    assert "traceback.format_exc()" in out
    assert "you will not be asked twice" in out


def test_every_broad_spelling_is_broad():
    for header in ("except:", "except Exception:", "except BaseException as err:",
                   "except (ValueError, Exception):", "except builtins.Exception:"):
        src = f"try:\n    f()\n{header}\n    pass\n"
        assert _bounce(src), header


def test_a_narrow_handler_is_not_this_rule_s_business():
    assert _bounce("try:\n    f()\nexcept KeyError:\n    pass\n") == ""


def test_any_record_of_the_cause_is_enough():
    recorded = (
        "except Exception:\n    raise",
        "except Exception as e:\n    print('fell back:', e)",
        "except Exception:\n    import traceback; traceback.print_exc()",
        "except Exception:\n    msg = traceback.format_exc()",
        "except Exception:\n    log.exception('fell back')",
        "except Exception:\n    log.warning('fell back', exc_info=True)",
        "except Exception:\n    info = sys.exc_info()",
        "except Exception as e:\n    raise RuntimeError('wrapped') from e",
    )
    for handler in recorded:
        src = "try:\n    f()\n" + handler + "\n"
        assert _bounce(src) == "", handler


def test_a_nested_function_does_not_record_for_the_handler_around_it():
    src = ("try:\n    f()\nexcept Exception:\n"
           "    def later():\n        import traceback; traceback.print_exc()\n"
           "    use_fallback = True\n")
    assert _bounce(src)


# ------------------------------------------------------------ only what the session added

def test_a_handler_the_file_already_had_is_not_the_session_s_to_answer_for():
    old = "try:\n    f()\nexcept Exception:\n    pass\n"
    assert _bounce(old + "\nx = 1\n", old=old) == ""


def test_a_second_copy_of_an_existing_handler_is_still_new():
    old = "try:\n    f()\nexcept Exception:\n    pass\n"
    assert _bounce(old + old, old=old)


def test_non_python_files_and_files_that_do_not_parse_are_left_alone():
    assert silent_broad_fallbacks({"notes.md": "except Exception:\n    pass\n"}) == ""
    assert _bounce("try:\nexcept Exception:\n  pass\n") == ""


def test_an_unreadable_original_counts_as_absent_rather_than_raising():
    def boom(_p):
        raise OSError("gone")
    out = silent_broad_fallbacks({"a.py": "try:\n    f()\nexcept Exception:\n    pass\n"},
                                 before=boom)
    assert "a.py:3" in out


def test_the_list_is_bounded():
    src = "".join(f"try:\n    f{i}()\nexcept Exception:\n    pass\n" for i in range(9))
    out = _bounce(src)
    assert out.count("svc/engine.py:") == 5 and "and 4 more" in out


# ------------------------------------------------------------ driven through the real build path

def _fresh_repo_dev(monkeypatch, *, writes, plan_steps=()):
    """A REAL `LLMRepoDeveloper.implement()` over the repo fixture, with only the documented
    `drive_tool_loop` seam faked -- and the fake CALLS the `validate` it is handed, which is the whole
    question (the shape `test_build_declares_a_script_it_never_wrote.py` established)."""
    import looplab.agents.agent as agent_mod

    refusals: list = []

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        name = emit_spec["function"]["name"]
        if name == "declare_stages":
            return finalize({"stages": []})
        if name == "propose_plan":
            return finalize({"steps": list(plan_steps)})
        for path, body in (writes or {}).items():
            tools.execute("write_file", {"path": path, "content": body})
        args = {"summary": "built it"}
        validate = opts.get("validate")
        if validate is not None and (refusal := validate(args)):
            refusals.append(refusal)
        return finalize(args)

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)

    from looplab.adapters.repo_task import EvalSpec, LLMRepoDeveloper, RepoTask
    fixture = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
    task = RepoTask(id="r", goal="g", direction="max", editable_path=str(fixture),
                    edit_surface=["*.py"], protect=[],
                    eval=EvalSpec(command=[sys.executable, "ttrain.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}))
    dev = LLMRepoDeveloper(object(), task, plan_decompose=bool(plan_steps), plan_min_steps=2)
    dev.implement(Idea(operator="draft", params={}, rationale="x"))
    return refusals


_SILENT = ("def fast():\n    try:\n        return new_path()\n"
           "    except Exception:\n        return old_path()\n")


def test_the_real_build_path_bounces_it(monkeypatch):
    refusals = _fresh_repo_dev(monkeypatch, writes={"solution.py": _SILENT})
    assert refusals and "solution.py:4" in refusals[0]


def test_the_last_plan_step_carries_it_too(monkeypatch):
    # Dicts, not strings: `_propose_plan` keeps only {title, detail} steps, and a list of strings
    # silently falls back to ONE session -- which would not test the last step at all.
    refusals = _fresh_repo_dev(monkeypatch, writes={"solution.py": _SILENT},
                               plan_steps=[{"title": "write the fast path", "detail": "d"},
                                           {"title": "wire it in", "detail": "d"}])
    assert refusals and "solution.py:4" in refusals[0]


def test_a_build_that_records_its_fallback_is_not_bounced(monkeypatch):
    recorded = _SILENT.replace("    except Exception:\n",
                               "    except Exception:\n        traceback.print_exc()\n")
    assert _fresh_repo_dev(monkeypatch, writes={"solution.py": recorded}) == []
