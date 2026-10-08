"""Universal loading indicator for CPU-intensive work.

Applies to: ``shared/loading.py``, ``main.py``, ``scanner/scanner.py``,
``editor/editor.py``, ``shared/splash.py``, ``settings/settings.py``,
``importer/importrun.py``, ``importer/values.py``.

Provides two interchangeable indicators:

- ``LoadingSplash`` wraps a ``QSplashScreen`` — the same banner-based splash the
  app uses at startup. It is for synchronous work of unknown duration where the
  GUI thread is blocked (e.g. ffprobe keyframe scan during window construction).
  The caller must close it before constructing any mpv-backed widget, per
  invariant 6 in AGENTS.md.

- ``LoadingDialog`` is a dialog with a progress bar and an optional cancel link.
  It is for work running on a worker ``QThread`` where the GUI thread is free
  (e.g. the editor's batch export, the scanner's full-source blackdetect scan).
  It stays up for the duration of the task and reports progress via signals.

Both share the same visual language: the commcut banner centered above a
status message. The goal is that every long-running operation in the app
presents the same face to the user, with the same cancel semantics and the same
error handling.

Usage (splash):
    with LoadingSplash(app, "Scanning keyframes…") as splash:
        keyframes = scan_keyframes(path)

Usage (modal dialog, exec):
    dialog = LoadingDialog("Scanning full source…", cancellable=True)
    worker.scanned.connect(dialog.accept)
    worker.failed.connect(lambda reason: (log(reason), dialog.reject()))
    dialog.canceled.connect(worker.cancel)
    thread.start()
    result = dialog.exec()
    if result == QDialog.Accepted:
        # success
    else:
        # cancelled or failed

Usage (modeless dialog, show):
    dialog = LoadingDialog("Scanning full source…", cancellable=True, modal=False)
    worker.scanned.connect(lambda: dialog.deleteLater())
    worker.failed.connect(lambda reason: (log(reason), dialog.deleteLater()))
    dialog.canceled.connect(worker.cancel)
    thread.start()
    dialog.show()
    # teardown via deleteLater() when the worker finishes
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QLabel, QProgressBar,
    QSplashScreen, QVBoxLayout,
)

from shared.splash import (
    MARGIN, MESSAGE_BAND, MESSAGE_COLOR, SPLASH_HEIGHT, SPLASH_WIDTH,
    splash_pixmap,
)


class LoadingSplash:
    """A splash screen that stays visible for the duration of a ``with`` block.

    Wraps ``QSplashScreen`` with the app's banner pixmap and delegates
    ``showMessage`` to it, so a caller gets the identical visual as the startup
    splash via a context manager instead of manual show/close calls.

    If the GUI thread is blocked inside the ``with`` body (typical for truly
    synchronous ffprobe/ffmpeg work), the splash will not repaint until the body
    returns. Callers that run blocking work should call
    ``app.processEvents()`` once after entering the block, as ``show_splash``
    in ``shared/splash.py`` already does.
    """

    def __init__(self, app, message: str):
        self._app = app
        self._splash = QSplashScreen(splash_pixmap())
        self._splash.showMessage(
            message, Qt.AlignHCenter | Qt.AlignBottom, MESSAGE_COLOR)
        self._splash.show()
        app.processEvents()

    def set_message(self, message: str):
        self._splash.showMessage(
            message, Qt.AlignHCenter | Qt.AlignBottom, MESSAGE_COLOR)

    def finish(self, widget=None):
        """Hide the splash. If ``widget`` is given it must be shown first.

        Delegates to ``QSplashScreen.finish`` — calling it with ``None`` simply
        hides the splash, which is what a timed display needs.
        """
        self._splash.finish(widget)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._splash.finish(None)
        return False


class LoadingDialog(QDialog):
    """A dialog with a progress bar and optional cancel link.

    Mirrors the visual language of the splash — the commcut banner above a
    status message — but adds a progress bar so it can cover a worker
    thread's lifetime and a Cancel link when the operation is cancellable.

    By default the dialog is modal and meant to be driven with ``exec()``.
    Pass ``modal=False`` for modeless use: the dialog is shown with ``show()``
    and stays visible (showing "Cancelling…") while the worker shuts down,
    matching the pattern of a non-modal progress indicator.

    Typical wiring (modal):

    ::

        dialog = LoadingDialog("Scanning full source…", cancellable=True)
        worker.scanned.connect(dialog.accept)
        worker.failed.connect(lambda r: (log(r), dialog.reject()))
        dialog.canceled.connect(worker.cancel)
        thread.start()
        result = dialog.exec()

    Typical wiring (modeless):

    ::

        dialog = LoadingDialog("Scanning full source…", cancellable=True,
                               modal=False)
        worker.scanned.connect(lambda: dialog.deleteLater())
        worker.failed.connect(lambda r: (log(r), dialog.deleteLater()))
        dialog.canceled.connect(worker.cancel)
        thread.start()
        dialog.show()
    """

    #: Standard width, the same as the splash so both feel like the same screen.
    WIDTH = SPLASH_WIDTH

    def __init__(self, message: str, cancellable: bool = False,
                 parent=None, modal: bool = True, title: str = "commcut"):
        super().__init__(parent, Qt.WindowTitleHint | Qt.WindowCloseButtonHint)
        self.setWindowTitle(title)
        self.setModal(modal)
        self.setFixedWidth(self.WIDTH)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        layout.setSpacing(0)

        self._banner = QLabel()
        self._banner.setPixmap(
            splash_pixmap().scaled(
                SPLASH_WIDTH - 2 * MARGIN,
                SPLASH_HEIGHT - 2 * MARGIN - MESSAGE_BAND,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )
        self._banner.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._banner)

        self._message = QLabel(message)
        self._message.setWordWrap(True)
        self._message.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._message)

        layout.addSpacing(8)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(8)
        layout.addWidget(self._progress)

        layout.addSpacing(12)

        if cancellable:
            self._cancel_link = _CancelLink()
            self._cancel_link.clicked.connect(self._on_cancel_clicked)
            layout.addWidget(self._cancel_link, alignment=Qt.AlignHCenter)
        else:
            self._cancel_link = None

    canceled = Signal()

    def _on_cancel_clicked(self):
        self.setEnabled(False)
        self.canceled.emit()
        if self.isModal():
            self.reject()

    @property
    def progress_bar(self) -> QProgressBar:
        return self._progress

    def set_message(self, message: str):
        self._message.setText(message)

    def set_range(self, minimum: int, maximum: int):
        self._progress.setRange(int(minimum), int(maximum))

    def set_value(self, value: int):
        self._progress.setValue(int(value))


class _CancelLink(QLabel):
    """A small "Cancel" text link.

    Lighter than a button for a modal dialog — it reads as a link rather than
    another action button, and matches the app's minimal chrome.
    """

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__("Cancel", parent)
        self.setTextFormat(Qt.RichText)
        self.setCursor(Qt.PointingHandCursor)
        self._refresh(False)

    def _refresh(self, hovered: bool):
        color = "#0066cc" if hovered else "#333"
        self.setText(
            f'<a href="#" style="color:{color};text-decoration:none">Cancel</a>'
        )

    def enterEvent(self, event):
        self._refresh(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._refresh(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)