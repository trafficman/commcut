from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter, QColor, QPen


# Color of the marker lines.
MARKER_COLOR = QColor(220, 220, 220)
PLAYHEAD_COLOR = QColor(230, 80, 80)
BACKGROUND_COLOR = QColor(30, 30, 30)


class MarkerTimelineWidget(QWidget):
    """A simple timeline that draws a playhead and vertical lines at marker times.

    Unlike the editor's TimelineWidget, this has no zoom/scroll state and no
    segment rendering — the full duration is always mapped to the widget width.
    Clicking seeks the playhead.
    """

    seekRequested = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(50)

        self.duration = 0.0
        self.position = 0.0
        self.markers = []

        self._dragging = False

    # --- coordinate mapping (no zoom: full duration fills the widget) ---
    def time_to_x(self, t):
        if self.duration <= 0 or self.width() <= 0:
            return 0
        return (t / self.duration) * self.width()

    def x_to_time(self, x):
        if self.width() <= 0 or self.duration <= 0:
            return 0.0
        return max(0.0, min(self.duration, (x / self.width()) * self.duration))

    # --- data setters ---
    def set_duration(self, duration):
        self.duration = duration
        self.update()

    def set_position(self, position):
        self.position = position
        self.update()

    def set_markers(self, markers):
        """Set the list of marker times (seconds) to draw as vertical lines."""
        self.markers = list(markers)
        self.update()

    # --- rendering ---
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        width = self.width()
        height = self.height()

        painter.fillRect(0, 0, width, height, BACKGROUND_COLOR)

        if self.duration <= 0:
            return

        # Layer 1: marker lines (vertical, full height)
        painter.setPen(QPen(MARKER_COLOR, 1))
        for t in self.markers:
            x = int(self.time_to_x(t))
            if 0 <= x <= width:
                painter.drawLine(x, 0, x, height)

        # Layer 2: playhead
        playhead_x = int(self.time_to_x(self.position))
        painter.setPen(QPen(PLAYHEAD_COLOR, 2))
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

    def _clamp_time(self, x):
        return max(0.0, min(self.duration, self.x_to_time(x)))
