"""The web tool's DENY-LIST (`EvalSpec.web_deny`, `tools/web.py`) — item A3 of docs/60 §60.9.

Measured over the AlgoTune probe corpus (docs/56 §150 finding 13): 52 of 76 runs fetched the
published solver source for the task they were being graded on, through `web_fetch`, while the
card's fence was prose. The fence is now an OPERATOR DECLARATION, validated at submit like
`protect_packages`, threaded to every site that composes `WebTools`, refused by name, and stamped
on the tool span — and every property here is DRIVEN (CLAUDE.md's tier 1): a fetch that must be
refused is refused with the network seam armed to fail the test if it is ever touched.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

import looplab.tools.web as web
from looplab.tools._base import ToolResult
from looplab.tools.web import (WebDenyRefusal, WebTools, normalize_web_deny, task_web_deny,
                               web_deny_match)

ROOT = Path(__file__).resolve().parents[1]
MAKE_TASK = ROOT / "benchmarks" / "algotune" / "make_task.py"

_SOLVER = "https://github.com/oripress/AlgoTune/blob/main/results/o4-mini/kd_tree/solver.py"
_DENY = ("https://github.com/oripress/AlgoTune/", "https://algotune.io")


# ------------------------------------------------------------------------------- the match rule

def test_a_prefix_is_validated_at_submit_and_normalized_once():
    assert normalize_web_deny([" HTTPS://GitHub.com/oripress/AlgoTune/ ",
                               "https://github.com/oripress/AlgoTune/"]) == (
        "https://github.com/oripress/AlgoTune/",)
    for bad in ("algotune.io", "/results/", "ftp://x/y", "https://", "", "   "):
        with pytest.raises(ValueError, match="web_deny"):
            normalize_web_deny([bad])


def test_the_match_covers_the_host_its_subdomains_and_the_path_prefix_only():
    deny = normalize_web_deny(list(_DENY))
    assert web_deny_match(_SOLVER, deny) == "https://github.com/oripress/AlgoTune/"
    # Scheme is a transport, not a resource; a redirect makes them one page anyway.
    assert web_deny_match("http://github.com/oripress/AlgoTune/tree/main/results/", deny)
    # Case: over-fencing is the harmless direction, `/OriPress/` walking around is not.
    assert web_deny_match("https://GITHUB.com/OriPress/AlgoTune/blob/x", deny)
    # A no-path prefix covers the host and its subdomains — and NOT a host that merely starts
    # with the same letters, which a bare `startswith` on the string would have admitted.
    assert web_deny_match("https://www.algotune.io/leaderboard", deny) == "https://algotune.io"
    assert web_deny_match("https://algotune.io.evil.example/", deny) is None
    # A trailing slash bounds the prefix to that directory: a fork is not the repo.
    assert web_deny_match("https://github.com/oripress/AlgoTune-fork/x", deny) is None
    assert web_deny_match("https://github.com/oripress/", deny) is None
    assert web_deny_match("https://arxiv.org/abs/2507.15887", deny) is None
    assert web_deny_match("not a url", deny) is None


def test_a_path_that_cannot_be_canonicalized_fails_CLOSED_on_the_declared_host():
    """`..` was the one spelling that walked around a prefix, and it walked the PERMISSIVE way.

    `_host_path` answers `parts is None` for a path carrying a `..` segment — deliberately, since a
    path that climbs out is not the prefix's page and resolving it would be reinterpreting the
    operator's declaration. `web_deny_match` then returned None, and both callers read None as "no
    declared prefix covers this URL": `_fetch` does `if prefix: raise`, `_search` does
    `if web_deny_match(...): continue`. So one extra segment fetched the graded task's published
    solver, and GitHub — like nginx and most CDNs — normalizes the request-URI itself and serves the
    denied page with NO redirect, so `_SSRFRedirectHandler`'s per-hop re-check never fires either.

    The sibling above pins the harmless direction (`%6F`, `//`, `/./`, `/OriPress/`, the trailing-dot
    FQDN all over-fence); this is the same rule applied to the one case that went the other way.
    """
    deny = normalize_web_deny(list(_DENY))
    assert web_deny_match("https://github.com/oripress/AlgoTune/x/../solver.py", deny) == \
        "https://github.com/oripress/AlgoTune/"
    assert web_deny_match("https://github.com/foo/../oripress/AlgoTune/solver.py", deny) == \
        "https://github.com/oripress/AlgoTune/"
    # Encoded, because one `unquote` pass happens before the segments are split.
    assert web_deny_match("https://github.com/oripress/AlgoTune/x/%2E%2E/solver.py", deny)
    # …and it fences by HOST only: a `..` on a host no prefix declares is nobody's business here.
    assert web_deny_match("https://arxiv.org/a/../abs/2507.15887", deny) is None


# --------------------------------------------------------------------------------- the refusal

def _armed(monkeypatch):
    """Arm the network seam to FAIL the test if a refused URL is ever fetched."""
    calls: list[str] = []

    def _get(self, url, data=None):
        calls.append(url)
        return "<html><body>PUBLISHED SOLVER SOURCE</body></html>"

    monkeypatch.setattr(WebTools, "_get", _get)
    return calls


def test_a_fetch_under_a_declared_prefix_is_refused_by_name_and_never_downloaded(monkeypatch):
    calls = _armed(monkeypatch)
    out = WebTools(deny=_DENY).execute("web_fetch", {"url": _SOLVER})
    assert calls == [], "a refused URL was DOWNLOADED"
    assert "PUBLISHED SOLVER SOURCE" not in out
    # It NAMES the declaration and the prefix, and says the page exists — never "(unavailable)",
    # which reads as a transport failure and sends the model to a mirror.
    assert "web_deny" in out and "https://github.com/oripress/AlgoTune/" in out and _SOLVER in out
    assert "refused" in out and "fence" in out
    assert "unavailable" not in out and "unreachable URL" in out


def test_a_fetch_outside_every_prefix_is_the_historical_fetch(monkeypatch):
    calls = _armed(monkeypatch)
    out = WebTools(deny=_DENY).execute("web_fetch", {"url": "https://arxiv.org/abs/2507.15887"})
    assert calls == ["https://arxiv.org/abs/2507.15887"]
    assert out == "PUBLISHED SOLVER SOURCE"


def test_the_refusal_is_data_on_the_result_so_the_tool_span_can_count_it(monkeypatch):
    """`agents/tool_loop.py::_run_tool_call` stamps every key of `trace_attributes()` on the tool
    span as `result_<key>`, so the prefix that fired lands as `result_structured.web_fetch_refused`
    — the count doc 60 A3's acceptance ("= 0 on a control run") needs to exist."""
    _armed(monkeypatch)
    res = WebTools(deny=_DENY).execute_result("web_fetch", {"url": _SOLVER})
    assert isinstance(res, ToolResult) and res.is_error and res.retryable is False
    attrs = res.trace_attributes()
    assert attrs["structured"]["web_fetch_refused"] == "https://github.com/oripress/AlgoTune/"
    assert attrs["structured"]["refused"] == "web_deny" and attrs["structured"]["url"] == _SOLVER
    assert attrs["provenance"]["fence"] == "web_deny"
    # …and the string view the model reads is exactly the typed result's content.
    assert WebTools(deny=_DENY).execute("web_fetch", {"url": _SOLVER}) == res.content
    # A page that was fetched carries no such key: the stamp is a claim only a refusal makes.
    ok = WebTools(deny=_DENY).execute_result("web_fetch", {"url": "https://arxiv.org/abs/1"})
    assert not ok.is_error and "structured" not in ok.trace_attributes()


