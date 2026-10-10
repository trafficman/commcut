"""The scanner's marker timelines read as timelines while they are empty.

Both scanner timelines were a uniform dark box until the first marker
landed — Test Scan's midpoints or a Place Boundary press — which looks
like a missing or broken widget. `MarkerTimelineWidget` now paints a
grey track in the band the editor's segment rectangles occupy, as the
first layer, so the empty state still reads as a timeline. These tests
measure the painted pixels, because "the track is there" is a pixel
fact — the same reason `test_splash.py` measures the splash.
"""

import pytest

from editor_stub import ensure_qapp
from scanner.marker_timeline import (
    BACKGROUND_COLOR,
    PLAYHEAD_COLOR,
    TRACK_COLOR,
    TRACK_INSET,
    MarkerTimelineWidget,
)

DURATION = 120.0


@pytest.fixture(scope="module")
def qapp():
    return ensure_qapp()


@pytest.fixture
def timeline(qapp):
    widget = MarkerTimelineWidget()
    widget.resize(800, 60)
    widget.set_duration(DURATION)
    widget.show()
    return widget


def pixel_at(widget, x, y):
    """The colour the widget actually painted at `(x, y)`."""
    return widget.grab().toImage().pixelColor(x, y)


class TestEmptyTimelineShowsATrack:
    def test_the_middle_band_is_track_grey(self, timeline):
        assert pixel_at(timeline, 400, 30) == TRACK_COLOR

    def test_the_track_occupies_the_segment_band(self, timeline):
        """The track fills the band the editor's segment rectangles
        occupy — TRACK_INSET from each edge — and nothing beyond it."""
        assert pixel_at(timeline, 400, TRACK_INSET - 1) == BACKGROUND_COLOR
        assert pixel_at(timeline, 400, TRACK_INSET) == TRACK_COLOR
        bottom = timeline.height() - TRACK_INSET
        assert pixel_at(timeline, 400, bottom - 1) == TRACK_COLOR
        assert pixel_at(timeline, 400, bottom) == BACKGROUND_COLOR

    def test_the_bands_above_and_below_the_track_stay_background(self, timeline):
        assert pixel_at(timeline, 400, 0) == BACKGROUND_COLOR
        assert pixel_at(timeline, 400, timeline.height() - 1) == BACKGROUND_COLOR

    def test_the_track_is_there_before_the_duration_is_known(self, qapp):
        widget = MarkerTimelineWidget()
        widget.resize(800, 60)
        widget.show()
        assert pixel_at(widget, 400, 30) == TRACK_COLOR


class TestMarkersDrawOverTheTrack:
    def test_a_marker_line_draws_over_the_track(self, timeline):
        timeline.set_markers([DURATION / 2])
        x = int(timeline.time_to_x(DURATION / 2))
        # A 1px line antialiases across two pixels, so the painted
        # colour is a blend; what matters is that the marker, not the
        # track, is what painted there.
        assert pixel_at(timeline, x, 30) != TRACK_COLOR

    def test_the_playhead_draws_over_the_track(self, timeline):
        # The 2px playhead at x=0 fully covers its pixel, so its
        # colour is exact — and proves it paints above the track.
        assert pixel_at(timeline, 0, 30) == PLAYHEAD_COLOR
