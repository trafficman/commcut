"""Tests for the shared tag form.

The form is now one widget shown by two windows, so these are about the *shared*
behaviour rather than the editor's: what the fields read back, what the dropdowns
offer, in what order, and what the required-field outline says.

`tests/test_editor_vocabulary.py` covers the same dropdown rules *through the
editor*, because that is the window a user meets them in. These exist so a change
to the form cannot pass by only being right for one of its two callers.

The editor's own suites -- locks, required tags, staging, export -- are the
regression guard for "the editor is unchanged by this existing".
"""

import os

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QComboBox, QLineEdit

from editor_stub import ensure_qapp
from shared.tag_form import (
    EMPTY_VOCABULARY_HINT,
    LOCK_BUTTONS,
    REQUIRED_TAG_LABELS,
    SUGGESTED_TAG_FIELDS,
    TAG_FIELDS,
    TagForm,
    field_change_signal,
    field_text,
    set_field_text,
)
from shared.vocabulary import get_vocabulary, forget_cached_vocabulary


@pytest.fixture(scope="module")
def qapp():
    return ensure_qapp()


@pytest.fixture
def form(qapp, tmp_path):
    """A form bound to a private vocabulary file, so a suite run cannot write to
    the real install root and no two forms share a vocabulary."""
    path = str(tmp_path / "vocabulary.json")
    widget = TagForm()
    widget.init_vocabulary(path)
    yield widget
    widget.deleteLater()
    forget_cached_vocabulary(path)


# ---------------------------------------------------------------------------
# The fields
# ---------------------------------------------------------------------------

def test_every_tag_has_exactly_one_field(form):
    assert set(form.read_tags()) == set(TAG_FIELDS)


def test_the_fields_read_back_what_was_typed(form):
    for namespace, attr in TAG_FIELDS.items():
        set_field_text(form.field(namespace), "typed value")
        assert form.read_tags()[namespace] == "typed value", (
            f"{attr} did not read back what was written")


def test_the_two_widget_types_are_treated_as_one(form):
    """The point of `field_text`: a QLineEdit and a QComboBox disagree about
    their accessors, and one accessor means a widget type changing breaks one
    function rather than every call site that guessed wrong."""
    assert isinstance(form.field("title"), QLineEdit)
    assert isinstance(form.field("network"), QComboBox)
    assert field_text(form.field("title")) == form.read_tags()["title"]
    assert field_text(form.field("network")) == form.read_tags()["network"]


def test_write_tags_does_not_fire_the_change_signals(form, qapp):
    """A programmatic fill must not look like a person editing. The editor's
    dirty flag and its "edited segment" test both hang off these signals."""
    seen = []
    for namespace in TAG_FIELDS:
        field_change_signal(form.field(namespace)).connect(seen.append)

    form.write_tags({"title": "Programmed"})

    assert seen == []
    assert form.read_tags()["title"] == "Programmed", "but it did write"


def test_write_tags_overrides_win_over_the_supplied_tags(form):
    form.write_tags({"network": "From The Tags"},
                    overrides={"network": "From The Locks"})
    assert form.read_tags()["network"] == "From The Locks"


def test_a_typed_value_survives_a_dropdown_refresh(form):
    """The bug that matters most in a long session: `QComboBox.clear()` empties
    the line edit too, so a refresh can wipe what someone is mid-way through
    typing. Here it is a committed value on another field."""
    get_vocabulary(form.vocabulary_path).record({"network": "Committed"})
    form.write_tags({"network": "Committed", "block": "Half typed"})

    form.refresh_combos()

    assert form.read_tags()["network"] == "Committed"
    assert form.read_tags()["block"] == "Half typed"


# ---------------------------------------------------------------------------
# The dropdowns
# ---------------------------------------------------------------------------

