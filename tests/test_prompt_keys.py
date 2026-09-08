"""PROMPT_KEYS registry enforcement (docs/15 §P4.7).

A prompt override lands as `<prompt_dir>/<key>.md`, so a typo'd key at a `render()` call site —
or a renamed key with a stale override file — silently falls back to the built-in default: the
operator's tuned prompt just stops applying, with no error anywhere. Same registry+source-scan
discipline as event types / hints / signals / task hooks.
"""
from __future__ import annotations

import re
from pathlib import Path

from _source_scan import scan
from looplab.core.prompts import PROMPT_KEYS

# \s* after the paren crosses newlines: best_of_n spells `render(\n    prompts, "key", …)` —
# the original same-line-only pattern was blind to it (the P4 review's own HIGH finding).
_CALL = re.compile(r'render\(\s*[\w.]+\s*,\s*"([a-z_]+)"')


def test_every_render_key_is_registered():
    unknown = {k: fs for k, fs in scan(_CALL).items() if k not in PROMPT_KEYS}
    assert not unknown, (
        f"render() call site(s) use unregistered prompt key(s) {unknown} — register in "
        "core/prompts.py::PROMPT_KEYS (and document the override file name) or fix the typo.")


def test_every_registered_key_has_a_call_site():
    calls = set(scan(_CALL))
    orphaned = [k for k in PROMPT_KEYS if k not in calls]
    assert not orphaned, (
        f"registered prompt key(s) {orphaned} have no render() call site — a rename left the "
        "registry (and any operator override files named after the old key) behind.")


def test_registered_keys_are_valid_override_filenames():
    for k in PROMPT_KEYS:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", k), f"{k!r}: keys become <key>.md filenames"


def test_prompt_store_override_roundtrip(tmp_path):
    # End-to-end: an override file named after a registered key actually replaces the default.
    from looplab.core.prompts import PromptStore, render
    (tmp_path / "developer_system.md").write_text("OVERRIDDEN $x", encoding="utf-8")
    store = PromptStore(str(tmp_path))
    assert render(store, "developer_system", "default", x="1") == "OVERRIDDEN 1"
    assert render(store, "researcher_system", "default") == "default"   # no file -> default
    assert render(None, "developer_system", "default") == "default"     # no store -> default


def test_prompt_vars_named_name_or_default_do_not_collide(tmp_path):
    # A template may use $name/$default as substitution vars; get()/render() take name/default as
    # positional-only params, so those vars pass cleanly through **vars instead of raising TypeError.
    from looplab.core.prompts import PromptStore, render
    (tmp_path / "researcher_system.md").write_text("Hi $name ($default)", encoding="utf-8")
    store = PromptStore(str(tmp_path))
    assert render(store, "researcher_system", "unused", name="Ada", default="D") == "Hi Ada (D)"
    assert store.get("researcher_system", "unused", name="Ada", default="D") == "Hi Ada (D)"
    assert render(None, "k", "Hi $name", name="Bob") == "Hi Bob"


def test_an_unreadable_override_falls_back_to_the_default_instead_of_crashing_the_role():
    """The override is re-read on EVERY call so edits hot-reload — which invites live editing, so the
    file can vanish between an exists() check and the open, and the read itself can fail
    (permissions, a transient FUSE error). Either used to propagate out of `get` and take down the
    calling role, where the documented behaviour for a missing override is the built-in default."""
    import tempfile

    from looplab.core.prompts import PromptStore

    with tempfile.TemporaryDirectory() as d:
        store = PromptStore(d)
        assert store.get("researcher_system", "BUILT-IN") == "BUILT-IN"     # no override file at all

        path = Path(d) / "researcher_system.md"
        path.write_text("OVERRIDE", encoding="utf-8")
        assert store.get("researcher_system", "BUILT-IN") == "OVERRIDE"

        path.unlink()                                   # deleted between calls, as hot reload invites
        assert store.get("researcher_system", "BUILT-IN") == "BUILT-IN"

        Path(d, "researcher_system.md").mkdir()         # a DIRECTORY where a file is expected
        assert store.get("researcher_system", "BUILT-IN") == "BUILT-IN"


# --------------------------------------------------------------- the TYPED registry (doc 27)


