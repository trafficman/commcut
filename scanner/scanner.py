import os
import subprocess
import sys
import tempfile
import time

# Make the project root importable so 'shared' resolves.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from shared.environment import (
    get_binary_path, no_console_kwargs, resource_path, setup_environment,
)
SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

from shared.diagnostics import log, log_exception
from shared.ffmpeg import clip_to_temp, ExportCancelled
from shared.mpv import MpvBridge, create_mpv_player, scan_keyframes
from shared.segments import (
    sidecar_path, probe_duration,
    SegmentModel,
)
from scanner.marker_timeline import MarkerTimelineWidget
from shared.session import OpenInstead, shell
from shared.sources import validate_source_video
from shared.splash import show_splash
from shared.ui_loader import UiLoader, adopt_title

from PySide6.QtWidgets import QMainWindow, QStyle, QProgressDialog, QMessageBox
from PySide6.QtCore import QFile, QObject, QThread, Signal, Slot

# Seconds of test footage the scanner works on (stream-copied to temp/).
CLIP_DURATION = 120


def _clear_temp_clips():
    """Remove stale .mp4 preview clips from temp/ so each scanner run starts
    fresh and clips don't accumulate across runs.
    """
    temp_dir = os.path.join(PROJECT_ROOT, "temp")
    if not os.path.isdir(temp_dir):
        return
    for name in os.listdir(temp_dir):
        if name.lower().endswith(".mp4"):
            try:
                os.remove(os.path.join(temp_dir, name))
            except OSError:
                pass


def _editor_to_launch(source_path):
    """True if a .cmct sidecar already exists for the source, else False.

    The scanner must never overwrite an existing .cmct, so when one is present
    the caller goes straight to the editor instead of running the scanner. The
    sidecar is still the hand-off medium: the scanner writes it and the editor
    reads it, so both windows agree on the model even though nothing is passed
    in memory between them.
    """
    return os.path.exists(sidecar_path(source_path))


def _model_from_midpoints(midpoints, duration, source_name):
    """Build a SegmentModel whose segment starts are the midpoint boundaries.

    Each midpoint (center of a black run) becomes a transition point, so the
    model tiles contiguously from 0 to duration. None of the segments are
    marked ignored — that curation is left to the editor (the user can
    ignore/merge/adjust after the scan).
    """
    starts = sorted({0.0, *(m for m in midpoints if 0.0 < m < duration)})
    segments = [{"start": s, "ignored": False, "tags": {}} for s in starts]
    return SegmentModel(source=source_name, duration=duration, segments=segments)


class FinishedScanWorker(QObject):
    """Runs blackdetect on the full source video off the GUI thread.

    Emits ``scanned(midpoints)`` with the list of boundary timestamps once the
    full-source ffmpeg pass is done, or ``failed(reason)`` if the scan could not
    run. The window disables its controls while this runs and re-enables them in
    either case — the same pattern ``ExportWorker`` uses for the main export.
    """
    scanned = Signal(list)
    failed = Signal(str)

    def __init__(self, source, min_sec, pix_th, duration, parent=None):
        super().__init__(parent)
        self.source = source
        self.min_sec = min_sec
        self.pix_th = pix_th
        self.duration = duration
        self._cancelled = False

    @Slot()
    def run(self):
        cmd = [
            get_binary_path("ffmpeg"), "-y", "-v", "info",
            "-i", self.source,
            "-vf", f"blackdetect=d={self.min_sec:.3f}:pix_th={self.pix_th:.4f}",
            "-an", "-f", "null", "-",
        ]
        try:
            stderr = self._run_ffmpeg_with_cancel(cmd)
        except ExportCancelled:
            self.failed.emit("Scan was cancelled.")
            return
        except Exception as error:
            log_exception("Finished scan blackdetect failed", error)
            self.failed.emit(
                f"The scan could not run: {type(error).__name__}: {error}"
            )
            return

        midpoints = [
            (t1 + t2) / 2.0
            for t1, t2 in ScannerWindow._parse_blackdetect_runs(stderr)
        ]
        self.scanned.emit(midpoints)

    def _run_ffmpeg_with_cancel(self, command):
        """Run ffmpeg, draining stderr while polling for cancellation.

        Mirrors the cancel pattern in ``shared.ffmpeg._run_ffmpeg``: stderr goes
        to a temp file (nothing drains it while the encode runs, and a full pipe
        buffer stalls ffmpeg to death), and ``cancel`` terminates the child
        immediately on Windows / sends SIGTERM on macOS+Linux.
        """
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=errors,
                **no_console_kwargs(),
            )
            try:
                while process.poll() is None:
                    if self._cancelled:
                        process.terminate()
                        process.wait()
                        raise ExportCancelled()
                    time.sleep(0.05)
                process.wait()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            errors.seek(0)
            return errors.read().decode("utf-8", "replace")

    @Slot()
    def cancel(self):
        """Terminate the running ffmpeg process.

        Called when the user cancels the progress dialog. The ``Popen``
        reference is checked because the process may have already exited.
        """
        self._cancelled = True