def test_a_dropdown_offers_what_the_vocabulary_holds(form):
    """Recorded through the *cached* instance the form reads, which is what
    `record_use` and a second editor in the same process both go through."""
    get_vocabulary(form.vocabulary_path).record({"network": "Cartoon Network"})

    form.refresh_combos()

    assert "Cartoon Network" in form.ordered_tag_values("network")


def test_the_ordering_is_most_recently_used_first(form):
    form.note_recent_tags({"network": "Third"})
    form.note_recent_tags({"network": "First"})

    values = form.ordered_tag_values("network")

    assert values[:2] == ["First", "Third"]


def test_the_remainder_is_alphabetical_so_the_list_is_deterministic(form):
    get_vocabulary(form.vocabulary_path).record({
        "network": "Zebra", "block": "Apple"})
    form.refresh_combos()

    assert form.ordered_tag_values("network") == ["Zebra"]
    assert form.ordered_tag_values("block") == ["Apple"]


def test_note_recent_tags_says_whether_the_order_moved(form):
    assert form.note_recent_tags({"network": "New"}) is True
    assert form.note_recent_tags({"network": "New"}) is False, (
        "already at the front, so nothing changed")
    assert form.note_recent_tags({"network": "Other"}) is True


def test_a_blank_value_is_not_moved_to_the_front(form):
    assert form.note_recent_tags({"network": "   "}) is False
    assert form.ordered_tag_values("network") == []


def test_an_empty_dropdown_explains_itself(form):
    """A closed-looking empty combo reads as broken; the placeholder reads as an
    instruction. It is disabled so it cannot be chosen by clicking."""
    form.refresh_combos()

    combo = form.field("special")
    assert combo.count() == 1
    assert combo.itemText(0) == EMPTY_VOCABULARY_HINT
    assert combo.model().item(0).isEnabled() is False


def test_a_refresh_leaves_an_already_emptied_dropdown_alone(form):
    """Otherwise the placeholder would be re-added on every refresh and grow."""
    form.refresh_combos()
    before = form.field("special").count()

    form.refresh_combos()

    assert form.field("special").count() == before


def test_the_popup_closes_after_selecting_a_value(form, qapp):
    """Regression: selecting from the popup used to re-open it.

    The old code connected `completer.complete()` to
    `QComboBox.editTextChanged`, which fires on *both* typing and picking.
    After a selection the popup re-opened, obscuring the form and forcing a
    manual click-away.  The fix uses `QLineEdit.textEdited`, which fires only
    on typing, so the popup closes on its own after a pick.
    """
    get_vocabulary(form.vocabulary_path).record({"network": "Cartoon Network"})
    form.refresh_combos()

    combo = form.field("network")
    combo.show()
    qapp.processEvents()

    completer = combo.completer()
    completer.complete()
    qapp.processEvents()

    popup = completer.popup()
    assert popup.isVisible()

    QTest.keyClick(popup, Qt.Key_Down)
    qapp.processEvents()
    QTest.keyClick(popup, Qt.Key_Return)
    qapp.processEvents()

    assert not popup.isVisible()
    assert combo.currentText() == "Cartoon Network"


def test_the_popup_reopens_when_typing_after_a_selection(form, qapp):
    """A selection that closes the popup must not break the next open on
    typing."""
    get_vocabulary(form.vocabulary_path).record({"network": "Cartoon Network"})
    form.refresh_combos()

    combo = form.field("network")
    combo.show()
    qapp.processEvents()

    completer = combo.completer()
    completer.complete()
    qapp.processEvents()
    popup = completer.popup()
    assert popup.isVisible()

    QTest.keyClick(popup, Qt.Key_Down)
    qapp.processEvents()
    QTest.keyClick(popup, Qt.Key_Return)
    qapp.processEvents()
    assert not popup.isVisible()

    combo.lineEdit().clear()
    QTest.keyClicks(combo.lineEdit(), "C")
    qapp.processEvents()
    assert popup.isVisible()


