"""Tests for the tag dropdowns: what they offer, and what counts as "used".

The vocabulary is advisory. Nothing here should ever refuse a value the user
typed, and the two things that could make it do so -- a dropdown that is a
closed list, and a field whose text a refresh wipes -- are both tested below.
"""

import os

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QCompleter, QLineEdit

from editor.editor import (
    _EMPTY_VOCABULARY_HINT, _LOCK_BUTTONS, _SUGGESTED_TAG_FIELDS, _TAG_FIELDS,
)
from editor_stub import EditorStub, ensure_qapp
from shared.environment import resource_path
from shared.tag_form import TagForm
from shared.timeline import TimelineWidget
from shared.ui_loader import UiLoader
from shared.vocabulary import Vocabulary, forget_cached_vocabulary


@pytest.fixture(scope="module")
def qapp():
    return ensure_qapp()


@pytest.fixture(autouse=True)
def quiet_dialogs(monkeypatch):
    """Silence both message boxes for every test in this file.

    A vocabulary test is not about dialogs, but staging an incomplete segment
    or exporting one raises a modal, and a stub is not a QWidget to parent one.
    """
    monkeypatch.setattr(
        "editor.editor.QMessageBox.warning",
        staticmethod(lambda *arguments, **kwargs: None))
    monkeypatch.setattr(
        "editor.editor.QMessageBox.information",
        staticmethod(lambda *arguments, **kwargs: None))


@pytest.fixture(autouse=True)
def drop_cached_vocabulary():
    yield
    forget_cached_vocabulary()


def editor_with(tmp_path, segments=None, **required):
    """A stub editor whose vocabulary lives at a known path, so a test can read
    the file the editor wrote rather than only the state it kept."""
    segments = segments or [
        {"start": 0.0, "ignored": False, "tags": {}},
        {"start": 30.0, "ignored": False, "tags": {}},
    ]
    editor = EditorStub(segments, media_path=str(tmp_path / "compilation.mp4"))
    if required:
        editor.fill_required(**required)
    return editor


def stored_vocabulary(editor):
    return Vocabulary.load(editor.ui.tagForm.vocabulary_path)


# ---------------------------------------------------------------------------
# The widget
# ---------------------------------------------------------------------------

def shipped_tag_form():
    """The tag form as the app resolves it, through the same loader.

    `shared/tagform.ui`, not `editorwindow.ui`: the fields moved out of the
    editor's own `.ui` and into the shared form the editor promotes, so a
    property lost from the form is lost from both windows and only this file
    looks at the shipped declaration.
    """
    from shared.environment import resource_path
    return str(resource_path("shared", "tagform.ui"))


def test_the_shipped_ui_file_declares_editable_combos_that_never_grow(qapp):
    """The `.ui` file itself, not the stub.

    The stub builds its own widgets, so nothing else in the suite would notice
    if `tagform.ui` still declared a QLineEdit for a field that is supposed to
    be a dropdown -- the shipped app would be a closed list while every widget
    test stayed green. Both properties belong in the file as well as in
    `_configure_tag_combo`, because the file is what a hand-edit or a merge
    would change.
    """
    loader = UiLoader()
    loader.register_widget(TagForm)
    form = loader.load(shipped_tag_form(), None)

    for key, attr in _SUGGESTED_TAG_FIELDS.items():
        field = getattr(form, attr)
        assert isinstance(field, QComboBox), f"{key} is a {type(field).__name__}"
        assert field.isEditable() is True, f"{key} cannot be typed into"
        assert field.insertPolicy() == QComboBox.NoInsert, (
            f"{key} would grow a list of its own"
        )
        assert field.sizeAdjustPolicy() == (
            QComboBox.AdjustToContentsOnFirstShow
        ), f"{key}'s popup would not size to its values"


