"""The boundary peek: briefly show the clip, then rest on the boundary.

A segment boundary is a transition point and therefore usually a black frame,
so snapping the playhead there and stopping is correct but useless to look at.
`shared.mpv.BoundaryPreview` seeks a few frames past the boundary for a short
dwell and then seeks back, and the editor routes every active-segment change
through it. These tests pin both halves: the seek sequence itself, and the
cancellation rules that stop a pending return from yanking the playhead out
from under whatever the user does next.

Driven through `editor_stub.EditorStub`, so the code under test is the shipped
`MediaPlayer` code plus the real `BoundaryPreview`.
"""

import pytest

from editor_stub import EditorStub, FakeBridge, ensure_qapp
from shared.mpv import (
    BoundaryPreview, SEGMENT_PREVIEW_DWELL_MS, SEGMENT_PREVIEW_FRAMES,
)

FPS = 23.976
DURATION = 120.0
BOUNDARIES = [0.0, 30.0, 70.0]
OFFSET = SEGMENT_PREVIEW_FRAMES / FPS


@pytest.fixture(scope="module")
def qapp():
    return ensure_qapp()


@pytest.fixture
def bridge():
    return FakeBridge(duration=DURATION, fps=FPS, paused=True)


@pytest.fixture
def preview(bridge):
    return BoundaryPreview(bridge)


@pytest.fixture
def editor(qapp):
    return EditorStub(
        [{"start": start, "ignored": False, "tags": {}} for start in BOUNDARIES],
        duration=DURATION,
    )


@pytest.fixture
def staging_editor(qapp, tmp_path):
    """A stub whose media path is writable, so on_stage can save a sidecar."""
    return EditorStub(
        [{"start": start, "ignored": False, "tags": {}} for start in BOUNDARIES],
        duration=DURATION,
        media_path=str(tmp_path / "compilation.mp4"),
    )


# ---------------------------------------------------------------------------
# BoundaryPreview: the seek sequence
# ---------------------------------------------------------------------------

class TestPeekSequence:
    def test_peeks_past_the_boundary_then_returns(self, bridge, preview):
        preview.flash(30.0)
        assert bridge.seeks == [30.0, pytest.approx(30.0 + OFFSET)]
        assert preview.pending

        preview._return_to_boundary()
        assert bridge.seeks[-1] == pytest.approx(30.0)
        assert not preview.pending

    def test_the_peek_is_the_configured_frame_count(self, bridge, preview):
        preview.flash(30.0)
        # Frames, not seconds: the offset is a function of the container fps.
        assert bridge.seeks[1] - bridge.seeks[0] == pytest.approx(
            SEGMENT_PREVIEW_FRAMES / FPS)

    def test_the_playhead_rests_on_the_boundary(self, bridge, preview):
        preview.flash(30.0)
        preview._return_to_boundary()
        assert bridge.position == pytest.approx(30.0)

    def test_the_default_dwell_is_short(self, preview):
        assert preview._timer.interval() == SEGMENT_PREVIEW_DWELL_MS
        assert preview._timer.isSingleShot()

    def test_a_second_flash_supersedes_the_first(self, bridge, preview):
        preview.flash(30.0)
        preview.flash(70.0)

        bridge.take_seeks()
        preview._return_to_boundary()

        # Only the newest boundary comes back; the stale one is forgotten.
        assert bridge.seeks == [pytest.approx(70.0)]

    def test_the_peek_never_runs_past_the_end_of_the_media(self, bridge, preview):
        preview.flash(DURATION - 0.1)
        assert bridge.seeks[1] <= DURATION

    def test_a_return_after_a_cancel_does_nothing(self, bridge, preview):
        preview.flash(30.0)
        preview.cancel()
        bridge.take_seeks()

        preview._return_to_boundary()
        assert bridge.seeks == []
        assert not preview.pending


class TestReturnIsGivenUpWhenThePlayheadMoves:
    def test_a_user_seek_is_not_yanked_back(self, bridge, preview):
        preview.flash(30.0)
        bridge.seek_exact(45.0)  # a timeline click, say
        bridge.take_seeks()

        preview._return_to_boundary()
        assert bridge.seeks == []

    def test_ordinary_position_drift_still_returns(self, bridge, preview):
        preview.flash(30.0)
        # mpv reports the frame's real PTS, so the echoed position is not
        # bit-identical to the seek target.
        bridge.position += 0.001
        bridge.take_seeks()

        preview._return_to_boundary()
        assert bridge.seeks == [pytest.approx(30.0)]


class TestPeekIsSkipped:
    def test_when_frames_are_zero(self, bridge, preview):
        preview.frames = 0
        preview.flash(30.0)

        assert bridge.seeks == [30.0]
        assert not preview.pending

    def test_when_frames_are_none(self, bridge, preview):
        preview.frames = None
        preview.flash(30.0)

        assert bridge.seeks == [30.0]
        assert not preview.pending

    def test_when_the_frame_rate_is_unknown(self, bridge, preview):
        bridge.video_fps = None
        preview.flash(30.0)

        assert bridge.seeks == [30.0]
        assert not preview.pending

    def test_while_playing(self, bridge, preview):
        bridge.paused = False
        preview.flash(30.0)

        assert bridge.seeks == [30.0]
        assert not preview.pending

    def test_at_the_first_segment_start(self, bridge, preview):
        """Zero is a legitimate boundary, not a missing one."""
        preview.flash(0.0)
        assert bridge.seeks == [0.0, pytest.approx(OFFSET)]