def test_the_popup_prunes_to_matching_tags_as_you_type(form, qapp):
    """The dropdown narrows with what was typed instead of staying full and
    scrolling to the best match: it shrinks toward one matching tag, or none."""
    vocabulary = get_vocabulary(form.vocabulary_path)
    vocabulary.record({"network": "Toonami"})
    vocabulary.record({"network": "Toonami Kids"})
    vocabulary.record({"network": "Nickelodeon"})
    form.refresh_combos()

    combo = form.field("network")
    completer = combo.completer()
    combo.show()
    qapp.processEvents()

    def visible_rows():
        model = completer.completionModel()
        return [model.index(row, 0).data()
                for row in range(model.rowCount())]

    QTest.keyClicks(combo.lineEdit(), "toon")
    qapp.processEvents()
    assert set(visible_rows()) == {"Toonami", "Toonami Kids"}

    combo.lineEdit().clear()
    QTest.keyClicks(combo.lineEdit(), "kids")
    qapp.processEvents()
    assert visible_rows() == ["Toonami Kids"]

    combo.lineEdit().clear()
    QTest.keyClicks(combo.lineEdit(), "zzz")
    qapp.processEvents()
    assert visible_rows() == []


def test_enter_commits_the_matched_tag(form, qapp):
    """A pruned popup with a single match commits it on Enter: the list is a
    promise, and Enter keeps it."""
    get_vocabulary(form.vocabulary_path).record({"network": "Toonami"})
    get_vocabulary(form.vocabulary_path).record({"network": "Toonami Kids"})
    form.refresh_combos()

    combo = form.field("network")
    completer = combo.completer()
    combo.show()
    qapp.processEvents()

    QTest.keyClicks(combo.lineEdit(), "kids")
    qapp.processEvents()
    assert completer.popup().isVisible()

    QTest.keyClick(combo.lineEdit(), Qt.Key_Return)
    qapp.processEvents()

    assert combo.currentText() == "Toonami Kids"
    assert not completer.popup().isVisible()


def test_enter_with_no_match_keeps_what_was_typed(form, qapp):
    """When the popup is already gone there is nothing to commit, so a custom
    value is staged as-is."""
    get_vocabulary(form.vocabulary_path).record({"network": "Toonami"})
    form.refresh_combos()

    combo = form.field("network")
    combo.show()
    qapp.processEvents()

    QTest.keyClicks(combo.lineEdit(), "zzz")
    qapp.processEvents()
    assert not combo.completer().popup().isVisible()

    QTest.keyClick(combo.lineEdit(), Qt.Key_Return)
    qapp.processEvents()

    assert combo.currentText() == "zzz"


def test_enter_commits_the_first_match_when_several_remain(form, qapp):
    """With several matches left, Enter commits the first one the completer
    detects -- narrow further, or click, to take a different one."""
    get_vocabulary(form.vocabulary_path).record({"network": "Toonami"})
    get_vocabulary(form.vocabulary_path).record({"network": "Toonami Kids"})
    form.refresh_combos()

    combo = form.field("network")
    completer = combo.completer()
    combo.show()
    qapp.processEvents()

    QTest.keyClicks(combo.lineEdit(), "toon")
    qapp.processEvents()
    assert completer.completionModel().rowCount() == 2

    QTest.keyClick(combo.lineEdit(), Qt.Key_Return)
    qapp.processEvents()

    assert combo.currentText() == "Toonami"
    assert not completer.popup().isVisible()


# ---------------------------------------------------------------------------
# Required fields
# ---------------------------------------------------------------------------

def test_the_outline_names_the_required_tags_that_are_missing(form):
    form.write_tags({})

    assert set(form.missing_required_labels()) == set(
        REQUIRED_TAG_LABELS.values())


def test_filling_every_required_field_clears_the_list(form):
    form.write_tags({namespace: "x" for namespace in REQUIRED_TAG_LABELS})

    assert form.missing_required_labels() == []


