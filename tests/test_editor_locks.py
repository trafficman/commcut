"""Tests for the editor's tag-lock display, tracking, and carry-over rules.

The editor window itself needs libmpv and a real video, so these tests bind
the real ``MediaPlayer`` methods onto a lightweight stub that supplies only
the ``ui`` namespace and the segment model. That keeps the code under test the
shipped code rather than a re-implementation.
"""

import os

import pytest
from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton

from editor.editor import MediaPlayer, _LOCK_BUTTONS, _TAG_FIELDS
from shared.segments import SegmentModel


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class _EditorStub:
    """Minimal MediaPlayer surface covering the lock and segment code paths."""

    _is_segment_edited = MediaPlayer._is_segment_edited
    _write_tags_to_form = MediaPlayer._write_tags_to_form
    _refresh_lock_buttons = MediaPlayer._refresh_lock_buttons
    _read_tags_from_form = MediaPlayer._read_tags_from_form
    _update_stage_button = MediaPlayer._update_stage_button
    _inherited_tags = MediaPlayer._inherited_tags
    on_toggle_lock = MediaPlayer.on_toggle_lock
    on_tag_edited = MediaPlayer.on_tag_edited
    on_end_segment = MediaPlayer.on_end_segment
    on_start_segment = MediaPlayer.on_start_segment
    _move_active = MediaPlayer._move_active
    _snap_playhead_to_active_start = lambda self: None

    def __init__(self, segments, duration=120.0):
        self.ui = type("Ui", (), {})()
        for attr in _TAG_FIELDS.values():
            setattr(self.ui, attr, QLineEdit())
        for attr in _LOCK_BUTTONS.values():
            button = QPushButton()
            button.setCheckable(True)  # matches checkable=true in editorwindow.ui
            setattr(self.ui, attr, button)
        for attr in ("stageButton", "undoButton", "clipIgnore"):
            setattr(self.ui, attr, QPushButton())
        for key, attr in _LOCK_BUTTONS.items():
            getattr(self.ui, attr).toggled.connect(
                lambda checked, k=key: self.on_toggle_lock(k, checked))
        for key, attr in _TAG_FIELDS.items():
            getattr(self.ui, attr).textChanged.connect(
                lambda text, k=key: self.on_tag_edited(k, text))

        self.segment_model = SegmentModel("test.mp4", duration, segments)
        self.current_index = 0
        self.tag_locks = {}
        self.dirty = False
        self.player = type("Player", (), {"time_pos": 10.0})()
        self._refresh_timeline = self._refresh_form_and_locks

    def _refresh_form_and_locks(self):
        self._write_tags_to_form(
            self.segment_model.segments[self.current_index]["tags"])
        self._refresh_lock_buttons()

    # --- helpers ---

    def set_tag(self, key, value):
        getattr(self.ui, _TAG_FIELDS[key]).setText(value)

    def get_tag(self, key):
        return getattr(self.ui, _TAG_FIELDS[key]).text()

    def click_lock(self, key):
        getattr(self.ui, _LOCK_BUTTONS[key]).click()

    def checked_locks(self):
        return sorted(
            key for key, attr in _LOCK_BUTTONS.items()
            if getattr(self.ui, attr).isChecked()
        )

    def form(self):
        return {k: getattr(self.ui, v).text() for k, v in _TAG_FIELDS.items()
                if getattr(self.ui, v).text()}

    def go_to(self, index):
        self.current_index = index
        self._refresh_form_and_locks()