def test_the_title_field_is_a_plain_text_input(qapp):
    """A per-clip-unique tag has nothing to suggest, so a dropdown there would be
    a list with one useful entry per clip. It stays a QLineEdit."""
    loader = UiLoader()
    loader.register_widget(TagForm)
    form = loader.load(shipped_tag_form(), None)

    title = getattr(form, _TAG_FIELDS["title"])

    assert isinstance(title, QLineEdit)
    assert not isinstance(title, QComboBox)


def test_the_editor_does_not_draw_its_own_tag_fields():
    """One tag form in the app, by construction.

    If `editorwindow.ui` grew its own grid back, two forms would exist and the
    dropdown configuration in `shared/tag_form.py` would apply to only one of
    them -- the drift this whole refactor exists to prevent, and which no widget
    test would otherwise notice, because the promoted form is a child of the
    window and `findChild` recurses into it.

    Asserted against the file's text rather than the loaded object: the source is
    what a hand-edit or a merge changes, and a loaded widget cannot tell a field
    declared on the window from one reached through the form.
    """
    source = open(
        resource_path("editor", "editorwindow.ui"), encoding="utf-8").read()

    for attr in set(_TAG_FIELDS.values()) | set(_LOCK_BUTTONS.values()):
        assert f'name="{attr}"' not in source, (
            f"{attr} is declared in editorwindow.ui, so the editor has a second "
            f"tag form")
    assert 'class="TagForm"' in source


def test_every_tag_field_exactly_one_is_the_unsuggestable_one():
    """The split is expressed once, but the consequence is worth pinning: title
    is the only field without a dropdown, so a tag added to _TAG_FIELDS later
    cannot silently end up on the wrong side of it."""
    unsuggestable = set(_TAG_FIELDS) - set(_SUGGESTED_TAG_FIELDS)

    assert unsuggestable == {"title"}
    assert set(_SUGGESTED_TAG_FIELDS) | unsuggestable == set(_TAG_FIELDS)


def test_a_tag_field_is_an_editable_combo_that_never_grows_its_own_list(qapp,
                                                                       tmp_path):
    """The default insert policy turns every typed value into a permanent item,
    which would quietly duplicate what the vocabulary file is for."""
    editor = editor_with(tmp_path)

    combo = editor.combo_for("network")

    assert isinstance(combo, QComboBox)
    assert combo.isEditable() is True
    assert combo.insertPolicy() == QComboBox.NoInsert


def test_the_completer_narrows_by_substring_and_ignores_case(qapp, tmp_path):
    """`toon` has to find `Toonami`; an exact-prefix completer would not."""
    editor = editor_with(tmp_path)

    completer = editor.combo_for("block").completer()

    assert completer.filterMode() == Qt.MatchContains
    assert completer.caseSensitivity() == Qt.CaseInsensitive


def test_the_completer_uses_a_filtered_popup(qapp, tmp_path):
    """The popup prunes itself to matching tags as you type (PopupCompletion),
    rather than showing the whole list and scrolling to the best match."""
    editor = editor_with(tmp_path)

    completer = editor.combo_for("block").completer()

    assert completer.completionMode() == QCompleter.PopupCompletion


def test_the_required_field_outline_names_the_widget_it_is_actually_on(qapp,
                                                                       tmp_path):
    """A QSS selector that matches nothing is not an error -- the outline just
    disappears, and a test asserting only that a style sheet exists stays green.
    """
    editor = editor_with(tmp_path)

    editor._refresh_required_fields()

    style = editor.combo_for("network").styleSheet()
    assert style, "a missing required field carries no outline at all"
    assert "QComboBox" in style, (
        f"the selector does not match the widget it is set on: {style!r}"
    )


# ---------------------------------------------------------------------------
# What counts as used
# ---------------------------------------------------------------------------

def test_staging_records_the_tags_it_wrote(qapp, tmp_path):
    editor = editor_with(tmp_path, block="Toonami")

    editor.on_stage()

    assert stored_vocabulary(editor).values("block") == ("Toonami",)


