"""Tests for placing a segment's end boundary at the playhead.

A segment's end and the next segment's start are the same stored value, so
"End Seg" has two possible outcomes: inserting a boundary (playhead inside the
active segment) or moving the existing one (playhead past the end but still
within the following segment). Exactly one transition point is ever affected,
and a position beyond the following segment is refused rather than clamped.
"""

import pytest

from editor_stub import EditorStub, ensure_qapp
from shared.exporting import validate_segment_model
from shared.segments import (
    END_BOUNDARY_BLOCKED,
    END_BOUNDARY_INSERTED,
    END_BOUNDARY_MOVED,
    END_BOUNDARY_NO_CHANGE,
    SegmentModel,
)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    return ensure_qapp()


@pytest.fixture
def warnings(monkeypatch):
    """Capture QMessageBox.warning instead of showing a modal dialog."""
    calls = []

    def record(parent, title, text, *args, **kwargs):
        calls.append((title, text))

    monkeypatch.setattr("editor.editor.QMessageBox.warning", staticmethod(record))
    return calls


def _model(count=3, duration=90.0, **segment):
    """A model of `count` equal 30s segments starting at segment 0."""
    return SegmentModel("test.mp4", duration, [
        {"start": 30.0 * i, "ignored": False, "tags": {}}
        for i in range(count)
    ])


# ---------------------------------------------------------------------------
# Inserting (the original behavior)
# ---------------------------------------------------------------------------

def test_playhead_inside_inserts_a_boundary():
    model = _model()
    assert model.place_end_boundary(0, 15.0) == END_BOUNDARY_INSERTED
    assert [s["start"] for s in model.segments] == [0.0, 15.0, 30.0, 60.0]


def test_insert_still_carries_the_supplied_tags():
    model = _model()
    model.place_end_boundary(0, 15.0, {"network": "Cartoon Network"})
    assert model.segments[1]["tags"] == {"network": "Cartoon Network"}


# ---------------------------------------------------------------------------
# Moving forward
# ---------------------------------------------------------------------------

def test_playhead_in_the_next_segment_moves_the_boundary_forward():
    model = _model()
    assert model.place_end_boundary(0, 45.0) == END_BOUNDARY_MOVED
    assert [s["start"] for s in model.segments] == [0.0, 45.0, 60.0]


def test_move_forward_keeps_the_segment_count():
    model = _model()
    model.place_end_boundary(0, 45.0)
    assert model.segment_count() == 3


def test_move_forward_resizes_both_neighbours():
    model = _model()
    model.place_end_boundary(0, 45.0)
    assert model.end(0) == 45.0
    assert model.start(1) == 45.0
    assert model.end(0) - model.start(0) == 45.0
    assert model.end(1) - model.start(1) == 15.0


def test_move_forward_keeps_the_timing_invariant():
    model = _model()
    model.place_end_boundary(0, 45.0)
    validate_segment_model(model)  # raises if starts are not strictly increasing


def test_move_forward_does_not_touch_neighbour_metadata():
    model = _model()
    model.segments[1]["tags"] = {"title": "Already Staged"}
    model.segments[1]["ignored"] = True

    model.place_end_boundary(0, 45.0)

    assert model.segments[1]["tags"] == {"title": "Already Staged"}
    assert model.segments[1]["ignored"] is True


def test_move_forward_works_on_a_neighbour_that_is_ignored():
    model = _model()
    model.segments[1]["ignored"] = True
    assert model.place_end_boundary(0, 45.0) == END_BOUNDARY_MOVED
    assert model.segments[1]["ignored"] is True


def test_move_forward_on_the_second_to_last_segment_stays_under_duration():
    model = _model(count=2, duration=90.0)
    assert model.place_end_boundary(0, 75.0) == END_BOUNDARY_MOVED
    assert model.segments[1]["start"] == 75.0
    validate_segment_model(model)


def test_move_forward_moves_the_active_index_boundary_not_a_later_one():
    model = _model()
    model.place_end_boundary(1, 75.0)
    assert [s["start"] for s in model.segments] == [0.0, 30.0, 75.0]


# ---------------------------------------------------------------------------
# Refusing to cross a second boundary
# ---------------------------------------------------------------------------

def test_playhead_beyond_the_next_segment_is_blocked():
    model = _model()
    assert model.place_end_boundary(0, 75.0) == END_BOUNDARY_BLOCKED
    assert [s["start"] for s in model.segments] == [0.0, 30.0, 60.0]


