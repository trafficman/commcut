"""Zoom state must survive a timeline/window resize.

Regression: TimelineWidget.resizeEvent used to re-zoom to the active segment
unconditionally, so widening the editor window silently undid a zoom-to-fit
while the zoom toggle still showed the fit state. The widget now remembers the
zoom mode and re-applies that mode on resize.
"""

import pytest

from editor_stub import EditorStub, ensure_qapp
from shared.timeline import (
    ACTIVE_SEGMENT_VIEW_FRACTION, TimelineWidget, Segment, ZOOM_FIT, ZOOM_SEGMENT,
)

DURATION = 120.0
SEGMENTS = [
    Segment(0.0, 30.0),
    Segment(30.0, 70.0),
    Segment(70.0, 120.0),
]


@pytest.fixture(scope="module")
def qapp():
    return ensure_qapp()


@pytest.fixture
def timeline(qapp):
    widget = TimelineWidget()
    widget.resize(800, 60)
    widget.set_duration(DURATION)
    widget.set_segments(SEGMENTS)
    # Qt only delivers resizeEvent to a visible widget, and the resize is the
    # thing under test — a hidden widget would never re-apply the zoom.
    widget.show()
    return widget


def visible_seconds(timeline):
    """How much of the source the widget currently shows."""
    return timeline.width() / timeline.pixels_per_second


class TestFitModeSurvivesResize:
    def test_widening_keeps_the_whole_video_in_view(self, timeline):
        timeline.set_zoom_mode(ZOOM_FIT)
        timeline.resize(1600, 60)

        assert timeline.zoom_mode == ZOOM_FIT
        assert timeline.pixels_per_second == pytest.approx(1600 / DURATION)
        assert timeline.scroll_offset == 0.0
        assert visible_seconds(timeline) == pytest.approx(DURATION)

    def test_narrowing_keeps_the_whole_video_in_view(self, timeline):
        timeline.set_zoom_mode(ZOOM_FIT)
        timeline.resize(400, 60)

        assert visible_seconds(timeline) == pytest.approx(DURATION)

    def test_the_widened_view_is_not_the_active_segment_zoom(self, timeline):
        """The old bug replaced the fit zoom with the active-segment zoom."""
        timeline.set_zoom_mode(ZOOM_FIT)
        timeline.resize(1600, 60)

        segment_zoom = 1600 / (30.0 / ACTIVE_SEGMENT_VIEW_FRACTION)
        assert timeline.pixels_per_second != pytest.approx(segment_zoom)
        assert timeline.pixels_per_second < segment_zoom


class TestSegmentModeSurvivesResize:
    def test_widening_keeps_the_active_segment_filling_the_view(self, timeline):
        timeline.set_zoom_mode(ZOOM_SEGMENT, 1)
        timeline.resize(1600, 60)

        assert timeline.zoom_mode == ZOOM_SEGMENT
        # Segment 1 is 40s; it still fills ACTIVE_SEGMENT_VIEW_FRACTION.
        expected_view = 40.0 / ACTIVE_SEGMENT_VIEW_FRACTION
        assert visible_seconds(timeline) == pytest.approx(expected_view)

    def test_widening_still_centers_on_the_active_segment(self, timeline):
        timeline.set_zoom_mode(ZOOM_SEGMENT, 1)
        timeline.resize(1600, 60)

        left_edge = timeline.x_to_time(0)
        right_edge = timeline.x_to_time(timeline.width())
        # Segment 1 (30s-70s) is centered, with neighbours peeking in.
        assert left_edge < 30.0 < 70.0 < right_edge
        assert 50.0 - left_edge == pytest.approx(right_edge - 50.0)

    def test_resize_recenters_on_the_active_segment_not_a_stale_one(self, timeline):
        timeline.set_zoom_mode(ZOOM_SEGMENT, 0)
        timeline.set_active_index(2)
        timeline.resize(1600, 60)

        # Segment 2 runs 70-120, the last one, so the view is clamped to the
        # end of the source rather than centered on it.
        assert timeline.x_to_time(timeline.width()) == pytest.approx(120.0)
        assert visible_seconds(timeline) == pytest.approx(
            50.0 / ACTIVE_SEGMENT_VIEW_FRACTION)

    def test_apply_zoom_uses_the_remembered_mode_after_a_data_change(self, timeline):
        """Duration changes are not resize events; the mode is re-applied."""
        timeline.set_zoom_mode(ZOOM_FIT, 1)
        timeline.set_duration(240.0)
        timeline.apply_zoom()

        assert visible_seconds(timeline) == pytest.approx(240.0)
        assert timeline.scroll_offset == 0.0