def test_a_redirect_into_a_denied_prefix_is_refused_on_the_hop(monkeypatch):
    """A short-link or a mirror that 302s INTO the repository is the denied page one hop later,
    and the preflight over the caller's URL cannot see it — the redirect handler re-checks."""
    handler = web._SSRFRedirectHandler(deny=normalize_web_deny(list(_DENY)))
    with pytest.raises(WebDenyRefusal) as refused:
        handler.redirect_request(None, None, 302, "Found", {}, _SOLVER)
    assert refused.value.prefix == "https://github.com/oripress/AlgoTune/"

    # …and the refusal reaches the caller as the deny sentence, not as "(web fetch unavailable)".
    def _get(self, url, data=None):
        raise WebDenyRefusal(_SOLVER, "https://github.com/oripress/AlgoTune/")

    monkeypatch.setattr(WebTools, "_get", _get)
    out = WebTools(deny=_DENY).execute("web_fetch", {"url": "https://short.example/abc"})
    assert "unavailable" not in out and "web_deny" in out and _SOLVER in out


def test_an_empty_declaration_is_the_historical_tool_byte_for_byte():
    plain, fenced = WebTools(), WebTools(deny=_DENY)
    assert plain.deny == () and plain._opener is web._SSRF_OPENER
    assert plain.specs() == WebTools(deny=[]).specs()
    # The fenced spec tells the model the SHAPE of the refusal, never the list — naming the
    # prefixes would hand it the map of what to look for.
    fenced_text = json.dumps(fenced.specs())
    assert "fenced by the operator" in fenced_text
    assert "github.com" not in fenced_text and "algotune" not in fenced_text
    assert fenced._opener is not web._SSRF_OPENER


