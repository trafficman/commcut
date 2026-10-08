"""Tests for the scanner's worker-thread refactor.

Covers four changes in scanner/scanner.py:

  A. ScannerPreScanWorker — clip_to_temp + scan_keyframes moved off the GUI
     thread in create(), behind a modal LoadingDialog.exec().
  B. TestScanWorker — blackdetect on the preview clip moved off the GUI thread
     in on_test_scan().
  C. FinishedScanWorker — probe_duration folded into run() so the full-source
     scan no longer blocks the GUI thread to measure the video.
  D. ScannerWindow.__init__ — no longer builds its own preview clip; accepts
     clip_path and keyframes as constructor arguments.
  E. Both scan workers resolve the ffmpeg/ffprobe binary inside run()'s
     try, so a machine without the binaries reports failed() instead of
     dying mid-thread — and the tests stub get_binary_path, which the
     workers call before the stubbed _run_ffmpeg.

The ScannerWindow itself needs libmpv and a .ui file, so a ScannerStub mirrors
its lifecycle methods (close to EditorStub's pattern) without constructing a
player. The workers are plain QObjects and are tested directly.
"""

import inspect
import threading
import time

import pytest
from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QMainWindow, QPushButton

import scanner.scanner as scanner_module
from scanner.scanner import (
    FinishedScanWorker,
    ScannerPreScanWorker,
    ScannerWindow,
    TestScanWorker,
)
from editor_stub import (
    FakeBridge,
    FakeLoadingDialog,
    FakeThread,
    ensure_qapp,
)
from shared.ffmpeg import ExportCancelled


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeSlider:
    """A minimal stand-in for QSlider: just enough for value()."""

    def __init__(self, value=8):
        self._value = value

    def value(self):
        return self._value


class _FakeTimeline:
    """Records set_markers calls for assertion."""

    def __init__(self):
        self.markers = None

    def set_markers(self, markers):
        self.markers = markers


class _FakeMessageBox:
    """QMessageBox that records instead of blocking."""

    Yes = 16
    No = 64
    answers = []
    questions = []
    warnings = []

    @staticmethod
    def question(_parent, title, message, *_buttons, default=0):
        _FakeMessageBox.questions.append((title, message))
        return _FakeMessageBox.answers.pop(0) if _FakeMessageBox.answers else _FakeMessageBox.No

    @staticmethod
    def warning(_parent, title, message, *_args):
        _FakeMessageBox.warnings.append((title, message))


class _ShellStub:
    """Stubs shared.session.shell so _on_scan_complete can call open_safely."""

    def __init__(self):
        self.opened = []

    def open_safely(self, name, **kwargs):
        self.opened.append((name, kwargs))


class _FakeModel:
    """Stubs SegmentModel so _on_scan_complete does not touch the filesystem."""

    def save(self, path):
        pass

    def segment_count(self):
        return 1


# ---------------------------------------------------------------------------
# Stub workers — real Worker logic, no-op moveToThread (test_mesh_window.py pattern)
# ---------------------------------------------------------------------------

class StubTestScanWorker(TestScanWorker):
    """TestScanWorker with moveToThread as a no-op so FakeThread works.

    moveToThread onto a QThread that never starts an event loop leaves the
    worker queued to a loop that never runs, so started.connect(worker.run)
    never fires.  Making moveToThread a no-op keeps the worker in the GUI
    thread, so the FakeThread's started/finished emits deliver directly.
    """

    def moveToThread(self, thread):
        pass


class StubFinishedScanWorker(FinishedScanWorker):
    """FinishedScanWorker with moveToThread as a no-op."""

    def moveToThread(self, thread):
        pass


# ---------------------------------------------------------------------------
# ScannerStub — minimal ScannerWindow without libmpv
# ---------------------------------------------------------------------------