# ---------------------------------------------------------------------------
# The editor: which transitions peek, and what cancels one
# ---------------------------------------------------------------------------

class TestEditorNavigationPeeks:
    def test_active_next_peeks_past_the_new_boundary(self, editor):
        editor.navigate_to(1)

        assert editor.bridge.seeks == [30.0, pytest.approx(30.0 + OFFSET)]
        assert editor.preview_pending()

        editor.finish_preview()
        assert editor.bridge.seeks[-1] == pytest.approx(30.0)
        assert not editor.preview_pending()

    def test_active_previous_peeks_too(self, editor):
        editor.navigate_to(2)
        editor.bridge.take_seeks()
        editor.navigate_to(1)

        assert editor.bridge.seeks == [30.0, pytest.approx(30.0 + OFFSET)]
        editor.finish_preview()

    def test_navigation_does_not_move_the_active_index_off_target(self, editor):
        editor.navigate_to(2)
        assert editor.current_index == 2
        editor.finish_preview()

    def test_clamped_navigation_does_nothing(self, editor):
        editor.navigate_to(2)  # the last segment
        editor.finish_preview()
        # boundary, peek, boundary: navigation always ends on the boundary.
        assert editor.bridge.take_seeks() == [
            70.0, pytest.approx(70.0 + OFFSET), pytest.approx(70.0)]

        editor.navigate_to(3)  # past the end: refused, so no peek
        assert editor.bridge.take_seeks() == []
        assert not editor.preview_pending()

    def test_start_segment_peeks_on_the_new_segment(self, editor):
        editor.navigate_to(1)
        editor.finish_preview()
        editor.bridge.take_seeks()  # start from a clean seek log

        editor.set_playhead(45.0)
        editor.on_start_segment()

        # The split put a boundary at the playhead, which is now segment 2.
        assert editor.current_index == 2
        assert editor.bridge.take_seeks() == [45.0, pytest.approx(45.0 + OFFSET)]
        editor.finish_preview()

    def test_stage_peeks_on_the_next_segment(self, staging_editor):
        staging_editor.fill_required()
        staging_editor.on_stage()

        assert staging_editor.current_index == 1
        assert staging_editor.bridge.take_seeks() == [
            30.0, pytest.approx(30.0 + OFFSET)]
        staging_editor.finish_preview()

    def test_undo_snaps_without_peeking(self, editor):
        editor._activate(1, preview=False)

        assert editor.bridge.seeks == [30.0]
        assert not editor.preview_pending()

    def test_loading_the_file_snaps_without_peeking(self, editor):
        editor.on_file_loaded("test.mp4")

        assert editor.bridge.seeks == [0.0]
        assert not editor.preview_pending()

    def test_the_peek_can_be_turned_off_for_a_session(self, editor):
        editor.boundary_preview.frames = 0
        editor.navigate_to(1)

        assert editor.bridge.seeks == [30.0]
        assert not editor.preview_pending()

        # And back on, without touching the editor.
        editor.boundary_preview.frames = SEGMENT_PREVIEW_FRAMES
        editor.bridge.take_seeks()
        editor.navigate_to(2)
        assert editor.bridge.seeks == [70.0, pytest.approx(70.0 + OFFSET)]
        editor.finish_preview()


class TestPlayheadActionsCancelThePeek:
    def test_a_timeline_scrub(self, editor):
        editor.navigate_to(1)
        editor.bridge.take_seeks()

        editor.on_seek_requested(50.0)
        editor.finish_preview()

        assert not editor.preview_pending()
        assert editor.bridge.seeks == [50.0]

    def test_a_frame_step(self, editor):
        editor.navigate_to(1)
        editor.bridge.take_seeks()

        editor.on_step_frames(1)
        editor.finish_preview()

        assert not editor.preview_pending()
        assert editor.bridge.frame_steps == [1]
        assert editor.bridge.seeks == []

    def test_a_keyframe_step(self, editor):
        editor.navigate_to(1)
        editor.on_step_keyframe(-1)
        editor.finish_preview()

        assert not editor.preview_pending()
        assert editor.bridge.keyframe_steps == [-1]

    def test_the_transport_button(self, editor):
        editor.navigate_to(1)
        editor.on_transport_clicked()
        editor.finish_preview()

        assert not editor.preview_pending()
        assert editor.bridge.play_toggles == 1

    def test_end_segment_cancels_before_reading_the_playhead(self, editor):
        editor.navigate_to(1)
        # A cut reads the playhead; a pending return must not be able to move
        # it while the cut is being placed.
        editor.set_playhead(50.0)
        editor.on_end_segment()

        assert not editor.preview_pending()
        # The boundary the user asked for, not the peeking one.
        assert editor.segment_model.start(2) == pytest.approx(50.0)