def test_prompt_keys_is_derived_from_the_registry_and_not_a_second_list():
    """`PROMPT_KEYS` was the hand-kept list; it is now the projection of `PROMPT_REGISTRY`.

    Two lists is the drift this repo has measured everywhere else (docs/BACKLOG.md §0.8), and here
    it would be invisible: a key in one and not the other silently changes which override files the
    two-way scan above admits.
    """
    from looplab.core.prompts import PROMPT_KEYS, PROMPT_REGISTRY

    assert PROMPT_KEYS == tuple(d.key for d in PROMPT_REGISTRY)
    assert len(set(PROMPT_KEYS)) == len(PROMPT_KEYS), "a key registered twice"


def test_every_registered_prompt_declares_a_family_and_what_it_governs():
    from looplab.core.prompts import PROMPT_FAMILIES, PROMPT_REGISTRY, definition

    for d in PROMPT_REGISTRY:
        assert d.family in PROMPT_FAMILIES, d.key
        assert re.fullmatch(r"[a-z][a-z_]*", d.family), f"{d.key}: {d.family!r} is not a family name"
        assert d.what.strip() and len(d.what) <= 120, (
            f"{d.key}: `what` is the operator's one line about the JOB, not the wording")
        assert definition(d.key) is d
    assert definition("no_such_prompt_key") is None, "a reporter shows an unregistered key, not raises"


def test_a_prompt_definition_cannot_be_edited_after_the_fact():
    """Frozen: the registry is read by guards and by the operator surface, and a row a reader can
    rewrite is a registry that describes whatever the last reader wanted."""
    import dataclasses

    import pytest

    from looplab.core.prompts import PROMPT_REGISTRY

    with pytest.raises(dataclasses.FrozenInstanceError):
        PROMPT_REGISTRY[0].family = "somewhere-else"


def test_the_ungoverned_prompt_families_are_named_disjoint_and_real():
    """Doc 27 row 134's residue, made countable: Genesis, assistants, reports, monitors and stewards
    keep separate prompt families, and until this registry existed nothing in the tree said which
    those were or where their text lived.

    Disjointness is the property that matters — a family cannot be both governed by the store and
    listed here as outside it — and the cited module has to exist, or a migration that moved the
    text leaves a row pointing at nothing (the dead-citation rot `core/claimpin.py` measures).
    """
    from looplab.core.prompts import PROMPT_FAMILIES, UNGOVERNED_PROMPT_FAMILIES

    root = Path(__file__).resolve().parents[1] / "looplab"
    named = [name for name, _where in UNGOVERNED_PROMPT_FAMILIES]
    assert len(set(named)) == len(named), "a family listed twice"
    assert not set(named) & set(PROMPT_FAMILIES), (
        "a family cannot be both inside the store and outside it — a migration that landed must "
        "DELETE its row from UNGOVERNED_PROMPT_FAMILIES")
    for name, where in UNGOVERNED_PROMPT_FAMILIES:
        rel = where.split("::")[0].split(" ")[0]
        assert (root / rel).exists(), f"{name}: {rel} does not exist — re-point or delete the row"


# ------------------------------------------------------- the BUNDLE's identity + pin (doc 27)


def _store(tmp_path):
    from looplab.core.prompts import PromptStore

    return PromptStore(str(tmp_path))


def test_a_key_with_no_override_has_no_revision_and_one_with_an_override_does(tmp_path):
    """`NO_REVISION` is not a digest of "": "not overridden" (the built-in default, whose identity
    is the commit) and "overridden with an empty file" are different facts under a pin."""
    from looplab.core.prompts import NO_REVISION, REVISION_PREFIX

    store = _store(tmp_path)
    assert store.revision("researcher_system") == NO_REVISION

    (tmp_path / "researcher_system.md").write_text("BODY", encoding="utf-8")
    rev = store.revision("researcher_system")
    assert rev.startswith(REVISION_PREFIX) and rev != NO_REVISION

    (tmp_path / "researcher_system.md").write_text("", encoding="utf-8")
    empty = store.revision("researcher_system")
    assert empty not in (NO_REVISION, rev), "an empty override is a file, not the absence of one"