class ScannerStub(ScannerWindow):
    """Minimal ScannerWindow for testing lifecycle methods without libmpv.

    Inherits from ScannerWindow so super() in closeEvent resolves correctly,
    but skips ScannerWindow.__init__ (which needs libmpv + .ui) by calling
    QMainWindow.__init__ directly. All attributes the lifecycle methods touch
    are set up manually.
    """

    def __init__(self, clip_path="/fake/clip.mp4", video_fps=23.976):
        QMainWindow.__init__(self)
        self.source_path = "/fake/source.mp4"
        self.clip_path = clip_path
        self.bridge = FakeBridge(fps=video_fps)
        self.shell = _ShellStub()

        self.ui = type("Ui", (), {})()
        self.ui.scanButton = QPushButton()
        self.ui.timelineWidget1 = _FakeTimeline()
        self.ui.timelineWidget2 = _FakeTimeline()
        self.ui.horizontalSlider = _FakeSlider(8)
        self.ui.horizontalSlider_2 = _FakeSlider(90)

        self._scan_thread = None
        self._scan_worker = None
        self._scan_dialog = None
        self._test_scan_thread = None
        self._test_scan_worker = None
        self._test_scan_dialog = None
        self._close_after_worker = False
        self._enabled = True

    def setEnabled(self, enabled):
        self._enabled = enabled

    def isEnabled(self):
        return self._enabled


@pytest.fixture
def qapp():
    return ensure_qapp()


@pytest.fixture
def stub(qapp, monkeypatch):
    """A ScannerStub with QThread, LoadingDialog, QMessageBox, shell, log,
    _model_from_midpoints, and the worker classes all stubbed."""
    monkeypatch.setattr(scanner_module, "QThread", FakeThread)
    monkeypatch.setattr(scanner_module, "LoadingDialog", FakeLoadingDialog)
    monkeypatch.setattr(scanner_module, "QMessageBox", _FakeMessageBox)
    monkeypatch.setattr(scanner_module, "log", lambda *a: None)
    monkeypatch.setattr(scanner_module, "get_binary_path",
                        lambda name: "ffmpeg")
    monkeypatch.setattr(scanner_module, "_model_from_midpoints",
                        lambda midpoints, duration, name: _FakeModel())
    monkeypatch.setattr(scanner_module, "TestScanWorker", StubTestScanWorker)
    monkeypatch.setattr(scanner_module, "FinishedScanWorker",
                        StubFinishedScanWorker)

    _FakeMessageBox.answers = []
    _FakeMessageBox.questions = []
    _FakeMessageBox.warnings = []

    instance = ScannerStub()
    monkeypatch.setattr(scanner_module, "shell", lambda: instance.shell)
    return instance


# ---------------------------------------------------------------------------
# ScannerPreScanWorker unit tests
# ---------------------------------------------------------------------------

def test_pre_scan_worker_emits_result_on_success(monkeypatch, qapp):
    """clip_to_temp + scan_keyframes are called and result(clip, keyframes) fires."""
    monkeypatch.setattr(scanner_module, "clip_to_temp",
                        lambda *a, **k: "/tmp/preview.mp4")
    monkeypatch.setattr(scanner_module, "scan_keyframes",
                        lambda path: [0.0, 1.2, 3.4])

    worker = ScannerPreScanWorker("/source.mp4")
    received = []
    worker.result.connect(lambda clip, kf: received.append((clip, kf)))

    worker.run()

    assert received == [("/tmp/preview.mp4", [0.0, 1.2, 3.4])]
    assert worker.clip_path == "/tmp/preview.mp4"
    assert worker.keyframes == [0.0, 1.2, 3.4]


def test_pre_scan_worker_fails_when_clip_is_none(monkeypatch, qapp):
    """clip_to_temp returning None means the preview could not be built."""
    monkeypatch.setattr(scanner_module, "clip_to_temp", lambda *a, **k: None)
    monkeypatch.setattr(scanner_module, "scan_keyframes", lambda p: [])

    worker = ScannerPreScanWorker("/source.mp4")
    received = []
    worker.failed.connect(lambda reason: received.append(reason))

    worker.run()

    assert len(received) == 1
    assert "preview" in received[0].lower()
    assert worker.clip_path is None
    assert worker.keyframes is None


