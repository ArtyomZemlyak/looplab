"""Guard (driven): the bridge's per-eval PIP_TARGET dir is swept, and kept under the keep flag."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _bridge():
    path = ROOT / "benchmarks" / "algotune" / "looplab_eval.py"
    spec = importlib.util.spec_from_file_location("looplab_eval_bridge", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_registered_artefact_is_removed_and_the_keep_flag_holds_it(tmp_path, monkeypatch):
    mod = _bridge()
    leaked = tmp_path / "looplab-piptarget-xyz"
    leaked.mkdir()
    (leaked / "ext.so").write_bytes(b"compiled")
    mod._ARTEFACTS.append(leaked)
    monkeypatch.setenv("ALGOTUNE_KEEP_EVAL_ARTEFACTS", "1")
    mod._sweep_artefacts()
    assert leaked.exists(), "a disputed score needs the evidence the keep flag preserves"
    monkeypatch.delenv("ALGOTUNE_KEEP_EVAL_ARTEFACTS")
    mod._sweep_artefacts()
    assert not leaked.exists()


def test_the_piptarget_dir_is_registered_where_it_is_created():
    """Driven through the real source: the mkdtemp and the registration are one step.

    AST, not a substring (CLAUDE.md's ladder): the registration must be a real call, so a commented
    `_ARTEFACTS.append(...)` cannot satisfy it.
    """
    import ast
    src = (ROOT / "benchmarks" / "algotune" / "looplab_eval.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    registered = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "append" and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "_ARTEFACTS"
        and "_pip_target" in ast.dump(n)]
    assert registered, "the per-eval PIP_TARGET dir must join the artefact sweep"