@pytest.fixture(scope="module")
def qapp():
    """One offscreen QApplication for the widget-backed lock tests."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def editor(qapp):
    """A stub editor with one empty segment."""
    return _EditorStub([{"start": 0.0, "ignored": False, "tags": {}}])


def _three_segment_editor(qapp):
    return _EditorStub([
        {"start": 0.0, "ignored": False, "tags": {}},
        {"start": 30.0, "ignored": False, "tags": {}},
        {"start": 60.0, "ignored": False, "tags": {}},
    ])


# ---------------------------------------------------------------------------
# Lock display: editing a tag must not clear the other locks
# ---------------------------------------------------------------------------

def test_lock_stays_engaged_while_typing(qapp):
    """A lock clicked after typing must not be immediately switched off."""
    editor = _EditorStub([{"start": 0.0, "ignored": False, "tags": {}}])
    editor.set_tag("network", "Cartoon Network")
    editor.set_tag("filler_type", "Promo")
    editor.set_tag("time_period", "Morning")

    editor.click_lock("network")
    editor.click_lock("filler_type")
    editor.click_lock("time_period")

    assert editor.checked_locks() == ["filler_type", "network", "time_period"]
    assert editor.tag_locks == {
        "network": "Cartoon Network",
        "filler_type": "Promo",
        "time_period": "Morning",
    }


def test_unlocking_one_lock_leaves_the_others_engaged(editor):
    """The reported symptom: unlocking one tag switched every lock off."""
    for key, value in (("network", "Cartoon Network"),
                       ("filler_type", "Promo"),
                       ("time_period", "Morning")):
        editor.set_tag(key, value)
        editor.click_lock(key)

    editor.click_lock("filler_type")  # unlock just this one

    assert editor.checked_locks() == ["network", "time_period"]
    assert editor.tag_locks == {
        "network": "Cartoon Network",
        "time_period": "Morning",
    }


def test_lock_button_survives_a_full_refresh_after_typing(editor):
    editor.set_tag("network", "Cartoon Network")
    editor.click_lock("network")
    editor._refresh_form_and_locks()

    assert editor.checked_locks() == ["network"]


# ---------------------------------------------------------------------------
# The toggle is driven live by the field text
# ---------------------------------------------------------------------------

def test_typing_a_divergent_value_disengages_that_lock(qapp):
    """The lock is a pinned value; typing away from it switches the toggle off."""
    editor = _three_segment_editor(qapp)
    editor.set_tag("network", "Cartoon Network")
    editor.set_tag("filler_type", "Promo")
    editor.click_lock("network")
    editor.click_lock("filler_type")
    assert editor.checked_locks() == ["filler_type", "network"]

    editor.set_tag("network", "Nickelodeon")

    assert editor.checked_locks() == ["filler_type"]


def test_typing_the_pinned_value_back_re_engages_the_lock(qapp):
    editor = _three_segment_editor(qapp)
    editor.set_tag("network", "Cartoon Network")
    editor.set_tag("filler_type", "Promo")
    editor.click_lock("network")
    editor.click_lock("filler_type")

    editor.set_tag("network", "Nickelodeon")
    editor.set_tag("network", "Cartoon Network")

    assert editor.checked_locks() == ["filler_type", "network"]


def test_divergent_typing_leaves_the_pinned_value_intact(qapp):
    """A segment deviating from the lock must not repoint what propagates."""
    editor = _three_segment_editor(qapp)
    editor.set_tag("network", "Cartoon Network")
    editor.click_lock("network")

    editor.set_tag("network", "Nickelodeon")

    assert editor.tag_locks == {"network": "Cartoon Network"}


def test_deviating_segment_does_not_poison_the_next_segment(qapp):
    """End Seg after deviating still carries the pinned value, not the deviation."""
    editor = _three_segment_editor(qapp)
    editor.set_tag("network", "Cartoon Network")
    editor.click_lock("network")

    editor.set_tag("network", "Nickelodeon")
    editor.on_end_segment()

    assert editor.segment_model.segments[1]["tags"] == {
        "network": "Cartoon Network",
    }


def test_clicking_a_disengaged_toggle_pins_the_new_value(qapp):
    editor = _three_segment_editor(qapp)
    editor.set_tag("network", "Cartoon Network")
    editor.click_lock("network")

    editor.set_tag("network", "Nickelodeon")
    editor.click_lock("network")  # re-pin to the deviation

    assert editor.tag_locks == {"network": "Nickelodeon"}
    assert editor.checked_locks() == ["network"]


def test_locking_an_empty_field_disengages_once_typed_into(qapp):
    """An empty pin cannot silently carry nothing; typing exposes it."""
    editor = _three_segment_editor(qapp)
    editor.click_lock("network")
    assert editor.checked_locks() == ["network"]

    editor.set_tag("network", "Cartoon Network")

    assert editor.tag_locks == {"network": ""}
    assert editor.checked_locks() == []
    assert editor._inherited_tags() == {}


# ---------------------------------------------------------------------------
# Staged segments reflect the held locks
# ---------------------------------------------------------------------------

def test_staged_segment_shows_lock_when_tags_match(qapp):
    """A previously staged segment should not read as unlocked by default."""
    editor = _EditorStub([
        {"start": 0.0, "ignored": False,
         "tags": {"title": "First", "network": "Cartoon Network"}},
        {"start": 30.0, "ignored": False, "tags": {}},
    ])
    editor.go_to(1)
    editor.set_tag("network", "Cartoon Network")
    editor.click_lock("network")

    editor.go_to(0)  # back to the staged segment

    assert editor.get_tag("network") == "Cartoon Network"
    assert editor.checked_locks() == ["network"]


def test_staged_segment_shows_unlocked_when_tag_differs(qapp):
    editor = _EditorStub([
        {"start": 0.0, "ignored": False,
         "tags": {"title": "First", "network": "Nickelodeon"}},
        {"start": 30.0, "ignored": False, "tags": {}},
    ])
    editor.go_to(1)
    editor.set_tag("network", "Cartoon Network")
    editor.click_lock("network")

    editor.go_to(0)  # staged earlier, with a different network value

    # The lock is still held for future segments, but this segment's stored
    # value deviates from it, so the button reads as unlocked here.
    assert editor.tag_locks == {"network": "Cartoon Network"}
    assert editor.get_tag("network") == "Nickelodeon"
    assert editor.checked_locks() == []


# ---------------------------------------------------------------------------
# Carry-over into newly created segments
# ---------------------------------------------------------------------------

def test_end_segment_carries_over_locked_tags_only(qapp):
    editor = _three_segment_editor(qapp)
    editor.set_tag("network", "Cartoon Network")
    editor.set_tag("filler_type", "Promo")
    editor.set_tag("title", "Segment One")
    editor.set_tag("year", "1998")
    editor.click_lock("network")
    editor.click_lock("filler_type")

    editor.on_end_segment()

    assert editor.segment_model.segments[1]["tags"] == {
        "network": "Cartoon Network",
        "filler_type": "Promo",
    }


def test_end_segment_carries_nothing_when_no_tags_are_locked(qapp):
    editor = _three_segment_editor(qapp)
    editor.set_tag("network", "Cartoon Network")
    editor.set_tag("title", "Segment One")

    editor.on_end_segment()

    assert editor.segment_model.segments[1]["tags"] == {}


def test_start_segment_carries_over_locked_tags_only(qapp):
    editor = _three_segment_editor(qapp)
    editor.set_tag("network", "Cartoon Network")
    editor.set_tag("title", "Segment One")
    editor.click_lock("network")

    editor.on_start_segment()  # activates the new right-hand segment

    assert editor.current_index == 1
    assert editor.segment_model.segments[0]["ignored"] is True
    assert editor.segment_model.segments[1]["tags"] == {
        "network": "Cartoon Network",
    }


def test_unedited_segment_is_prefilled_from_locks(qapp):
    editor = _three_segment_editor(qapp)
    editor.set_tag("network", "Cartoon Network")
    editor.click_lock("network")

    editor.go_to(2)

    assert editor.form() == {"network": "Cartoon Network"}
    assert editor.checked_locks() == ["network"]


def test_created_segment_and_unedited_segment_show_the_same_form(qapp):
    """A freshly created segment must match a never-edited one."""
    editor = _three_segment_editor(qapp)
    editor.set_tag("network", "Cartoon Network")
    editor.set_tag("title", "Segment One")
    editor.click_lock("network")

    editor.on_end_segment()
    editor.go_to(1)
    created = editor.form()
    created_locks = editor.checked_locks()

    editor.go_to(2)
    assert editor.form() == created == {"network": "Cartoon Network"}
    assert editor.checked_locks() == created_locks == ["network"]