def test_pre_scan_worker_fails_on_exception(monkeypatch, qapp):
    """An unexpected error in clip_to_temp surfaces as a failed signal."""
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(scanner_module, "clip_to_temp", boom)

    worker = ScannerPreScanWorker("/source.mp4")
    received = []
    worker.failed.connect(lambda r: received.append(r))

    worker.run()

    assert len(received) == 1
    assert "OSError" in received[0]


# ---------------------------------------------------------------------------
# TestScanWorker unit tests
# ---------------------------------------------------------------------------

def test_test_scan_worker_emits_midpoints(monkeypatch, qapp):
    """blackdetect stderr is parsed and midpoints emitted via result."""
    fake_stderr = ("[blackdetect @ 0x1] black_start:1.000000 black_end:2.000000\n"
                   "[blackdetect @ 0x2] black_start:5.000000 black_end:7.000000\n")
    monkeypatch.setattr(scanner_module, "get_binary_path",
                        lambda name: "ffmpeg")
    monkeypatch.setattr(scanner_module, "_run_ffmpeg",
                        lambda cmd, should_cancel=None: (0, fake_stderr))

    worker = TestScanWorker("/clip.mp4", min_sec=0.5, pix_th=0.10)
    received = []
    worker.result.connect(lambda midpoints: received.append(midpoints))

    worker.run()

    assert received == [[1.5, 6.0]]


def test_test_scan_worker_fails_on_cancel(monkeypatch, qapp):
    """ExportCancelled is caught and surfaces as failed."""
    def raises(*a, **k):
        raise ExportCancelled()

    monkeypatch.setattr(scanner_module, "get_binary_path",
                        lambda name: "ffmpeg")
    monkeypatch.setattr(scanner_module, "_run_ffmpeg", raises)

    worker = TestScanWorker("/clip.mp4", min_sec=0.5, pix_th=0.10)
    received = []
    worker.failed.connect(lambda r: received.append(r))

    worker.run()

    assert len(received) == 1
    assert "cancel" in received[0].lower()


def test_test_scan_worker_fails_on_exception(monkeypatch, qapp):
    """A non-cancel exception surfaces as failed."""
    def boom(*a, **k):
        raise RuntimeError("ffprobe broke")

    monkeypatch.setattr(scanner_module, "get_binary_path",
                        lambda name: "ffmpeg")
    monkeypatch.setattr(scanner_module, "_run_ffmpeg", boom)

    worker = TestScanWorker("/clip.mp4", min_sec=0.5, pix_th=0.10)
    received = []
    worker.failed.connect(lambda r: received.append(r))

    worker.run()

    assert len(received) == 1
    assert "RuntimeError" in received[0]


def test_test_scan_worker_fails_when_ffmpeg_missing(monkeypatch, qapp):
    """A machine without ffmpeg reports failed, not a dead thread."""
    def missing(name):
        raise FileNotFoundError(f"no {name}")

    monkeypatch.setattr(scanner_module, "get_binary_path", missing)

    worker = TestScanWorker("/clip.mp4", min_sec=0.5, pix_th=0.10)
    received = []
    worker.failed.connect(lambda r: received.append(r))

    worker.run()

    assert len(received) == 1
    assert "FileNotFoundError" in received[0]


# ---------------------------------------------------------------------------
# FinishedScanWorker unit tests
# ---------------------------------------------------------------------------