def test_blocked_leaves_the_model_untouched():
    model = _model()
    before = [dict(s) for s in model.segments]
    model.place_end_boundary(0, 75.0)
    assert model.segments == before


def test_playhead_exactly_at_the_next_segments_end_is_blocked():
    """It would leave that segment with no duration at all."""
    model = _model()
    assert model.place_end_boundary(0, 60.0) == END_BOUNDARY_BLOCKED
    assert model.segments[1]["start"] == 30.0


def test_playhead_at_the_video_end_is_blocked_when_a_next_segment_exists():
    model = _model(count=3, duration=90.0)
    assert model.place_end_boundary(0, 90.0) == END_BOUNDARY_BLOCKED


# ---------------------------------------------------------------------------
# Nothing to do
# ---------------------------------------------------------------------------

def test_playhead_exactly_at_the_current_end_changes_nothing():
    model = _model()
    assert model.place_end_boundary(0, 30.0) == END_BOUNDARY_NO_CHANGE
    assert model.segment_count() == 3


def test_playhead_exactly_at_the_current_start_changes_nothing():
    model = _model()
    assert model.place_end_boundary(1, 30.0) == END_BOUNDARY_NO_CHANGE
    assert model.segment_count() == 3


def test_last_segment_cannot_move_its_end_forward():
    model = _model(count=2, duration=90.0)
    assert model.place_end_boundary(1, 90.0) == END_BOUNDARY_NO_CHANGE
    assert model.segments[1]["start"] == 30.0


def test_out_of_range_index_changes_nothing():
    model = _model()
    assert model.place_end_boundary(9, 45.0) == END_BOUNDARY_NO_CHANGE
    assert model.place_end_boundary(-1, 45.0) == END_BOUNDARY_NO_CHANGE


def test_playhead_before_the_active_start_is_unchanged_for_now():
    """Only the forward direction is handled; the back is left alone."""
    model = _model()
    assert model.place_end_boundary(1, 15.0) == END_BOUNDARY_NO_CHANGE
    assert model.segments[1]["start"] == 30.0


def test_first_segment_cannot_move_before_the_video_start():
    model = _model()
    assert model.place_end_boundary(0, 0.0) == END_BOUNDARY_NO_CHANGE
    assert model.segments[0]["start"] == 0.0


# ---------------------------------------------------------------------------
# Through the editor's End Seg button
# ---------------------------------------------------------------------------

def _editor(qapp):
    return EditorStub([
        {"start": 0.0, "ignored": False, "tags": {}},
        {"start": 30.0, "ignored": False, "tags": {}},
        {"start": 60.0, "ignored": False, "tags": {}},
    ], duration=90.0)


def test_end_seg_moves_the_boundary_and_marks_dirty(qapp):
    editor = _editor(qapp)
    editor.set_playhead(45.0)

    editor.on_end_segment()

    assert [s["start"] for s in editor.segment_model.segments] == [0.0, 45.0, 60.0]
    assert editor.dirty is True


def test_end_seg_still_inserts_inside_the_segment(qapp):
    editor = _editor(qapp)
    editor.set_playhead(15.0)

    editor.on_end_segment()

    assert editor.segment_model.segment_count() == 4
    assert editor.dirty is True


def test_end_seg_tells_the_user_when_it_refuses(qapp, warnings):
    editor = _editor(qapp)
    editor.set_playhead(75.0)

    editor.on_end_segment()

    assert warnings
    assert "Add Next Seg" in warnings[0][1]
    assert editor.dirty is False
    assert [s["start"] for s in editor.segment_model.segments] == [0.0, 30.0, 60.0]


def test_end_seg_is_silent_when_there_is_nothing_to_do(qapp, warnings):
    editor = _editor(qapp)
    editor.set_playhead(30.0)  # already on the boundary

    editor.on_end_segment()

    assert not warnings
    assert editor.dirty is False


def test_end_seg_ignores_a_missing_playhead(qapp, warnings):
    editor = _editor(qapp)
    editor.set_playhead(None)

    editor.on_end_segment()

    assert not warnings
    assert editor.dirty is False
    assert [s["start"] for s in editor.segment_model.segments] == [0.0, 30.0, 60.0]


def test_moved_boundary_keeps_the_active_segment_selected(qapp):
    editor = _editor(qapp)
    editor.set_playhead(45.0)

    editor.on_end_segment()

    assert editor.current_index == 0
    assert editor.get_tag("title") == editor.segment_model.segments[0]["tags"].get(
        "title", ""
    )
