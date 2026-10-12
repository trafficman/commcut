"""Tests for the editor's pre-scan worker thread.

The editor's pre-work — the ffprobe keyframe scan, and, when the
source has no ``.cmct`` sidecar yet, a duration probe for the
placeholder model — used to run on the GUI thread behind
``LoadingSplash``. A blocked GUI thread cannot paint the splash that
covers the work, which on X11 left the splash a black rectangle for
the length of the scan (a top-level window's first paint is not
guaranteed within one ``processEvents``). ``PreScanWorker`` now runs
it on a ``QThread`` while a local event loop keeps the splash
repainting — the same arrangement the scanner's pre-scan uses.

The worker is a plain QObject and is tested directly; ``_prescan``
is driven through a real QThread and a real event loop, so the
threading itself is what the tests exercise.
"""

import threading
import time

import pytest
from PySide6.QtCore import QObject, Qt, QThread, Signal

import editor.editor as editor_module
from editor.editor import PreScanWorker
from editor_stub import ensure_qapp
from shared.segments import SegmentModel


@pytest.fixture
def qapp():
    return ensure_qapp()


# ---------------------------------------------------------------------------
# PreScanWorker unit tests
# ---------------------------------------------------------------------------

def test_prescan_worker_emits_keyframes_and_writes_sidecar(
        tmp_path, monkeypatch, qapp):
    """scan_keyframes runs, and a missing sidecar is written from a
    probed duration."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")

    monkeypatch.setattr(editor_module, "scan_keyframes",
                        lambda p: [0.0, 1.2, 3.4])
    monkeypatch.setattr(editor_module, "probe_duration", lambda p: 90.0)

    worker = PreScanWorker(str(source))
    received = []
    worker.finished.connect(lambda keyframes: received.append(keyframes))

    worker.run()

    assert received == [[0.0, 1.2, 3.4]]
    assert worker.keyframes == [0.0, 1.2, 3.4]
    assert worker.error is None
    sidecar = source.with_suffix(".cmct")
    assert sidecar.exists()
    model = SegmentModel.load(str(sidecar))
    assert model.duration == 90.0
    assert model.segment_count() == 1


def test_prescan_worker_skips_the_probe_when_a_sidecar_exists(
        tmp_path, monkeypatch, qapp):
    """An existing .cmct is left alone: no duration probe, no rewrite."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    sidecar = source.with_suffix(".cmct")
    SegmentModel.placeholder("source.mp4", 111.0).save(str(sidecar))

    probed = []
    monkeypatch.setattr(editor_module, "scan_keyframes", lambda p: [0.0])
    monkeypatch.setattr(
        editor_module, "probe_duration",
        lambda p: probed.append(p) or 120.0)

    worker = PreScanWorker(str(source))
    worker.run()

    assert probed == [], "probe_duration must not run when a sidecar exists"
    assert SegmentModel.load(str(sidecar)).duration == 111.0


def test_prescan_worker_fails_on_exception(monkeypatch, qapp):
    """A missing binary surfaces as failed(), not a dead thread."""
    def boom(path):
        raise FileNotFoundError("no ffprobe")

    monkeypatch.setattr(editor_module, "scan_keyframes", boom)

    worker = PreScanWorker("/source.mp4")
    received = []
    worker.failed.connect(lambda reason: received.append(reason))

    worker.run()

    assert len(received) == 1
    assert "FileNotFoundError" in received[0]
    assert worker.error is not None


# ---------------------------------------------------------------------------
# _prescan integration tests
# ---------------------------------------------------------------------------

def test_prescan_returns_keyframes(tmp_path, monkeypatch, qapp):
    """_prescan runs the worker on a thread and returns the keyframes.

    The local event loop only returns once the worker's signal has
    been delivered, so a run that blocked the GUI thread would never
    get here.
    """
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")

    monkeypatch.setattr(editor_module, "scan_keyframes", lambda p: [0.0, 2.0])
    monkeypatch.setattr(editor_module, "probe_duration", lambda p: 60.0)

    keyframes = editor_module._prescan(str(source))

    assert keyframes == [0.0, 2.0]


def test_prescan_raises_when_the_worker_fails(monkeypatch, qapp):
    """A failed pre-scan raises, so the shell reports the window."""
    def boom(path):
        raise FileNotFoundError("no ffprobe")

    monkeypatch.setattr(editor_module, "scan_keyframes", boom)

    with pytest.raises(RuntimeError, match="FileNotFoundError"):
        editor_module._prescan("/source.mp4")


# ---------------------------------------------------------------------------
# Real-thread responsiveness test
# ---------------------------------------------------------------------------

def test_prescan_worker_not_blocking_gui(tmp_path, monkeypatch, qapp):
    """The bug this change fixes: the pre-scan ran on the GUI thread
    behind LoadingSplash, blocking the event loop — and the splash's
    first paint with it — while ffprobe ran.

    A real QThread proves the worker is off the GUI thread: a queued
    slot on the GUI thread is delivered while the worker is still running.
    """
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")

    started = threading.Event()
    release = threading.Event()

    def slow_scan_keyframes(path):
        started.set()
        release.wait(10)
        return [0.0, 1.2]

    monkeypatch.setattr(editor_module, "scan_keyframes", slow_scan_keyframes)
    monkeypatch.setattr(editor_module, "probe_duration", lambda p: 120.0)

    worker = PreScanWorker(str(source))
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