def test_finished_scan_worker_probes_duration_and_emits_scanned(monkeypatch, qapp):
    """probe_duration runs inside run() and its result travels with midpoints."""
    fake_stderr = "[blackdetect @ 0x1] black_start:10.000000 black_end:12.000000\n"
    monkeypatch.setattr(scanner_module, "probe_duration", lambda path: 120.0)
    monkeypatch.setattr(scanner_module, "get_binary_path",
                        lambda name: "ffmpeg")
    monkeypatch.setattr(
        scanner_module, "_run_ffmpeg",
        lambda cmd, should_cancel=None: (0, fake_stderr))

    worker = FinishedScanWorker("/source.mp4", min_sec=0.5, pix_th=0.10)
    received = []
    worker.scanned.connect(
        lambda midpoints, duration: received.append((midpoints, duration)))

    worker.run()

    assert received == [([11.0], 120.0)]


def test_finished_scan_worker_fails_when_duration_unknown(monkeypatch, qapp):
    """probe_duration returning None means the scan cannot run."""
    monkeypatch.setattr(scanner_module, "probe_duration", lambda path: None)

    worker = FinishedScanWorker("/source.mp4", min_sec=0.5, pix_th=0.10)
    received = []
    worker.failed.connect(lambda r: received.append(r))

    worker.run()

    assert len(received) == 1
    assert "duration" in received[0].lower()


def test_finished_scan_worker_fails_when_ffmpeg_missing(monkeypatch, qapp):
    """A machine without ffmpeg reports failed, not a dead thread."""
    def missing(name):
        raise FileNotFoundError(f"no {name}")

    monkeypatch.setattr(scanner_module, "probe_duration", lambda path: 120.0)
    monkeypatch.setattr(scanner_module, "get_binary_path", missing)

    worker = FinishedScanWorker("/source.mp4", min_sec=0.5, pix_th=0.10)
    received = []
    worker.failed.connect(lambda r: received.append(r))

    worker.run()

    assert len(received) == 1
    assert "FileNotFoundError" in received[0]


def test_finished_scan_worker_fails_when_ffprobe_missing(monkeypatch, qapp):
    """A machine without ffprobe reports failed, not a dead thread."""
    def missing(name):
        raise FileNotFoundError(f"no {name}")

    monkeypatch.setattr(scanner_module, "probe_duration", missing)

    worker = FinishedScanWorker("/source.mp4", min_sec=0.5, pix_th=0.10)
    received = []
    worker.failed.connect(lambda r: received.append(r))

    worker.run()

    assert len(received) == 1
    assert "FileNotFoundError" in received[0]


def test_finished_scan_worker_no_longer_takes_duration_arg():
    """FinishedScanWorker.__init__ dropped the duration parameter."""
    params = list(inspect.signature(FinishedScanWorker.__init__).parameters)
    assert "duration" not in params


# ---------------------------------------------------------------------------
# Fake-thread lifecycle tests (threading wiring)
# ---------------------------------------------------------------------------

def test_test_scan_thread_quits_and_tears_down_on_result(stub, monkeypatch):
    """_start_test_scan creates the worker+thread; result triggers teardown."""
    fake_stderr = ("[blackdetect @ 0x1] black_start:1.000000 black_end:2.000000\n"
                   "[blackdetect @ 0x2] black_start:5.000000 black_end:7.000000\n")
    monkeypatch.setattr(scanner_module, "_run_ffmpeg",
                        lambda cmd, should_cancel=None: (0, fake_stderr))

    assert stub._test_scan_thread is None
    assert stub._test_scan_worker is None

    stub._start_test_scan("/clip.mp4", 0.5, 0.10)

    # FakeThread.start() runs the worker synchronously, emits result, then
    # emits finished -> _on_test_scan_stopped.
    assert stub._test_scan_thread is None, "teardown cleared the thread"
    assert stub._test_scan_worker is None, "teardown cleared the worker"
    assert stub._test_scan_dialog is None, "teardown cleared the dialog"
    assert stub.ui.timelineWidget1.markers == [1.5, 6.0], "markers were set"
    assert stub.ui.scanButton.isEnabled(), "button was re-enabled"


