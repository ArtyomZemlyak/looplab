"""RepoScoutTools — the read-only filesystem tools the genesis BOSS uses to inspect a repo before
authoring a task. Must read text, refuse secrets/binaries, and never escape its allowed roots."""
from __future__ import annotations

from looplab.tools.reposcout import RepoScoutTools


def _repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    (r / "README.md").write_text("# Project\nBEST TRAIN: python train.py --epochs 50\n", encoding="utf-8")
    (r / "test.py").write_text('print({"recall@100": 0.91})\n', encoding="utf-8")
    (r / "src" / "model.py").write_text("class M: pass\n", encoding="utf-8")
    (r / ".env").write_text("LOOPLAB_LLM_API_KEY=sk-super-secret\n", encoding="utf-8")
    (r / "weights.bin").write_bytes(b"\x00\x01\x02\x03")
    (tmp_path / "outside.txt").write_text("should be unreachable", encoding="utf-8")
    return r


def test_list_and_read_text(tmp_path):
    r = _repo(tmp_path)
    t = RepoScoutTools([r])
    listing = t.execute("list_dir", {"path": str(r)})
    assert "README.md" in listing and "DIR  src/" in listing and "test.py" in listing
    readme = t.execute("read_file", {"path": str(r / "README.md")})
    assert "BEST TRAIN: python train.py" in readme
    assert "recall@100" in t.execute("read_file", {"path": str(r / "test.py")})


def test_find_files(tmp_path):
    r = _repo(tmp_path)
    t = RepoScoutTools([r])
    hits = t.execute("find_files", {"root": str(r), "pattern": "**/*.py"})
    assert "test.py" in hits and "model.py" in hits


def test_refuses_secret_file(tmp_path):
    r = _repo(tmp_path)
    t = RepoScoutTools([r])
    out = t.execute("read_file", {"path": str(r / ".env")})
    assert "refused" in out.lower()
    assert "sk-super-secret" not in out          # the credential never reaches the model


def test_binary_is_reported_not_read(tmp_path):
    r = _repo(tmp_path)
    t = RepoScoutTools([r])
    out = t.execute("read_file", {"path": str(r / "weights.bin")})
    assert "not read" in out and "exists" in out


def test_cannot_escape_allowed_roots(tmp_path):
    r = _repo(tmp_path)
    t = RepoScoutTools([r])               # root is the repo, NOT its parent
    assert "not allowed" in t.execute("read_file", {"path": str(tmp_path / "outside.txt")}).lower()
    assert "not allowed" in t.execute("read_file", {"path": str(r / ".." / "outside.txt")}).lower()
    assert "not allowed" in t.execute("list_dir", {"path": str(tmp_path)}).lower()


def test_unknown_tool_and_missing_path(tmp_path):
    t = RepoScoutTools([tmp_path])
    assert "unknown tool" in t.execute("nope", {})
    assert "no such" in t.execute("read_file", {"path": str(tmp_path / "ghost.py")}).lower()


def test_secret_variants_refused_and_hidden(tmp_path):
    """The secret gate is more than `.env`: credential file variants are refused AND hidden from
    list_dir/find_files (so neither contents NOR names reach the model)."""
    r = tmp_path / "repo"; (r / ".ssh").mkdir(parents=True)
    (r / "secrets.yaml").write_text("api_key: sk-x", encoding="utf-8")
    (r / "client_secret.json").write_text('{"k": "sk-y"}', encoding="utf-8")
    (r / ".git-credentials").write_text("https://x:ghp_tok@h", encoding="utf-8")
    (r / ".ssh" / "id_rsa").write_text("PRIVATE-KEY", encoding="utf-8")
    (r / "README.md").write_text("ok", encoding="utf-8")
    t = RepoScoutTools([r])
    for f, leak in [("secrets.yaml", "sk-x"), ("client_secret.json", "sk-y"),
                    (".git-credentials", "ghp_tok"), (".ssh/id_rsa", "PRIVATE-KEY")]:
        out = t.execute("read_file", {"path": str(r / f)})
        assert ("refused" in out.lower() or "not read" in out.lower()) and leak not in out, f
    listing = t.execute("list_dir", {"path": str(r)})
    assert "README.md" in listing
    assert "secrets.yaml" not in listing and ".git-credentials" not in listing and ".ssh" not in listing
    finds = t.execute("find_files", {"root": str(r), "pattern": "**/*"})
    assert "id_rsa" not in finds and "secrets.yaml" not in finds