class ScannerWindow(QMainWindow):
    """The Segment Scanner window.

    Loads a preview clip of the chosen source video and hooks up the transport
    controls (play/pause, frame ±, keyframe ±) plus the two marker timelines.
    The keyframe list is populated by scan_keyframes in run() before the window
    is constructed, then pushed onto the bridge via set_keyframes.

    Place Boundary, Undo, Test Scan, and Finished are wired: Place Boundary
    stamps the playhead into the User Marked timeline; Undo removes the last
    user-placed boundary; Test Scan runs blackdetect and stamps midpoint
    markers into the Scanner Preview timeline; Finished runs blackdetect on
    the full source, writes a .cmct of midpoint boundaries, and opens the
    Video Editor on that same source. Remaining: Export / smart cut.
    """

    def __init__(self, source_path):
        super().__init__()

        # The compilation video this run works on, chosen in the main menu's file
        # dialog. Held as an attribute because Finished scans this file and hands
        # the same path to the editor; nothing re-derives it.
        self.source_path = source_path

        # Load the .ui file
        ui_file = QFile(resource_path("scanner", "scannerwindow.ui"))
        if not ui_file.open(QFile.ReadOnly):
            # Raised rather than printed and exited: in a windowed packaged
            # build a print goes nowhere, and this runs before the excepthook
            # in run() would be worth relying on for a clean message.
            raise FileNotFoundError(
                f"Could not open the scanner UI file: {ui_file.fileName()}")

        loader = UiLoader()
        loader.register_widget(MarkerTimelineWidget)
        self.ui = loader.load(ui_file, self)
        adopt_title(self, self.ui)
        self.setCentralWidget(self.ui)

        # Create the embedded mpv player in the video frame
        video_frame = self.ui.videoContainer
        self.player = create_mpv_player(video_frame)
        self.bridge = MpvBridge(self.player, parent=self)

        # Wire the play/pause button
        self.ui.playPause.clicked.connect(self.on_play_pause)
        self.bridge.pauseChanged.connect(self.on_pause_changed)

        # Wire frame and keyframe skip buttons
        self.ui.forwardFrame.clicked.connect(lambda: self.bridge.step_frames(1))
        self.ui.backwardFrame.clicked.connect(lambda: self.bridge.step_frames(-1))
        self.ui.forwardKeyFrame.clicked.connect(self.bridge.next_keyframe)
        self.ui.backwardKeyFrame.clicked.connect(self.bridge.prev_keyframe)

        # Track the playhead position so Place Boundary can stamp it.
        self.current_position = 0.0
        self.bridge.positionChanged.connect(self._on_position_changed)

        # Wire both marker timelines to the bridge (playhead + duration + seek)
        for timeline in (self.ui.timelineWidget1, self.ui.timelineWidget2):
            self.bridge.positionChanged.connect(timeline.set_position)
            self.bridge.durationChanged.connect(timeline.set_duration)
            timeline.seekRequested.connect(self.bridge.seek_exact)

        # Slider value labels: live updates as the user drags the slider.
        # "Minimum Black Frames" is the count (0-40); "Black Levels" is a
        # percentage (0-100).
        self.ui.valueMinimumBlackFrames.setText(
            f"{self.ui.horizontalSlider.value()} frames"
        )
        self.ui.horizontalSlider.valueChanged.connect(
            lambda v: self.ui.valueMinimumBlackFrames.setText(f"{v} frames")
        )
        self.ui.valueBlackLevels.setText(f"{self.ui.horizontalSlider_2.value()}%")
        self.ui.horizontalSlider_2.valueChanged.connect(
            lambda v: self.ui.valueBlackLevels.setText(f"{v}%")
        )

        self._sync_button(paused=True)

        # Wire the Place Boundary button
        self.ui.boundaryButton.clicked.connect(self.on_place_boundary)

        # Wire the Undo button (removes the last user-placed boundary)
        self.ui.undoButton.clicked.connect(self.on_undo)

        # Wire the Test Scan button
        self.ui.scanButton.clicked.connect(self.on_test_scan)

        # Wire the Finished button (full-source scan -> .cmct -> editor)
        self.ui.finishedButton.clicked.connect(self.on_finished)

        # Load the preview clip. clip_to_temp names the file after the source,
        # so two different videos never share one preview.
        self.clip_path = clip_to_temp(
            self.source_path,
            CLIP_DURATION,
            output_dir=os.path.join(PROJECT_ROOT, "temp"),
        )
        self.bridge.load_file(self.clip_path)

        # --- full-source scan state ---
        # None when no Finished scan is running; set when one starts so the
        # window can refuse to close mid-scan and tear the thread down safely.
        self._scan_thread = None
        self._scan_worker = None
        self._scan_dialog = None
        self._close_after_scan = False

    def _on_position_changed(self, position):
        self.current_position = position

    def on_place_boundary(self):
        """Stamp the current playhead as a boundary in the User Marked timeline.

        Boundaries live only in memory (the marker timeline's list); nothing
        is written to a .cmct file.
        """
        self.ui.timelineWidget2.add_marker(self.current_position)

    def on_undo(self):
        """Remove the last user-placed boundary from the User Marked timeline.

        Assumes the user worked left-to-right, so the last (rightmost)
        marker in the list is the most recent placement. No-op if the
        timeline has no user markers.
        """
        self.ui.timelineWidget2.pop_marker()

    def on_test_scan(self):
        """Run ffmpeg's ``blackdetect`` on the 2-min preview and stamp one
        midpoint boundary per black run into the upper **Scanner Preview**
        timeline (timelineWidget1).

        Slider mapping:
          - Minimum Black Frames -> ``d = frames / fps`` (minimum run length)
          - Black Levels          -> ``pix_th = level / 100``
        Each black run with a real (non-N/A) end yields a single midpoint
        marker at ``(black_start + black_end) / 2``. Runs open at EOF
        (black_end:N/A) are skipped. Previous markers are cleared first so
        re-scanning reflects the current slider settings.
        """
        self.ui.timelineWidget1.set_markers([])

        fps = self.bridge.video_fps
        if not fps:
            return

        frames = self.ui.horizontalSlider.value()
        level = self.ui.horizontalSlider_2.value()
        min_sec = frames / fps if frames > 0 else 0.0
        pix_th = level / 100.0

        cmd = [
            get_binary_path("ffmpeg"), "-y", "-v", "info",
            "-i", self.clip_path,
            "-vf", f"blackdetect=d={min_sec:.3f}:pix_th={pix_th:.4f}",
            "-an", "-f", "null", "-",
        ]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, **no_console_kwargs())
        midpoints = [(t1 + t2) / 2.0 for t1, t2 in self._parse_blackdetect_runs(proc.stderr)]
        self.ui.timelineWidget1.set_markers(midpoints)

    @staticmethod
    def _parse_blackdetect_runs(stderr):
        """Parse blackdetect stderr into a sorted list of (t1, t2) black runs.

        Runs with black_end:N/A (open at EOF) are skipped. Shared by Test
        Scan (midpoints for the preview timeline) and Finished (midpoints
        written to the .cmct).
        """
        runs = []
        for line in stderr.splitlines():
            if "[blackdetect" not in line or "black_start:" not in line:
                continue
            kv = {}
            for tok in line.split():
                if ":" not in tok:
                    continue
                key, value = tok.split(":", 1)
                kv[key] = value
            end = kv.get("black_end")
            if end in (None, "N/A"):
                continue
            try:
                t1 = float(kv["black_start"])
                t2 = float(end)
            except (KeyError, ValueError):
                continue
            runs.append((t1, t2))
        runs.sort()
        return runs

    def on_finished(self):
        """Run blackdetect on the FULL source video off the GUI thread, then
        write the midpoint boundaries to a .cmct sidecar and open the editor.

        Each black run [t1, t2] contributes one boundary at its midpoint
        ``(t1 + t2) / 2`` — the same rule Test Scan uses for the preview
        timeline. The midpoints become the .cmct segment starts. The scanner
        never overwrites an existing .cmct: ``__main__`` already routes to the
        editor when one is present, so this is only reached when no sidecar
        exists yet.
        """
        source = self.source_path

        duration = probe_duration(source)
        if duration is None:
            log("Finished: could not probe source duration.")
            return

        fps = self.bridge.video_fps
        if not fps:
            log("Finished: frame rate unavailable; seek within the preview first.")
            return

        frames = self.ui.horizontalSlider.value()
        level = self.ui.horizontalSlider_2.value()
        min_sec = frames / fps if frames > 0 else 0.0
        pix_th = level / 100.0

        self._start_scan(source, min_sec, pix_th, duration)

    def _start_scan(self, source, min_sec, pix_th, duration):
        """Launch the full-source blackdetect on a worker thread.

        The window is disabled for the duration so the user cannot trigger a
        second run, close the window, or issue transport commands while ffmpeg
        is decoding the entire compilation. A modal progress dialog makes the
        freeze explicit and offers a cancel that terminates the scan cleanly.
        """
        if self._scan_thread is not None:
            return

        worker = FinishedScanWorker(source, min_sec, pix_th, duration)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.scanned.connect(self._on_scan_complete)
        worker.failed.connect(self._on_scan_failed)
        # The worker is a one-shot: it runs once and the thread exits.
        worker.scanned.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._on_scan_stopped)
        self._scan_worker = worker
        self._scan_thread = thread

        self.setEnabled(False)
        dialog = QProgressDialog("Scanning full source video…", "Cancel", 0, 0, self)
        dialog.setWindowTitle("Finished scan")
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.canceled.connect(self._on_scan_cancel_requested)
        dialog.show()
        self._scan_dialog = dialog

        thread.start()

    def _on_scan_cancel_requested(self):
        """Cancel requested: stop the ffmpeg process inside the worker thread.

        The worker checks its ``_cancelled`` flag every 50 ms while polling the
        subprocess; setting the flag and signalling ``cancel`` terminates ffmpeg
        on the next poll, which unblocks ``run()`` so the thread can finish.
        """
        if self._scan_worker is not None:
            self._scan_worker.cancel()
        dialog = self._scan_dialog
        if dialog is not None:
            dialog.setCancelButton(None)
            dialog.setLabelText("Cancelling…")

    def _on_scan_complete(self, midpoints):
        """Worker finished: write the .cmct, then hand off to the editor.

        Re-enables the window first so a refusal from the sidecar write or the
        editor hand-off can prompt the user without the window looking dead.
        """
        source = self.source_path
        model = _model_from_midpoints(midpoints, self._scan_worker.duration, os.path.basename(source))
        sidecar = sidecar_path(source)
        model.save(sidecar)
        log(f"Finished: wrote {model.segment_count()} segments to {sidecar}")

        self.setEnabled(True)
        shell().open_safely('editor', source=source)
        self.close()

    def _on_scan_failed(self, reason):
        """Worker could not run: log and report, then re-enable the window."""
        log(f"Finished: scan failed — {reason}")
        QMessageBox.warning(self, "Scan failed", reason)

    def _on_scan_stopped(self):
        """Tear the thread down after it has actually stopped."""
        dialog = self._scan_dialog
        if dialog is not None:
            dialog.reset()
            dialog.deleteLater()
        thread = self._scan_thread
        if thread is not None:
            thread.deleteLater()
        worker = self._scan_worker
        if worker is not None:
            worker.deleteLater()
        self._scan_dialog = None
        self._scan_thread = None
        self._scan_worker = None

        if self._close_after_scan:
            self._close_after_scan = False
            self.setEnabled(True)
            self.bridge.shutdown()
            self.close()
            return
        self.setEnabled(True)

    def on_play_pause(self):
        self.bridge.toggle_play()

    def closeEvent(self, event):
        """Shut the mpv player down before this window -- and its native video
        handle -- is destroyed.

        Refuses to close while a full-source scan is running, because a
        QThread destroyed while still executing aborts the process. The user is
        asked whether to cancel the scan and close; if so, the flag
        ``_close_after_scan`` is set so ``_on_scan_stopped`` closes the window
        once the worker has actually quit, and this close is ignored for now.

        The same mpv teardown rule the editor follows, for the same reason: the
        player is embedded into the video frame's HWND, so a player still running
        when that handle dies leaves libmpv rendering into a window that no
        longer exists, and joining its threads blocks the GUI thread. See
        ``MpvBridge.shutdown``.
        """
        if self._scan_thread is not None:
            answer = QMessageBox.question(
                self,
                "Scan in progress",
                "A full-source scan is in progress.\n\nCancel it and close?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer == QMessageBox.Yes:
                self._close_after_scan = True
                self._on_scan_cancel_requested()
                # The close happens again from _on_scan_stopped, once the thread
                # has finished — that second pass is the one that shuts the
                # player down and accepts.
            event.ignore()
            return

        self.bridge.shutdown()
        super().closeEvent(event)

    def on_pause_changed(self, paused):
        self._sync_button(paused)

    def _sync_button(self, paused):
        """Mirror mpv's pause state onto the play/pause button label/icon."""
        btn = self.ui.playPause
        style = btn.style()
        if paused:
            btn.setText("Play")
            btn.setIcon(style.standardIcon(QStyle.SP_MediaPlay))
        else:
            btn.setText("Pause")
            btn.setIcon(style.standardIcon(QStyle.SP_MediaPause))


def create(app, source):
    """Build the scanner window for `source`. Returns the window, unscaled.

    `app` is the process's QApplication, owned by main.py — this window does not
    make one and does not run an event loop, because it shares the loop with the
    menu and the editor. The shell shows the window.

    `source` is the video the main menu's file dialog returned, and it is
    **required**: the menu always asks, so there is no fallback path left to
    keep. `validate_source_video` is what turns it into something this window may
    open — it refuses a path that is not a video, and one whose folder cannot be
    written, because the `.cmct` sidecar goes beside the video.

    When that video already has a `.cmct` sidecar there is nothing to scan, so no
    scanner window is built: this raises :class:`~shared.session.OpenInstead` and
    the shell opens the editor on that same source instead. The rule is this
    module's, because only it knows that re-scanning would overwrite the existing
    model; the routing is the shell's, because this window has nothing to show.
    """
    source_path = validate_source_video(source)
    log(f"scanner working on {source_path}")

    # Never overwrite an existing .cmct: if one already exists for the source,
    # there is nothing to scan and nothing this window could show.
    if _editor_to_launch(source_path):
        log(f"{source_path} already has a sidecar; opening the editor")
        raise OpenInstead('editor', source=source_path)

    # Start fresh: drop any leftover preview clips from prior runs.
    _clear_temp_clips()

    media_path = clip_to_temp(
        source_path,
        CLIP_DURATION,
        output_dir=os.path.join(PROJECT_ROOT, "temp"),
    )
    if media_path is None:
        raise RuntimeError(
            f"Could not build a {CLIP_DURATION}s preview of {source_path}. "
            f"See the log for the ffmpeg error."
        )

    # Splash while ffprobe scans keyframes. The scan runs synchronously on the
    # GUI thread (it's typically fast); the splash gives the user something to
    # look at, and it also covers the fact that the menu is now a live window in
    # the same process and would otherwise look frozen while this runs.
    #
    # The splash is closed BEFORE the mpv player is constructed. The hazard that
    # rule works around was never reproduced (experiments/mpv_foreground/ ran
    # this exact case 20 times with no hang), but closing a splash is three
    # lines and costs nothing when it turns out to be unnecessary, so it stays
    # until the packaged build has run on untested hardware.
    splash = show_splash(app, f"Loading {os.path.basename(media_path)}…")

    keyframes = scan_keyframes(media_path)

    splash.close()
    window = ScannerWindow(source_path)
    window.bridge.set_keyframes(keyframes)
    window.resize(1024, 768)
    return window