def test_test_scan_thread_quits_and_tears_down_on_failed(stub, monkeypatch):
    """A failed test scan also quits the thread and tears down."""
    def raises(*a, **k):
        raise ExportCancelled()

    monkeypatch.setattr(scanner_module, "_run_ffmpeg", raises)

    stub._start_test_scan("/clip.mp4", 0.5, 0.10)

    assert stub._test_scan_thread is None
    assert stub._test_scan_worker is None
    assert any("cancel" in w[1].lower() for w in _FakeMessageBox.warnings)


def test_finished_scan_thread_quits_and_tears_down_on_scanned(stub, monkeypatch):
    """_start_scan creates the worker; scanned triggers teardown and hand-off."""
    fake_stderr = "[blackdetect @ 0x1] black_start:10.000000 black_end:12.000000\n"
    monkeypatch.setattr(scanner_module, "probe_duration", lambda path: 120.0)
    monkeypatch.setattr(
        scanner_module, "_run_ffmpeg",
        lambda cmd, should_cancel=None: (0, fake_stderr))

    stub._close_after_worker = False

    closed = []
    monkeypatch.setattr(stub, "close", lambda: closed.append(True))

    stub._start_scan("/source.mp4", 0.5, 0.10)

    assert stub._scan_thread is None, "teardown cleared the thread"
    assert stub._scan_worker is None
    assert stub._scan_dialog is None
    assert stub.shell.opened == [("editor", {"source": "/fake/source.mp4"})]
    assert closed, "window closed after successful scan"


def test_finished_scan_thread_quits_and_tears_down_on_failed(stub, monkeypatch):
    """A failed full scan also quits the thread and re-enables the window."""
    monkeypatch.setattr(scanner_module, "probe_duration", lambda path: 120.0)
    def raises(cmd, should_cancel=None):
        raise ExportCancelled()

    monkeypatch.setattr(scanner_module, "_run_ffmpeg", raises)

    stub._start_scan("/source.mp4", 0.5, 0.10)

    assert stub._scan_thread is None
    assert stub._scan_worker is None
    assert any("cancel" in w[1].lower() for w in _FakeMessageBox.warnings)


def test_both_scans_cannot_run_concurrently(stub, monkeypatch):
    """A second _start_test_scan while one is active is a no-op (guard)."""
    fake_stderr = "[blackdetect @ 0x1] black_start:1.000000 black_end:2.000000\n"
    monkeypatch.setattr(scanner_module, "_run_ffmpeg",
                        lambda cmd, should_cancel=None: (0, fake_stderr))

    # Manually set the guard to simulate an active thread.
    stub._test_scan_thread = FakeThread()

    stub._start_test_scan("/clip.mp4", 0.5, 0.10)

    assert stub._test_scan_thread is not None, "the existing thread was not replaced"


# ---------------------------------------------------------------------------
# closeEvent guard tests
# ---------------------------------------------------------------------------

def test_close_event_refuses_while_test_scan_active(qapp, monkeypatch):
    """closeEvent ignores the close while a test-scan thread is running."""
    monkeypatch.setattr(scanner_module, "QMessageBox", _FakeMessageBox)
    _FakeMessageBox.answers = [_FakeMessageBox.No]
    stub = ScannerStub()
    stub._test_scan_thread = object()  # truthy => a thread is active

    event = QCloseEvent()
    stub.closeEvent(event)

    assert stub.bridge.shutdowns == 0, "mpv was not shut down during active scan"
    assert not event.isAccepted(), "close was refused"


def test_close_event_refuses_while_scan_active(qapp, monkeypatch):
    """closeEvent ignores the close while a full-source scan is running."""
    monkeypatch.setattr(scanner_module, "QMessageBox", _FakeMessageBox)
    _FakeMessageBox.answers = [_FakeMessageBox.No]
    stub = ScannerStub()
    stub._scan_thread = object()

    event = QCloseEvent()
    stub.closeEvent(event)

    assert stub.bridge.shutdowns == 0, "mpv was not shut down during active scan"
    assert not event.isAccepted()


