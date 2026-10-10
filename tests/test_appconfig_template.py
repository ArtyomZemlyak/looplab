from looplab.core.appconfig import load_document, render_template


def test_generated_template_uses_a_working_backend_for_each_task_kind(tmp_path):
    for kind in ("dataset", "repo", "code_regression", "mlebench", "mlebench_real"):
        path = tmp_path / f"{kind}.yaml"
        path.write_text(render_template(kind), encoding="utf-8")
        _task, settings, _out = load_document(path)
        # doc 75 UX-02: the DEFAULT backend is written commented — an active `backend: llm` outranked
        # `LOOPLAB_BACKEND=toy` and said nothing the default does not. The run still uses llm.
        from looplab.core.config import Settings
        assert "backend" not in settings and Settings(**settings).backend == "llm"
        assert "  # backend: llm" in path.read_text(encoding="utf-8")

    path = tmp_path / "quadratic.yaml"
    path.write_text(render_template("quadratic"), encoding="utf-8")
    _task, settings, _out = load_document(path)
    assert settings["backend"] == "toy"
