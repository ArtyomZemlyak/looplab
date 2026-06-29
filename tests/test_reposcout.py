"""RepoScoutTools — the read-only filesystem tools the genesis BOSS uses to inspect a repo before
authoring a task. Must read text, refuse secrets/binaries, and never escape its allowed roots."""
from __future__ import annotations

from looplab.reposcout import RepoScoutTools


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