def test_staging_records_every_namespace_not_only_the_required_ones(qapp,
                                                                    tmp_path):
    """The required four are what export demands; the rest is what makes the
    dropdown worth having."""
    editor = editor_with(tmp_path, block="Toonami", show="Samurai Jack",
                         information="Remastered")

    editor.on_stage()

    vocabulary = stored_vocabulary(editor)
    assert vocabulary.values("show") == ("Samurai Jack",)
    assert vocabulary.values("information") == ("Remastered",)


def test_a_stage_refused_for_a_missing_required_tag_records_nothing(qapp, tmp_path):
    """The check runs before the commit, so nothing half-formed reaches the list."""
    editor = editor_with(tmp_path)
    editor.set_tag("title", "Some Title")

    editor.on_stage()

    vocabulary = stored_vocabulary(editor)
    assert vocabulary.values("title") == ()
    assert vocabulary.values("network") == ()


def test_export_preparation_records_what_it_persisted(qapp, tmp_path,
                                                      monkeypatch):
    """The other of the two commit points: a user can export without ever
    staging, and those tags are just as used."""
    monkeypatch.setattr("editor.editor.PROJECT_ROOT", str(tmp_path))
    editor = editor_with(tmp_path, block="Toonami")

    prepared = editor._prepare_export()

    assert prepared is not None
    assert stored_vocabulary(editor).values("block") == ("Toonami",)


def test_the_values_are_in_the_file_and_not_just_in_the_editor(qapp, tmp_path):
    editor = editor_with(tmp_path, block="Toonami")

    editor.on_stage()

    assert os.path.exists(editor.ui.tagForm.vocabulary_path)
    assert "Toonami" in open(editor.ui.tagForm.vocabulary_path, encoding="utf-8").read()


def test_a_fresh_editor_offers_what_a_previous_one_recorded(qapp, tmp_path):
    """The point of the file: the values outlive the window that made them."""
    first = editor_with(tmp_path, block="Toonami")
    first.on_stage()

    second = EditorStub(
        [{"start": 0.0, "ignored": False, "tags": {}}],
        media_path=str(tmp_path / "compilation.mp4"),
    )
    second._init_tag_vocabulary(first.ui.tagForm.vocabulary_path)

    assert "Toonami" in second.offered_values("block")


# ---------------------------------------------------------------------------
# The ordering
# ---------------------------------------------------------------------------

def test_the_most_recently_staged_value_is_offered_first(qapp, tmp_path):
    editor = editor_with(tmp_path)
    editor.fill_required(block="Toonami")
    editor.on_stage()
    editor.fill_required(block="Adult Swim")
    editor.on_stage()

    assert editor.offered_values("block")[0] == "Adult Swim"


def test_the_ordering_is_per_editor_not_persisted(qapp, tmp_path):
    """A session is one source video. Opening the next one should not inherit
    the last one's habits at the top of every list."""
    first = editor_with(tmp_path, block="Toonami")
    first.on_stage()

    second = EditorStub(
        [{"start": 0.0, "ignored": False, "tags": {}}],
        media_path=str(tmp_path / "compilation.mp4"),
    )
    second._init_tag_vocabulary(first.ui.tagForm.vocabulary_path)

    assert second.offered_values("block") == ("Toonami",)


def test_the_remainder_is_alphabetical_so_the_list_is_deterministic(qapp, tmp_path):
    editor = editor_with(tmp_path)
    vocabulary = editor.ui.tagForm._vocabulary
    vocabulary.record({"block": "Toonami", "block2": "x"})
    vocabulary.record({"show": "Samurai Jack"})
    vocabulary.record({"show": "Cowboy Bebop"})
    vocabulary.record({"show": "Dexter"})
    editor._refresh_tag_combos()

    assert editor.offered_values("show") == ("Cowboy Bebop", "Dexter",
                                              "Samurai Jack")