def test_the_revision_covers_the_body_a_role_is_handed_and_not_the_frontmatter(tmp_path):
    """The body is what the model sees; frontmatter is metadata it never sees. An edit there is not
    a change of treatment and must not read as one — or every `tier:` tweak would report as a
    prompt the run did not pin."""
    store = _store(tmp_path)
    path = tmp_path / "developer_system.md"

    path.write_text("---\ntier: global\n---\nBODY\n", encoding="utf-8")
    with_front = store.revision("developer_system")
    path.write_text("---\ntier: domain\nowner: ada\n---\nBODY\n", encoding="utf-8")
    assert store.revision("developer_system") == with_front

    path.write_text("---\ntier: global\n---\nBODY CHANGED\n", encoding="utf-8")
    assert store.revision("developer_system") != with_front


def test_the_bundle_revision_is_keyed_so_a_body_moving_between_keys_is_a_new_bundle(tmp_path):
    """Over the {key: revision} MAP, never over a concatenation: a concatenation lets content move
    between two keys without changing the hash, which is the forgery a keyed digest refuses."""
    store = _store(tmp_path)
    empty = store.bundle_revision()

    (tmp_path / "researcher_system.md").write_text("SHARED BODY", encoding="utf-8")
    a = store.bundle_revision()
    assert a != empty

    (tmp_path / "researcher_system.md").unlink()
    (tmp_path / "developer_system.md").write_text("SHARED BODY", encoding="utf-8")
    b = store.bundle_revision()
    assert b not in (a, empty), "the same body under a different key is a different bundle"


def test_a_bundle_revision_over_a_narrowed_key_set_ignores_the_rest(tmp_path):
    store = _store(tmp_path)
    (tmp_path / "developer_system.md").write_text("BODY", encoding="utf-8")
    assert store.bundle_revision(("researcher_system",)) == _store(tmp_path).bundle_revision(
        ("researcher_system",))
    assert store.bundle_revision(("developer_system",)) != store.bundle_revision(
        ("researcher_system",))


def test_an_unpinned_store_records_nothing_and_serves_exactly_what_it_always_did(tmp_path):
    """A prompt is a contract: an unpinned store is every store constructed today, and it must not
    have grown a behaviour."""
    store = _store(tmp_path)
    (tmp_path / "researcher_system.md").write_text("OVERRIDE $x", encoding="utf-8")
    assert store.get("researcher_system", "BUILT-IN", x="1") == "OVERRIDE 1"
    (tmp_path / "researcher_system.md").write_text("EDITED", encoding="utf-8")
    assert store.get("researcher_system", "BUILT-IN") == "EDITED", "hot reload, unchanged"
    assert store.pinned is None
    assert store.divergences() == {}


def test_a_pinned_bundle_reports_a_mid_run_edit_and_still_serves_the_live_text(tmp_path, caplog):
    """THE DEFECT (doc 27 `prompt-bundle-unpinned-across-hot-reload`): an operator edits
    `researcher_system.md` at node 7, and nodes 0-6 and 8-N get different treatment of identical
    inputs with nothing anywhere able to tell the two halves apart.

    Driven end to end, and BOTH halves asserted: the record appears (the fix) AND the live text is
    still served (hot reload is the store's purpose and doc 27 keeps it — a lock here would change
    shipped behaviour for every operator who tunes a prompt live).
    """
    import logging

    store = _store(tmp_path)
    path = tmp_path / "researcher_system.md"
    path.write_text("PINNED BODY", encoding="utf-8")

    manifest = store.pin()
    assert manifest["researcher_system"] == store.revision("researcher_system")
    assert store.get("researcher_system", "BUILT-IN") == "PINNED BODY"
    assert store.divergences() == {}, "nothing moved yet"

    path.write_text("EDITED MID-RUN", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="looplab.core.prompts"):
        assert store.get("researcher_system", "BUILT-IN") == "EDITED MID-RUN", (
            "hot reload must survive the pin — the pin is a record, not a refusal")
    moved = store.divergences()
    assert set(moved) == {"researcher_system"}
    pinned_rev, live_rev = moved["researcher_system"]
    assert pinned_rev == manifest["researcher_system"] != live_rev
    assert live_rev == store.revision("researcher_system")
    assert sum("researcher_system" in r.getMessage() for r in caplog.records) == 1