class TestZoomModeValidation:
    def test_unknown_mode_is_rejected(self, timeline):
        with pytest.raises(ValueError):
            timeline.set_zoom_mode("zoom-to-fit-hard")

    def test_default_mode_is_fit(self):
        # A freshly built widget must not assume zoom-to-active-segment, or a
        # resize before the first zoom call would surprise the user.
        assert TimelineWidget().zoom_mode == ZOOM_FIT

    def test_fit_needs_no_segments(self, qapp):
        """Fit is well defined before any segments are known."""
        widget = TimelineWidget()
        widget.resize(800, 60)
        widget.set_duration(DURATION)
        widget.show()
        widget.set_zoom_mode(ZOOM_FIT)
        widget.resize(1600, 60)

        assert widget.pixels_per_second == pytest.approx(1600 / DURATION)


class TestEditorZoomToggle:
    """The editor's toggle and the widget's zoom must never disagree."""

    @pytest.fixture
    def editor(self, qapp):
        return EditorStub(
            [{"start": s.start, "ignored": False, "tags": {}} for s in SEGMENTS],
            duration=DURATION,
        )

    def test_opens_zoomed_to_the_active_segment(self, editor):
        assert editor.ui.toggleZoom.isChecked()
        assert editor.timeline().zoom_mode == ZOOM_SEGMENT

    def test_zoom_out_toggle_sets_fit_mode(self, editor):
        editor.zoom_fit_toggle(False)

        assert editor.timeline().zoom_mode == ZOOM_FIT
        assert visible_seconds(editor.timeline()) == pytest.approx(DURATION)

    def test_resize_after_zoom_out_keeps_the_whole_video_in_view(self, editor):
        editor.zoom_fit_toggle(False)
        editor.resize_timeline(1600)

        timeline = editor.timeline()
        assert timeline.zoom_mode == ZOOM_FIT
        assert visible_seconds(timeline) == pytest.approx(DURATION)
        # The button state is still the source of truth and still agrees.
        assert not editor.ui.toggleZoom.isChecked()

    def test_resize_after_zoom_in_keeps_the_active_segment_filling_the_view(self, editor):
        editor.zoom_fit_toggle(True)
        editor.resize_timeline(1600)

        timeline = editor.timeline()
        assert timeline.zoom_mode == ZOOM_SEGMENT
        assert visible_seconds(timeline) == pytest.approx(
            30.0 / ACTIVE_SEGMENT_VIEW_FRACTION)

    def test_refreshing_the_timeline_follows_the_toggle(self, editor):
        editor.zoom_fit_toggle(False)
        editor.go_to(1)

        assert editor.timeline().zoom_mode == ZOOM_FIT
        assert visible_seconds(editor.timeline()) == pytest.approx(DURATION)

    def test_navigating_while_fit_keeps_fit_and_snaps_after_zoom_in(self, editor):
        editor.zoom_fit_toggle(False)
        editor.go_to(2)
        editor.resize_timeline(1200)
        assert visible_seconds(editor.timeline()) == pytest.approx(DURATION)

        editor.zoom_fit_toggle(True)
        timeline = editor.timeline()
        assert timeline.zoom_segment_index == 2
        assert timeline.x_to_time(timeline.width()) == pytest.approx(DURATION)