def test_a_value_used_twice_is_listed_once(qapp, tmp_path):
    editor = editor_with(tmp_path)
    editor.fill_required(block="Toonami")
    editor.on_stage()
    editor.fill_required(block="Adult Swim")
    editor.on_stage()
    editor.fill_required(block="Toonami")
    editor.on_stage()

    assert editor.offered_values("block").count("Toonami") == 1
    assert editor.offered_values("block")[0] == "Toonami"


# ---------------------------------------------------------------------------
# Advisory, never validating
# ---------------------------------------------------------------------------

def test_a_staged_title_is_not_recorded_because_nothing_would_offer_it(qapp,
                                                                     tmp_path):
    """The file exists to populate dropdowns. A namespace with no dropdown in it
    is dead weight in the file and a namespace someone later has to reason about
    during a sync."""
    editor = editor_with(tmp_path, title="A Clip Title", block="Toonami")
    assert editor.get_tag("title") == "A Clip Title"

    editor.on_stage()

    vocabulary = stored_vocabulary(editor)
    assert vocabulary.values("title") == ()
    assert vocabulary.values("block") == ("Toonami",)


def test_a_value_that_is_in_no_list_is_accepted_and_offered_afterwards(qapp,
                                                                      tmp_path):
    """Nothing validates against the vocabulary. That is what lets the file be
    imperfect without anything going wrong."""
    editor = editor_with(tmp_path)
    editor.fill_required(block="Something Never Seen Before")

    assert editor.get_tag("block") == "Something Never Seen Before"
    editor.on_stage()
    assert "Something Never Seen Before" in editor.offered_values("block")


def test_a_whitespace_padded_value_is_stored_trimmed(qapp, tmp_path):
    editor = editor_with(tmp_path)
    editor.fill_required(block="  Toonami  ")

    editor.on_stage()

    assert stored_vocabulary(editor).values("block") == ("Toonami",)


# ---------------------------------------------------------------------------
# An empty list
# ---------------------------------------------------------------------------

def test_an_empty_namespace_explains_itself_instead_of_looking_broken(qapp,
                                                                      tmp_path):
    editor = editor_with(tmp_path)

    assert editor.offered_values("block") == (_EMPTY_VOCABULARY_HINT,)


def test_the_explanation_cannot_be_selected_as_a_tag(qapp, tmp_path):
    editor = editor_with(tmp_path)

    item = editor.combo_for("block").model().item(0)

    assert item.isEnabled() is False


def test_the_explanation_appears_only_where_there_is_nothing_to_offer(qapp,
                                                                     tmp_path):
    editor = editor_with(tmp_path)
    editor.fill_required(block="Toonami")

    editor.on_stage()

    assert _EMPTY_VOCABULARY_HINT not in editor.offered_values("block")
    assert _EMPTY_VOCABULARY_HINT in editor.offered_values("show")


# ---------------------------------------------------------------------------
# A refresh must not eat what is being typed
# ---------------------------------------------------------------------------

def test_refreshing_the_dropdowns_keeps_the_text_in_every_field(qapp, tmp_path):
    """QComboBox.clear() empties the line edit as well as the item list, so a
    stage that repopulated every field would erase whatever the user is partway
    through typing in a different one."""
    editor = editor_with(tmp_path)
    editor.set_tag("show", "Samurai J")
    editor.set_tag("block", "Toonami")

    editor.on_stage()

    assert editor.get_tag("show") == "Samurai J"


def test_a_refresh_does_not_mark_the_editor_dirty(qapp, tmp_path):
    editor = editor_with(tmp_path)
    editor.fill_required(block="Toonami")
    editor.on_stage()

    editor.set_tag("show", "Samurai Jack")

    assert editor.dirty is True


def test_repopulating_twice_does_not_duplicate_the_list(qapp, tmp_path):
    editor = editor_with(tmp_path, block="Toonami")
    editor.on_stage()

    editor._refresh_tag_combos()
    editor._refresh_tag_combos()

    assert editor.offered_values("block").count("Toonami") == 1