# ------------------------------------------------------------------------ the declaration

def test_eval_spec_carries_and_validates_the_declaration():
    from pydantic import ValidationError

    from looplab.adapters.repo_task import EvalSpec

    assert EvalSpec(command=["python", "x.py"]).web_deny == []
    spec = EvalSpec(command=["python", "x.py"], web_deny=[" https://ALGOTUNE.io/ "])
    assert spec.web_deny == ["https://algotune.io/"]
    with pytest.raises(ValidationError, match="web_deny"):
        EvalSpec(command=["python", "x.py"], web_deny=["algotune.io"])


def test_the_task_hook_is_total_and_quiet_like_the_grader_fence():
    class _Task:
        def __init__(self, spec):
            self._spec = spec

        def eval_spec(self):
            return self._spec

    class _Raises:
        def eval_spec(self):
            raise RuntimeError("no adapter")

    assert task_web_deny(_Task({"command": ["x"], "web_deny": list(_DENY)})) == normalize_web_deny(
        list(_DENY))
    assert task_web_deny(_Task({"command": ["x"]})) == ()
    assert task_web_deny(_Raises()) == ()
    assert task_web_deny(None) == ()
    # A declaration that does not validate fences nothing rather than breaking tool construction:
    # the SUBMIT-time validator is where an operator hears about it.
    assert task_web_deny(_Task({"web_deny": ["algotune.io"]})) == ()