def test_env_example_readable_but_env_refused(tmp_path):
    """`.env.example` (a non-secret template the boss wants) is readable; the real `.env` is refused."""
    r = tmp_path / "repo"; r.mkdir()
    (r / ".env.example").write_text("LOOPLAB_LLM_MODEL=your-model\n", encoding="utf-8")
    (r / ".env").write_text("LOOPLAB_LLM_API_KEY=sk-real\n", encoding="utf-8")
    t = RepoScoutTools([r])
    assert "your-model" in t.execute("read_file", {"path": str(r / ".env.example")})
    assert "sk-real" not in t.execute("read_file", {"path": str(r / ".env")})


def test_read_is_allowlist_no_false_positive_on_token(tmp_path):
    """read_file is an allowlist (unknown extensions not read), but a legit code file whose NAME
    contains 'token' (tokenizer.py — common in NLP repos) must still be readable."""
    r = tmp_path / "repo"; r.mkdir()
    (r / "tokenizer.py").write_text("# tokenize text\n", encoding="utf-8")
    (r / "model.bin").write_bytes(b"\x00\x01\x02")
    (r / "Makefile").write_text("build:\n\techo hi\n", encoding="utf-8")
    t = RepoScoutTools([r])
    assert "tokenize text" in t.execute("read_file", {"path": str(r / "tokenizer.py")})
    assert "exists, not read" in t.execute("read_file", {"path": str(r / "model.bin")})
    assert "build:" in t.execute("read_file", {"path": str(r / "Makefile")})   # safe extensionless name


def test_read_file_page_fits_the_loop_cap_and_ends_with_the_resume_marker(tmp_path):
    """P3: the agent loop hard-caps every tool result at 4000 chars and cuts the TAIL — so one
    read_file page (header + body + marker) must fit UNDER that with the resume marker intact, and
    following the marker's start_line must walk the whole file to a final page WITHOUT a marker
    (marker absence == end of file, the documented contract)."""
    import re
    r = tmp_path / "repo"
    r.mkdir()
    body = "\n".join(f"line {i}: " + "x" * 90 for i in range(400)) + "\n"     # ~40KB, 400 lines
    (r / "big.py").write_text(body, encoding="utf-8")
    t = RepoScoutTools(roots=[str(r)], default_root=str(r))
    page = t.execute("read_file", {"path": "big.py"})
    assert len(page) <= 3900, f"page too big for the loop cap: {len(page)}"
    m = re.search(r"continue with start_line=(\d+)\)$", page)
    assert m, f"a continuing page must END with the resume marker; tail was: {page[-80:]!r}"
    assert "line 0:" in page                                    # page 1 really starts at the top
    hops = 0
    while m:
        hops += 1
        assert hops < 50, "pagination did not terminate"
        page = t.execute("read_file", {"path": "big.py", "start_line": int(m.group(1))})
        assert len(page) <= 3900
        m = re.search(r"continue with start_line=(\d+)\)$", page)
    assert "line 399:" in page          # the true tail is reachable; the final page has no marker