def test_a_optional_field_is_never_outlined(form):
    form.write_tags({namespace: "x" for namespace in REQUIRED_TAG_LABELS})

    form.refresh_required_fields()

    assert form.field("block").styleSheet() == ""
    assert form.field("information").styleSheet() == ""


def test_the_outline_is_drawn_on_the_field_and_removed_when_filled(form):
    form.write_tags({})
    form.refresh_required_fields()
    assert "border" in form.field("title").styleSheet()

    form.write_tags({namespace: "x" for namespace in REQUIRED_TAG_LABELS})
    form.refresh_required_fields()

    assert form.field("title").styleSheet() == ""


def test_an_exempt_form_is_outlined_for_nothing(form):
    """The editor's skipped segments are excluded from export, so the backend's
    required-tag rule does not apply to them either, and outlining fields that
    nothing will ask for is worse than no outline."""
    form.write_tags({})

    form.refresh_required_fields(exempt=True)

    assert form.field("title").styleSheet() == ""


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------

def test_locks_can_be_hidden(form):
    """The Library Mesh Tag Editor shows the same form for independent clips: a
    lock carries a value to the next segment, and a queue has no next segment.
    The widgets stay so there is one form rather than two.

    `isHidden` rather than `isVisible`: the form is never shown in this test, and
    a child of an undisplayed parent reports `isVisible() == False` whatever its
    own flag says.
    """
    form.set_locks_visible(False)
    assert all(form.lock_button(key).isHidden() for key in LOCK_BUTTONS)

    form.set_locks_visible(True)
    assert all(not form.lock_button(key).isHidden() for key in LOCK_BUTTONS)


def test_title_has_no_lock(form):
    """A title is unique per clip, so there is nothing to carry forward."""
    assert "title" not in LOCK_BUTTONS
    assert "title" not in SUGGESTED_TAG_FIELDS


# ---------------------------------------------------------------------------
# The two callers
# ---------------------------------------------------------------------------

def test_two_forms_do_not_share_a_vocabulary(qapp, tmp_path):
    """The most-recently-used order is per form, so one session's habits do not
    sit at the top of the next one's lists."""
    first, second = TagForm(), TagForm()
    try:
        first.init_vocabulary(str(tmp_path / "one.json"))
        second.init_vocabulary(str(tmp_path / "two.json"))

        first.note_recent_tags({"network": "Only Mine"})

        assert "Only Mine" in first.ordered_tag_values("network")
        assert "Only Mine" not in second.ordered_tag_values("network")
    finally:
        first.deleteLater()
        second.deleteLater()


def test_a_form_produced_without_a_vocabulary_still_reads_and_writes(qapp):
    """The library uses this form's field accessors without ever asking it for
    tag values, and it must not have to know a vocabulary exists."""
    bare = TagForm()
    try:
        bare.write_tags({"title": "Fine"})

        assert bare.read_tags()["title"] == "Fine"
        assert bare.missing_required_labels(), (
            "the required rule is shared.exporting's and needs no vocabulary")
        assert bare.ordered_tag_values("network") == []
    finally:
        bare.deleteLater()


def test_the_form_is_promoted_into_both_windows_not_drawn_twice(qapp):
    """The shipped editor and the shipped queue each host this widget. Asserted
    against the `.ui` sources, which is what a merge would change -- a loaded
    object cannot distinguish a field declared on a window from one reached
    through the form it promotes."""
    from shared.environment import resource_path

    for package, ui in (("editor", "editorwindow.ui"),
                        ("importer", "queuewindow.ui")):
        path = resource_path(package, ui)
        if not os.path.exists(path):
            continue
        source = open(path, encoding="utf-8").read()
        assert 'class="TagForm"' in source, f"{ui} does not host the shared form"
        for attr in set(TAG_FIELDS.values()) | set(LOCK_BUTTONS.values()):
            assert f'name="{attr}"' not in source, (
                f"{ui} declares {attr} itself, so it has a second tag form")