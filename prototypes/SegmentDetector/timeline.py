from dataclasses import dataclass
from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter, QColor, QPen, QBrush


@dataclass
class Segment:
    """A contiguous unedited region of the source timeline."""
    start: float  # seconds
    end: float    # seconds


class TimelineWidget(QWidget):
    """Zoom-ready playback timeline.

    All time<->pixel conversion goes through time_to_x/x_to_time, so zooming
    and scrolling only need to change pixels_per_second and scroll_offset.
    """

    seekRequested = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(50)

        self.duration = 0.0
        self.position = 0.0
        self.segments = []

        # Zoom/scroll state. pixels_per_second is the zoom level; scroll_offset
        # is how many seconds are scrolled off the left edge.
        self.pixels_per_second = 100.0
        self.scroll_offset = 0.0

        self._dragging = False

    # --- coordinate mapping (single source of truth) ---
    def time_to_x(self, t):
        return (t - self.scroll_offset) * self.pixels_per_second

    def x_to_time(self, x):
        return x / self.pixels_per_second + self.scroll_offset

    # --- data setters ---
    def set_duration(self, duration):
        self.duration = duration
        self.update()

    def set_position(self, position):
        self.position = position
        self.update()

    def set_segments(self, segments):
        self.segments = segments
        self.update()

    # --- zoom ---
    def set_pixels_per_second(self, pps):
        self.pixels_per_second = max(1.0, pps)
        self.update()

    def zoom_fit(self):
        """Fit the whole duration into the current widget width."""
        if self.duration > 0 and self.width() > 0:
            self.pixels_per_second = self.width() / self.duration
            self.scroll_offset = 0.0
        self.update()

    # --- rendering ---
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        width = self.width()
        height = self.height()

        painter.fillRect(0, 0, width, height, QColor(30, 30, 30))

        if self.duration <= 0:
            return

        # Layer 1: segment blocks
        segment_brush = QBrush(QColor(50, 90, 140, 180))
        painter.setBrush(segment_brush)
        painter.setPen(Qt.NoPen)
        for seg in self.segments:
            x_start = self.time_to_x(seg.start)
            x_end = self.time_to_x(seg.end)
            painter.drawRect(int(x_start), 10, max(2, int(x_end - x_start)), height - 20)

        # Layer 2: playhead
        playhead_x = int(self.time_to_x(self.position))
        painter.setPen(QPen(QColor(230, 80, 80), 2))
        painter.drawLine(playhead_x, 0, playhead_x, height)

    # --- interaction ---
    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton or self.duration <= 0:
            return
        self._dragging = True
        self.set_position(self._clamp_time(event.position().x()))
        self.seekRequested.emit(self.position)

    def mouseMoveEvent(self, event):
        if not self._dragging:
            return
        self.set_position(self._clamp_time(event.position().x()))
        self.seekRequested.emit(self.position)

    def mouseReleaseEvent(self, event):
        self._dragging = False

    def resizeEvent(self, event):
        # Keep the whole timeline fitted when the widget is resized.
        self.zoom_fit()
        super().resizeEvent(event)

    def _clamp_time(self, x):
        return max(0.0, min(self.duration, self.x_to_time(x)))