def test_every_site_that_composes_the_web_tool_goes_through_the_one_fenced_constructor():
    """The hole was two `WebTools(enabled=True)` sites that never saw the task. AST, so a
    commented-out call cannot satisfy it, and every future site inherits the rule or goes red:
    the agent layer may not construct `WebTools` at all — only `tools/web.py::build_web_tools`
    does, and it is the one place the declaration is read."""
    from looplab.tools.web import build_web_tools

    class _Task:
        def eval_spec(self):
            return {"command": ["x"], "web_deny": list(_DENY)}

    tools = build_web_tools(_Task())
    assert isinstance(tools, WebTools) and tools.deny == normalize_web_deny(list(_DENY))

    for rel in ("looplab/agents/factory.py", "looplab/agents/deep_research.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        tree = ast.parse(src)
        bare = [f"{rel}:{node.lineno}" for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "WebTools"]
        assert bare == [], f"WebTools built outside build_web_tools (no web_deny): {bare}"
        called = [node for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                  and node.func.id == "build_web_tools"]
        assert called, f"{rel} no longer composes the web tool through build_web_tools"


# ---------------------------------------------------------------- the AlgoTune declaration

def _make_task_module():
    spec = importlib.util.spec_from_file_location("_algotune_make_task_web_deny", MAKE_TASK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_algotune_bridge_declares_where_its_solutions_are_published():
    """The benchmark's own task template must carry the declaration — the fence is only as good as
    the operator remembering to state it, and this is the operator we control. The prefixes are
    checked against the REAL shapes: the `results/<model>/<task>/` tree under a ref, the raw host,
    the leaderboard site and the dataset that holds the graded instances."""
    mod = _make_task_module()
    deny = normalize_web_deny(mod.web_deny_prefixes("kd_tree"))
    for url in (_SOLVER,
                "https://github.com/oripress/AlgoTune/tree/abc123def/results/gpt-4o/kd_tree/",
                "https://raw.githubusercontent.com/oripress/AlgoTune/main/results/gpt-4o/kd_tree/solver.py",
                "https://github.com/oripress/AlgoTune",           # the README's leaderboard table
                "https://algotune.io/leaderboard", "https://www.algotune.io/",
                "https://huggingface.co/datasets/oripress/AlgoTune/tree/main/data/kd_tree"):
        assert web_deny_match(url, deny), f"not fenced: {url}"
    # The paper is NOT fenced: it describes the benchmark, not the answers.
    assert web_deny_match("https://arxiv.org/abs/2507.15887", deny) is None
    # …and the declaration validates through the field it lands in.
    from looplab.adapters.repo_task import EvalSpec
    assert EvalSpec(command=["x"], web_deny=mod.web_deny_prefixes("kd_tree")).web_deny


def test_the_generated_task_file_carries_the_declaration(tmp_path):
    """Through the real generator, end to end, so a refactor of the spec dict cannot drop it."""
    root = tmp_path / "AlgoTune"
    task_dir = root / "AlgoTuneTasks" / "fake_task"
    task_dir.mkdir(parents=True)
    (task_dir / "description.txt").write_text("Find the thing.\n", encoding="utf-8")
    (task_dir / "fake_task.py").write_text("class FakeTask:\n    pass\n", encoding="utf-8")
    out = tmp_path / "ws"
    subprocess.run([sys.executable, str(MAKE_TASK), "--algotune-root", str(root),
                    "--task", "fake_task", "--out-dir", str(out), "--no-full-context"],
                   check=True, capture_output=True, text=True)
    spec = json.loads((out / "algotune_fake_task.json").read_text(encoding="utf-8"))
    deny = normalize_web_deny(spec["eval"]["web_deny"])
    assert web_deny_match(_SOLVER, deny) and web_deny_match("https://algotune.io/x", deny)
    assert spec["eval"]["protect_packages"] == ["AlgoTuner", "AlgoTuneTasks"]


def test_a_refused_SEARCH_is_a_tool_result_and_not_an_escaping_exception():
    """`_search` re-raises `WebDenyRefusal` so it is not swallowed into "(web search unavailable)",
    and NOTHING above caught it: `execute_result`'s `web_search` branch had no try, and neither
    `_run_tool_call` nor `drive_tool_loop` has one.

    Driven: the exception escaped the provider entirely. Inside `DeepResearcher.research` the outer
    `except Exception` then discards the WHOLE paid session as "(deep research unavailable: …)" —
    the "(unreachable)" shape this module's docstring says the fence must never produce — plus a
    lost memo and a dangling tool_call_id. Reachable whenever a search redirects into a declared
    prefix, or when the search endpoint itself is one.

    MUTATION: drop the `except WebDenyRefusal` from the `web_search` branch -> this raises instead
    of returning, and the refusal stops being countable on the span.
    """
    from looplab.tools.web import WebDenyRefusal, WebTools

    prefix = "https://html.duckduckgo.com/"
    tools = WebTools(enabled=True, deny=(prefix,))
    tools._get = lambda url, data=None: (_ for _ in ()).throw(
        WebDenyRefusal("https://html.duckduckgo.com/html/", prefix))

    result = tools.execute_result("web_search", {"query": "algotune solver"})

    assert result.is_error is True and result.retryable is False
    # The SAME structured answer `web_fetch` gives, so one query counts the refusals of both.
    assert result.structured["refused"] == "web_deny"
    assert result.structured["web_fetch_refused"] == prefix
    assert "refused" in str(result.content) and "unavailable" not in str(result.content)


def test_a_dropped_result_does_not_hand_the_next_row_the_denied_page_s_snippet():
    """The other half of the search fence: the rows that SURVIVE must still describe themselves.

    `_RESULT` and `_SNIPPET` are two scans of the same page that pair up positionally. The snippet
    lookup was keyed on `len(out)` — the count of KEPT rows — which is the same number as the
    enumeration index only while nothing is ever skipped, and the deny filter's `continue` is the
    skip. With the denied row dropped, every later result was rendered under its own URL carrying
    the PREVIOUS row's snippet: up to 300 characters of the denied page's text, attributed to an
    allowed URL, inside the tool result the fence exists to keep it out of.
    """
    tools = WebTools(enabled=True, deny=("https://github.com/oripress/AlgoTune/",))
    tools._get = lambda url, data=None: (
        '<a class="result__a" href="https://github.com/oripress/AlgoTune/solver.py">Solver</a>'
        '<a class="result__snippet">PUBLISHED SOLVER SOURCE</a>'
        '<a class="result__a" href="https://arxiv.org/abs/1">A paper</a>'
        '<a class="result__snippet">an abstract about trees</a>'
        '<a class="result__a" href="https://example.com/blog">A blog</a>'
        '<a class="result__snippet">a blog post about trees</a>')

    out = tools.execute("web_search", {"query": "kd tree solver"})

    assert "PUBLISHED SOLVER SOURCE" not in out, "the denied page's snippet reached the model"
    assert "1 result(s) withheld" in out
    # Each surviving row keeps ITS OWN snippet, and the ordinals stay dense.
    assert "1. A paper\n   https://arxiv.org/abs/1\n   an abstract about trees" in out
    assert "2. A blog\n   https://example.com/blog\n   a blog post about trees" in out


def test_a_declaration_the_matcher_cannot_honour_is_refused_or_narrowed_at_the_task_file():
    """The DECLARATION side of the same rule the URL side got on 2026-09-08.

    `web_deny_match` skips a prefix whose own path carries `..` (`p_parts is None` -> `continue`),
    which is FAIL-OPEN: the operator's entry fenced nothing at all while the task file said it did
    — the exact failure `normalize_web_deny`'s docstring cites `envsafe.validate_env_map` for. And
    a QUERY was kept in the stored form while the comparison reads neither side's query, so the
    entry read as "this page with this query" and covered the whole path.
    """
    import pytest

    from looplab.tools.web import normalize_web_deny, web_deny_match

    # 1. `..` in a DECLARED prefix: refused where the operator can see it, not skipped in silence.
    for bad in ("https://github.com/oripress/../oripress/AlgoTune/",
                "https://github.com/oripress/AlgoTune/..",
                "https://github.com/a/%2E%2E/b/"):
        with pytest.raises(ValueError) as exc:
            normalize_web_deny([bad])
        assert "`..`" in str(exc.value) and bad in str(exc.value), str(exc.value)
    # …and the fail-open behaviour it replaces, shown directly against the raw matcher.
    assert web_deny_match("https://github.com/oripress/AlgoTune/solver.py",
                          ["https://github.com/oripress/../oripress/AlgoTune/"]) is None, (
        "an unnormalized `..` prefix still fences nothing — which is why the refusal is upstream")

    # 2. a QUERY: dropped, so the STORED form is the COMPARED form and every message that names
    # the prefix names what the fence actually does.
    norm = normalize_web_deny(["https://kaggle.com/c/x/leaderboard?tab=public#top"])
    assert norm == ("https://kaggle.com/c/x/leaderboard",), norm
    assert web_deny_match("https://kaggle.com/c/x/leaderboard?tab=private", norm) == norm[0], (
        "the fence covers the path either way — the declaration must SAY so")
