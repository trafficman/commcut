"""Tests for the editor's tag-lock display, tracking, and carry-over rules.

Driven through the shared widget-backed `editor_stub.EditorStub`, so the
behavior under test is the shipped `MediaPlayer` behavior.
"""

import pytest

from editor_stub import EditorStub, ensure_qapp


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    return ensure_qapp()


@pytest.fixture
def editor(qapp):
    """A stub editor with one empty segment."""
    return EditorStub([{"start": 0.0, "ignored": False, "tags": {}}])


def _three_segment_editor(qapp):
    return EditorStub([
        {"start": 0.0, "ignored": False, "tags": {}},
        {"start": 30.0, "ignored": False, "tags": {}},
        {"start": 60.0, "ignored": False, "tags": {}},
    ])


# ---------------------------------------------------------------------------
# Lock display: editing a tag must not clear the other locks
# ---------------------------------------------------------------------------

def test_lock_stays_engaged_while_typing(qapp):
    """A lock clicked after typing must not be immediately switched off."""
    editor = EditorStub([{"start": 0.0, "ignored": False, "tags": {}}])
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
    editor = EditorStub([
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
    editor = EditorStub([
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
