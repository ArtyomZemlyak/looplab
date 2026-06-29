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