def test_close_event_proceeds_when_no_scan_active(qapp, monkeypatch):
    """With no active threads, closeEvent shuts down mpv and accepts."""
    monkeypatch.setattr(scanner_module, "QMessageBox", _FakeMessageBox)
    stub = ScannerStub()

    event = QCloseEvent()
    stub.closeEvent(event)

    assert stub.bridge.shutdowns == 1, "mpv was shut down"
    assert event.isAccepted(), "close was accepted"


def test_close_event_yes_cancels_both_scans(qapp, monkeypatch):
    """Clicking Yes in closeEvent cancels both active scans and sets the flag."""
    monkeypatch.setattr(scanner_module, "QMessageBox", _FakeMessageBox)
    _FakeMessageBox.answers = [_FakeMessageBox.Yes]
    stub = ScannerStub()
    stub._scan_thread = object()
    stub._test_scan_thread = object()

    event = type("E", (), {"ignore": lambda self: None})()
    stub.closeEvent(event)

    assert stub._close_after_worker is True
    assert _FakeMessageBox.questions[0][0] == "Scan in progress"


def test_on_scan_stopped_closes_when_no_other_thread_active(qapp, monkeypatch):
    """_on_scan_stopped closes if _close_after_worker is set and no test scan."""
    monkeypatch.setattr(scanner_module, "QThread", FakeThread)
    _FakeMessageBox.answers = []

    stub = ScannerStub()
    stub._close_after_worker = True
    stub._scan_thread = None
    stub._scan_dialog = None
    stub._scan_worker = None
    stub._test_scan_thread = None

    closed = []
    monkeypatch.setattr(stub, "close", lambda: closed.append(True))

    stub._on_scan_stopped()

    assert closed, "window was closed via _close_after_worker"
    assert stub._close_after_worker is False


def test_on_scan_stopped_waits_for_test_scan(qapp, monkeypatch):
    """_on_scan_stopped does not close while a test scan is still active."""
    monkeypatch.setattr(scanner_module, "QThread", FakeThread)
    _FakeMessageBox.answers = []

    stub = ScannerStub()
    stub._close_after_worker = True
    stub._test_scan_thread = object()
    stub._scan_thread = None
    stub._scan_dialog = None
    stub._scan_worker = None

    closed = []
    monkeypatch.setattr(stub, "close", lambda: closed.append(True))

    stub._on_scan_stopped()

    assert not closed, "did not close while test scan was still active"
    assert stub.bridge.shutdowns == 0


def test_on_test_scan_stopped_closes_when_no_other_thread_active(qapp, monkeypatch):
    """_on_test_scan_stopped closes if _close_after_worker is set and no scan."""
    monkeypatch.setattr(scanner_module, "QThread", FakeThread)
    _FakeMessageBox.answers = []

    stub = ScannerStub()
    stub._close_after_worker = True
    stub._test_scan_thread = None
    stub._test_scan_dialog = None
    stub._test_scan_worker = None
    stub._scan_thread = None

    closed = []
    monkeypatch.setattr(stub, "close", lambda: closed.append(True))

    stub._on_test_scan_stopped()

    assert closed, "window was closed via _close_after_worker"


def test_on_test_scan_stopped_waits_for_scan(qapp, monkeypatch):
    """_on_test_scan_stopped does not close while a full scan is still active."""
    monkeypatch.setattr(scanner_module, "QThread", FakeThread)
    _FakeMessageBox.answers = []

    stub = ScannerStub()
    stub._close_after_worker = True
    stub._test_scan_thread = None
    stub._test_scan_dialog = None
    stub._test_scan_worker = None
    stub._scan_thread = object()

    closed = []
    monkeypatch.setattr(stub, "close", lambda: closed.append(True))

    stub._on_test_scan_stopped()

    assert not closed, "did not close while full scan was still active"


# ---------------------------------------------------------------------------
# Real-thread responsiveness tests
# ---------------------------------------------------------------------------

