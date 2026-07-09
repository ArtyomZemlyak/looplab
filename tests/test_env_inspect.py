"""EnvInspectTools — read-only environment introspection the repo Developer uses to GROUND generated
code in the real installed API (instead of guessing a wrong-version arg, the #1 real-run failure)."""
from looplab.tools.env_inspect import EnvInspectTools


def _t():
    return EnvInspectTools()


def test_specs_expose_the_read_only_tools():
    names = [s["function"]["name"] for s in _t().specs()]
    assert names == ["pkg_info", "py_api", "read_installed", "grep_installed", "gpu_info"]


def test_pkg_info_reports_version_for_an_installed_dist():
    out = _t().execute("pkg_info", {"name": "pytest"})
    assert "pytest" in out and any(c.isdigit() for c in out)   # a version number is present


def test_pkg_info_handles_missing_package_gracefully():
    out = _t().execute("pkg_info", {"name": "definitely_not_a_real_pkg_xyz"})
    assert "not installed" in out


def test_py_api_lists_enum_valid_values():
    """The precision='16-mixed' case: an Enum's VALID VALUES are surfaced so the model picks a legal
    one instead of guessing."""
    out = _t().execute("py_api", {"target": "socket.AddressFamily"})   # a real stdlib IntEnum
    assert "VALID VALUES" in out and "AF_INET" in out


def test_py_api_gives_signature_for_a_function():
    out = _t().execute("py_api", {"target": "json.dumps"})
    assert "signature:" in out and "obj" in out


def test_py_api_unresolvable_is_reported_not_raised():
    out = _t().execute("py_api", {"target": "json.no_such_attr_here"})
    assert "could not resolve" in out


def test_read_installed_returns_module_source():
    out = _t().execute("read_installed", {"module": "json.decoder", "max_lines": 20})
    assert "json/decoder.py" in out and "of " in out   # header with path + total line count


def test_grep_installed_finds_a_symbol_in_package_source():
    out = _t().execute("grep_installed", {"query": "def dumps", "package": "json", "max_hits": 3})
    assert "dumps" in out and ".py:" in out


def test_grep_installed_missing_symbol_is_reported():
    out = _t().execute("grep_installed", {"query": "zzz_no_such_symbol_qq", "package": "json"})
    assert "not found" in out


def test_execute_unknown_tool_is_reported():
    assert "unknown tool" in _t().execute("nope", {})


def test_execute_never_raises_on_bad_args():
    # empty/garbage args must return a string, never raise (the tool loop must not crash)
    for name in ("pkg_info", "py_api", "read_installed", "grep_installed"):
        assert isinstance(_t().execute(name, {}), str)


def test_read_installed_page_fits_the_loop_cap_and_carries_the_resume_marker():
    """P3: read_installed used to return up to a 12KB / 300-line page with NO resume pointer — the
    agent loop's 4000-char cap silently amputated the tail. Now it paginates like read_file: one
    bounded page ending with '… (more below — continue with start_line=N)' when more source remains,
    and a final page with NO marker (marker absence == end of file)."""
    import re
    out = _t().execute("read_installed", {"module": "argparse"})     # a big stdlib module (~90KB)
    assert len(out) <= 3900, f"page too big for the loop cap: {len(out)}"
    m = re.search(r"continue with start_line=(\d+)\)$", out)
    assert m, f"a continuing page must END with the marker; tail was: {out[-80:]!r}"
    nxt = _t().execute("read_installed", {"module": "argparse", "start_line": int(m.group(1))})
    assert len(nxt) <= 3900 and "(lines " in nxt                     # windowed continuation


def test_read_installed_lines_param_and_max_lines_alias():
    # `lines` is the documented window name (consistent with read_file/repo_read); the old
    # `max_lines` spelling stays accepted so older transcripts/models don't break.
    a = _t().execute("read_installed", {"module": "json.decoder", "start_line": 3, "lines": 5})
    b = _t().execute("read_installed", {"module": "json.decoder", "start_line": 3, "max_lines": 5})
    assert a == b and "(lines 3-7 of" in a


def test_grep_installed_output_is_clamped_under_the_loop_cap():
    # Long site-packages paths make even the default 20 hits overflow the loop's 4000-char cap,
    # which would drop the tail (and any "(capped …)" note) SILENTLY — the tool clamps itself.
    out = _t().execute("grep_installed", {"query": "def ", "package": "json", "max_hits": 100})
    assert len(out) <= 3700