def test_read_file_explicit_window_reports_range(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    (r / "f.py").write_text("".join(f"l{i}\n" for i in range(10)), encoding="utf-8")
    t = RepoScoutTools(roots=[str(r)], default_root=str(r))
    out = t.execute("read_file", {"path": "f.py", "start_line": 3, "lines": 4})
    assert out.startswith("(lines 3-6 of 10)") and "l2\n" in out and "l5" in out and "l6" not in out
    assert "continue with start_line=7" in out                  # window ended before EOF -> marker
    tail = t.execute("read_file", {"path": "f.py", "start_line": 7})
    assert "l9" in tail and "more below" not in tail            # EOF page carries NO marker


def test_read_file_header_agrees_with_marker_when_the_char_cap_cuts_the_window(tmp_path):
    """F13: the `(lines A-B of N)` header used to be computed from the PRE-cap line window, so a
    window that ran into the char cap claimed a range it never showed. The header is now computed
    from the actual post-cap `shown` count, so header end + 1 == the resume marker's start_line."""
    import re
    r = tmp_path / "repo"
    r.mkdir()
    (r / "long.py").write_text("".join(f"line {i}: " + "y" * 90 + "\n" for i in range(200)),
                               encoding="utf-8")
    t = RepoScoutTools(roots=[str(r)], default_root=str(r))
    out = t.execute("read_file", {"path": "long.py", "start_line": 1, "lines": 150})  # ~15KB asked
    hm = re.match(r"\(lines 1-(\d+) of 200\)", out)
    mm = re.search(r"continue with start_line=(\d+)\)$", out)
    assert hm and mm, out[:120]
    shown_to = int(hm.group(1))
    assert shown_to < 150                                # the char cap really cut the line window
    assert int(mm.group(1)) == shown_to + 1              # header and resume marker agree


def test_read_file_single_overlong_line_still_progresses(tmp_path):
    """F5: a single line longer than one page yielded shown=0 and a resume marker pointing at the
    SAME start_line — an infinite identical-page loop. The page must keep the truncated line's head,
    say honestly that it was cut mid-line, and resume at the NEXT line so pagination progresses."""
    import re
    r = tmp_path / "repo"
    r.mkdir()
    body = "X = '" + "a" * 5000 + "'\n" + "".join(f"after{i} = {i}\n" for i in range(5))
    (r / "wide.py").write_text(body, encoding="utf-8")
    t = RepoScoutTools(roots=[str(r)], default_root=str(r))
    p1 = t.execute("read_file", {"path": "wide.py"})
    assert len(p1) <= 4000                               # still fits the loop's RESULT_CAP
    assert "line 1 is longer than one page" in p1 and "NOT reachable by line windows" in p1
    m = re.search(r"continue with start_line=(\d+)\)$", p1)
    assert m and int(m.group(1)) == 2                    # progress: the NEXT line, not the same one
    p2 = t.execute("read_file", {"path": "wide.py", "start_line": 2})
    assert p2 != p1                                      # page 2 differs — no identical-page loop
    assert "after0" in p2 and "after4" in p2 and "more below" not in p2


def test_mid_line_marker_ends_with_the_canonical_stem(tmp_path):
    """H4a: the mid-line marker must keep the documented '… (more below — continue with
    start_line=N)' STEM (the explanation precedes it), so "a reply WITHOUT the marker IS the end"
    stays true for a reader matching the canonical stem — the mid-line case included."""
    import re
    r = tmp_path / "repo"
    r.mkdir()
    (r / "wide.py").write_text("X = '" + "a" * 5000 + "'\nY = 1\n", encoding="utf-8")
    t = RepoScoutTools(roots=[str(r)], default_root=str(r))
    p1 = t.execute("read_file", {"path": "wide.py"})
    assert re.search(r"… \(more below — continue with start_line=2\)$", p1), p1[-120:]


def test_char_cap_cut_drops_the_partial_trailing_line(tmp_path):
    """H4b: when the char cap cuts a multi-line window mid-line, the partial fragment past the last
    complete line is DROPPED — header, body, and marker then agree on whole lines, and the next page
    re-serves that line in full (no half-line shown, nothing double-served)."""
    import re
    r = tmp_path / "repo"
    r.mkdir()
    (r / "long.py").write_text("".join(f"line {i}: " + "y" * 90 + "\n" for i in range(200)),
                               encoding="utf-8")
    t = RepoScoutTools(roots=[str(r)], default_root=str(r))
    out = t.execute("read_file", {"path": "long.py", "start_line": 1, "lines": 150})
    hm = re.match(r"\(lines 1-(\d+) of 200\)", out)
    mm = re.search(r"continue with start_line=(\d+)\)$", out)
    assert hm and mm, out[:120]
    shown_to = int(hm.group(1))
    assert shown_to < 150                                     # the char cap really cut the window
    assert f"line {shown_to - 1}: " in out                    # last complete line survives (0-based)
    assert f"\nline {shown_to}:" not in out                   # the next line's fragment is dropped
    nxt = t.execute("read_file", {"path": "long.py", "start_line": int(mm.group(1)), "lines": 2})
    assert f"line {shown_to}: " + "y" * 90 in nxt             # re-served IN FULL on the next page