def test_pre_scan_worker_not_blocking_gui(tmp_path, monkeypatch, qapp):
    """The bug this change fixes: pre-scan ran synchronously (LoadingSplash),
    blocking the event loop — and mpv with it — while it ran.

    A real QThread proves the worker is off the GUI thread: a queued slot on
    the GUI thread is delivered while the worker is still running.
    """
    source = tmp_path / "source.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"video")

    started = threading.Event()
    release = threading.Event()

    def slow_clip_to_temp(*a, **k):
        started.set()
        release.wait(10)
        return str(tmp_path / "preview.mp4")

    monkeypatch.setattr(scanner_module, "clip_to_temp", slow_clip_to_temp)
    monkeypatch.setattr(scanner_module, "scan_keyframes", lambda p: [])

    worker = ScannerPreScanWorker(str(source))
    thread = QThread()
    worker.moveToThread(thread)

    class _Probe(QObject):
        ticked = Signal()

        def __init__(self):
            super().__init__()
            self.ticked.connect(self._deliver, Qt.QueuedConnection)

        def _deliver(self):
            _Probe.served.set()

    _Probe.served = threading.Event()
    probe = _Probe()

    app = qapp
    probe.ticked.emit()
    app.processEvents()
    _Probe.served.clear()

    thread.started.connect(worker.run)
    thread.start()
    try:
        assert started.wait(10), "the worker never started"
        assert thread.isRunning(), "the worker is not on its own thread"
        assert not _Probe.served.is_set()

        probe.ticked.emit()
        for _ in range(200):
            app.processEvents()
            if _Probe.served.is_set():
                break
            time.sleep(0.01)

        assert not release.is_set(), "the worker was not allowed to finish"
        assert _Probe.served.is_set(), (
            "a queued slot could not be delivered while the pre-scan ran — "
            "the GUI thread was blocked"
        )
    finally:
        release.set()
        thread.quit()
        assert thread.wait(10000)


def test_test_scan_worker_not_blocking_gui(monkeypatch, qapp):
    """TestScanWorker runs blackdetect on its own thread, not the GUI thread."""
    started = threading.Event()
    release = threading.Event()

    def slow_run_ffmpeg(*a, **k):
        started.set()
        release.wait(10)
        return (0, "[blackdetect @ 0x1] black_start:1.000000 black_end:2.000000\n")

    monkeypatch.setattr(scanner_module, "get_binary_path",
                        lambda name: "ffmpeg")
    monkeypatch.setattr(scanner_module, "_run_ffmpeg", slow_run_ffmpeg)

    worker = TestScanWorker("/clip.mp4", min_sec=0.5, pix_th=0.10)
    thread = QThread()
    worker.moveToThread(thread)

    class _Probe(QObject):
        ticked = Signal()

        def __init__(self):
            super().__init__()
            self.ticked.connect(self._deliver, Qt.QueuedConnection)

        def _deliver(self):
            _Probe.served.set()

    _Probe.served = threading.Event()
    probe = _Probe()

    app = qapp
    probe.ticked.emit()
    app.processEvents()
    _Probe.served.clear()

    thread.started.connect(worker.run)
    thread.start()
    try:
        assert started.wait(10), "the worker never started"
        assert thread.isRunning(), "the worker is not on its own thread"

        probe.ticked.emit()
        for _ in range(200):
            app.processEvents()
            if _Probe.served.is_set():
                break
            time.sleep(0.01)

        assert not release.is_set()
        assert _Probe.served.is_set(), (
            "a queued slot could not be delivered while the test scan ran — "
            "the GUI thread was blocked"
        )
    finally:
        release.set()
        thread.quit()
        assert thread.wait(10000)


# ---------------------------------------------------------------------------
# Constructor signature test
# ---------------------------------------------------------------------------

def test_scanner_window_init_takes_clip_and_keyframes():
    """ScannerWindow.__init__ accepts clip_path and keyframes from create()."""
    params = list(inspect.signature(ScannerWindow.__init__).parameters)
    assert params[1] == "source_path"
    assert params[2] == "clip_path"
    assert params[3] == "keyframes"