def test_one_warning_per_revision_not_per_render_and_an_edit_back_ends_the_divergence(tmp_path,
                                                                                     caplog):
    """The store is re-read on EVERY render, so a per-call rule prints one line per paid call; a
    per-key-once rule reports the first edit and hides every later one. Keyed on the live revision,
    both are right — and an operator who edits the file back to the pinned bytes has ENDED the
    divergence rather than caused a third."""
    import logging

    store = _store(tmp_path)
    path = tmp_path / "developer_system.md"
    path.write_text("PINNED", encoding="utf-8")
    store.pin()

    with caplog.at_level(logging.WARNING, logger="looplab.core.prompts"):
        path.write_text("FIRST EDIT", encoding="utf-8")
        for _ in range(5):
            store.get("developer_system", "BUILT-IN")
        assert len(caplog.records) == 1, "one line per revision, not one per render"

        path.write_text("SECOND EDIT", encoding="utf-8")
        store.get("developer_system", "BUILT-IN")
        assert len(caplog.records) == 2, "a second edit is a second fact"

        path.write_text("PINNED", encoding="utf-8")
        store.get("developer_system", "BUILT-IN")
        assert store.divergences() == {}, "back to the pinned bytes is no longer a divergence"
        assert len(caplog.records) == 2


def test_a_key_outside_the_pinned_bundle_is_not_a_divergence(tmp_path, caplog):
    """A pin is over a bundle; a render of something the caller did not pin has no baseline to have
    moved from, and reporting one would make the pin noisy exactly where it knows nothing."""
    import logging

    store = _store(tmp_path)
    store.pin(("developer_system",))
    (tmp_path / "researcher_system.md").write_text("APPEARED", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="looplab.core.prompts"):
        assert store.get("researcher_system", "BUILT-IN") == "APPEARED"
    assert store.divergences() == {} and not caplog.records


def test_an_override_appearing_or_vanishing_under_a_pin_is_a_divergence(tmp_path):
    """The two cases a "digest the files that exist" pin would miss entirely."""
    from looplab.core.prompts import NO_REVISION

    store = _store(tmp_path)
    manifest = store.pin()
    assert manifest["researcher_system"] == NO_REVISION

    (tmp_path / "researcher_system.md").write_text("APPEARED", encoding="utf-8")
    store.get("researcher_system", "BUILT-IN")
    assert store.divergences()["researcher_system"][0] == NO_REVISION

    path = tmp_path / "developer_system.md"
    path.write_text("HERE AT PIN TIME", encoding="utf-8")
    store.pin()
    path.unlink()
    assert store.get("developer_system", "BUILT-IN") == "BUILT-IN"
    assert store.divergences()["developer_system"][1] == NO_REVISION


def test_re_pinning_opens_a_new_phase_and_the_reports_are_copies(tmp_path):
    """Re-pin is how a caller opens the next phase: the manifest is replaced and what was already
    reported is cleared, so the next divergence is measured against the NEW baseline. Both readers
    hand back copies — a caller that could edit the baseline would move it under the comparison,
    and one that could edit the report could erase that a phase ran on unpinned text."""
    store = _store(tmp_path)
    path = tmp_path / "researcher_system.md"
    path.write_text("A", encoding="utf-8")
    store.pin()
    path.write_text("B", encoding="utf-8")
    store.get("researcher_system", "BUILT-IN")
    assert store.divergences()

    store.pin()
    assert store.divergences() == {}
    store.get("researcher_system", "BUILT-IN")
    assert store.divergences() == {}, "B is the new baseline"

    store.pinned["researcher_system"] = "tampered"
    store.divergences()["researcher_system"] = ("x", "y")
    assert store.pinned["researcher_system"] != "tampered"
    assert store.divergences() == {}


def test_an_unreadable_override_under_a_pin_reads_as_the_default_not_as_a_crash(tmp_path):
    """The `_read` fallback is shared by `get` and `revision` on purpose — a second reader of "what
    would this key resolve to" is a second answer, and the pin would then be taken over bytes no
    role was ever handed."""
    from looplab.core.prompts import NO_REVISION

    store = _store(tmp_path)
    (tmp_path / "researcher_system.md").mkdir()          # a DIRECTORY where a file is expected
    assert store.get("researcher_system", "BUILT-IN") == "BUILT-IN"
    assert store.revision("researcher_system") == NO_REVISION
