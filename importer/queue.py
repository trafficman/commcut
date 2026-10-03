"""The Library Mesh Tag Editor: one clip at a time, until the library is done.

Applies to: `importer/queue.py`, `importer/queuewindow.ui`, `importer/rules.py`,
`shared/mesh.py`, `shared/importing.py`, `shared/tag_form.py`.

The Mesh Wizard has already said what every **folder name** in `import/` means.
What is left is what it cannot say: a **title**, which is a *region* of a file
name rather than a whole token, and so the one tag per clip that has to come from
a person. This window is that per-clip pass, and it is the whole of untagged
import — the "auto import and fix up the rest" options are retired, because no
clip can be finished without a title.

Three decisions shape it:

- **A settled clip's `.cnfo` is written to `import/` immediately.** The tagged
  import then takes over unchanged — `build_catalog`, `candidates_from_catalog`,
  `plan_import`, `execute_import` — so the untagged path converges on the tagged
  one rather than forking it. It also means an eight-hundred-clip session has a
  save point per clip. Resume asks `missing_required_tags(record)`, *not* "is
  there a record", because a half-tagged record exists and is not finished.
- **The rules are learned from file names and can never set a title.** A rule is
  a standing instruction; a title is per clip. That distinction is the reason the
  title is the only thing still asked for here, and it is enforced in
  `MeshSession.learn_rule` rather than here.
- **"Skip - Delete" removes the file.** `import/` is a staging folder, not a
  library: anything in it is a copy from somewhere else or expendable. So there
  is a destructive option, labelled as one, confirmed with the file's name, kept
  off `Next`'s side of the button row, and never a default button.

`scan_keyframes` is deliberately not called: it is an ffprobe pass over every
frame, for a window that has no segments to mark and clips that are thirty
seconds long.
"""

import os
import sys
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.environment import resource_path, setup_environment

SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

from PySide6.QtCore import QFile, QObject, QThread, Signal, Slot
from PySide6.QtWidgets import QMainWindow, QMessageBox, QProgressDialog

from shared.catalog import build_catalog, sync_vocabulary
from shared.diagnostics import log, log_exception
from shared.exporting import (
    ExportSchemes,
    export_folder,
    load_export_schemes,
    missing_required_tags,
)
from shared.importing import (
    candidates_from_catalog,
    execute_import,
    find_videos,
    import_folder,
    plan_import,
)
from shared.mesh import MeshSession
from shared.mpv import MpvBridge, create_mpv_player
from shared.records import (
    RECORD_EXTENSION,
    ClipRecord,
    load_record,
    write_record,
)
from shared.segments import probe_duration
from shared.tag_form import TAG_FIELDS, TagForm, field_change_signal
from shared.ui_loader import UiLoader
from shared.vocabulary import get_vocabulary, record_use, vocabulary_path

from importer.rules import RulesDialog


# ---------------------------------------------------------------------------
# What the queue knows
# ---------------------------------------------------------------------------

def _record_path(video_path: str) -> str:
    """The record file beside `video_path`.

    **Not** `shared.segments.sidecar_path`. That is the editor's `.cmct` — the
    in-progress boundary model for a compilation, written next to the source
    video. An imported clip's record is the `.cnfo` the export planner publishes,
    which is what `build_catalog` looks for and what `find_videos` reports as
    `has_record`. Writing `.cmct` here would leave the folder in a state no other
    part of the app reads.
    """
    return os.path.splitext(video_path)[0] + RECORD_EXTENSION


class QueueClip:
    """One video waiting to be tagged."""

    def __init__(self, found):
        self.path = found.path
        self.relative_path = found.relative_path
        self.filename = os.path.basename(found.path)
        self.duration: float | None = None
        self.problem: str = ""

    @property
    def name(self) -> str:
        return self.filename

    def is_already_done(self) -> bool:
        """True when a record beside this video already holds every required tag.

        **The record existing is not the same as the clip being done.** A clip the
        user tagged but then abandoned has a record with three of four tags, and
        treating that as finished would strand the clip forever. So this asks the
        same question `plan_import` will ask: what is still missing?
        """
        path = _record_path(self.path)
        if not os.path.exists(path):
            return False
        try:
            record = load_record(path)
        except (OSError, ValueError):
            return False
        return not missing_required_tags(record.tag_dict)

    def existing_tags(self) -> dict[str, str]:
        """Whatever a partial record already holds, so reopening does not discard
        work. A record that cannot be read contributes nothing."""
        path = _record_path(self.path)
        if not os.path.exists(path):
            return {}
        try:
            return dict(load_record(path).tag_dict)
        except (OSError, ValueError):
            return {}

    def write_record(self, tags: dict[str, str], provenance: str) -> None:
        """Publish the clip's record beside its video.

        `provenance` is the imported file's own name, which is the one honest
        value a record's `<source>` can hold for a clip that was never cut from a
        compilation.
        """
        record = ClipRecord(
            source=provenance,
            segment_index=0,
            start=0.0,
            duration=float(self.duration or 0.0),
            tags=tuple(sorted(tags.items())),
        )
        write_record(_record_path(self.path), record)


class ProbeWorker(QObject):
    """Measures one clip's duration off the GUI thread.

    The record needs a real duration, and `plan_import` refuses a clip of no
    length, so a clip that will not measure cannot be recorded and therefore
    cannot be finished. One ffprobe per clip, which is the unavoidable cost of
    importing somebody else's library and the reason it happens on load rather
    than at the end.

    Signals are raised through the real methods rather than being reimplemented,
    so the broad `except` below is the shipped one.
    """

    measured = Signal(object, float, str)
    finished = Signal()

    def __init__(self, clip, cancel_event=None):
        super().__init__()
        self.clip = clip
        self.cancel_event = cancel_event or threading.Event()

    @Slot()
    def run(self):
        try:
            if self.cancel_event.is_set():
                self.measured.emit(self.clip, 0.0, "cancelled")
            else:
                duration = probe_duration(self.clip.path)
                if duration and duration > 0:
                    self.measured.emit(self.clip, float(duration), "")
                else:
                    self.measured.emit(
                        self.clip, 0.0,
                        "the video's length could not be read, so no record can "
                        "be written for it")
        except Exception as error:  # noqa: BLE001 - reported, never raised
            log_exception("probing a queued clip failed", error)
            self.measured.emit(self.clip, 0.0, f"{type(error).__name__}: {error}")
        self.finished.emit()


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------

class QueueWindow(QMainWindow):
    """Every clip in `import/`, one at a time, with the wizard's answers applied."""

    def __init__(self, session: MeshSession = None, root: str | None = None,
                 parent=None):
        super().__init__()
        loader = UiLoader()
        loader.register_widget(TagForm)
        self.ui = loader.load(
            QFile(resource_path("importer", "queuewindow.ui")), self)
        self.setCentralWidget(self.ui)

        self.root = root or import_folder()
        self.library_root = export_folder()
        self.session = session or self._build_session()
        self.clips: list[QueueClip] = []
        self.position = 0
        self.done = 0
        self.deleted: list[str] = []
        self.unprobeable: list[tuple[str, str]] = []

        self._thread: QThread | None = None
        self._worker: ProbeWorker | None = None
        self._bridge: MpvBridge | None = None
        self._cancel = threading.Event()

        self.ui.tagForm.set_locks_visible(False)
        self.ui.tagForm.init_vocabulary(vocabulary_path())
        self.ui.buttonNext.clicked.connect(self.on_next)
        self.ui.buttonSkipDelete.clicked.connect(self.on_skip_delete)
        self.ui.buttonAddTitle.clicked.connect(self.on_add_title)
        self.ui.buttonManageRules.clicked.connect(self.on_manage_rules)
        self.ui.buttonPlay.clicked.connect(self.on_play_pause)
        # `cursorPositionChanged` rather than a "selection changed" signal,
        # because QPlainTextEdit has none: a selection is a cursor range, and
        # moving either end moves the position. Together with `textChanged` it
        # covers drag-selecting, click-selecting, and the programmatic
        # `selectAll` a fresh clip is shown with.
        self.ui.textFileName.cursorPositionChanged.connect(
            self._refresh_add_title)
        self.ui.textFileName.textChanged.connect(self._refresh_add_title)
        self.ui.sliderPosition.sliderReleased.connect(self._on_seek)
        # Every field re-derives what Next can do. The form owns the outline and
        # the required rule; this only decides whether the button is live.
        for namespace in TAG_FIELDS:
            field_change_signal(self.ui.tagForm.field(namespace)).connect(
                self._refresh_next)
        self.ui.tagForm.refresh_required_fields()

        self._enumerate()
        self._load_current()

    # -- input ------------------------------------------------------------

    def _build_session(self) -> MeshSession:
        """Read the library and the import folder the way the Wizard did."""
        vocabulary = get_vocabulary(vocabulary_path())
        sync_vocabulary(self.library_root, vocabulary)
        return MeshSession(self.root, find_videos(self.root),
                           library=build_catalog(self.library_root),
                           vocabulary=vocabulary)

    def _enumerate(self):
        """One snapshot of the library, sorted, and the clips already finished
        taken out of it.

        Snapshot rather than re-scan, because "Skip - Delete" removes a file
        mid-queue and a list that re-scanned would shift underneath the user. A
        deleted clip stays in the list, marked done, and is not revisited.
        """
        pending = [QueueClip(found) for found in find_videos(self.root)]
        pending.sort(key=lambda clip: clip.relative_path.casefold())
        self.clips = [clip for clip in pending if not clip.is_already_done()]
        self.done = len(pending) - len(self.clips)
        self.position = 0

    # -- one clip ---------------------------------------------------------

    def _remaining(self) -> int:
        return max(0, len(self.clips) - self.position)

    def _load_current(self):
        if self.position >= len(self.clips):
            self._show_report()
            return

        clip = self.clips[self.position]
        self._stop_probe()
        self.ui.textFileName.setPlainText(clip.filename)
        # Deliberately *not* select-all. A fresh clip with the whole name already
        # selected invites a reflexive click on Add Title, which would take
        # "Toonami - 30 Sec - Cartoon.mkv" as the title -- almost never right, and
        # written to a record as though it were deliberate. The user selects the
        # part that is the title, which is the whole point of the control.
        cursor = self.ui.textFileName.textCursor()
        cursor.clearSelection()
        self.ui.textFileName.setTextCursor(cursor)

        self.ui.labelProgress.setText(
            f"{self.position + 1} of {len(self.clips)} left"
            + (f"  ·  {self.done} already done" if self.done else ""))

        self.ui.labelConflicts.setText("")
        resolved = self.session.tags_for_clip(clip.relative_path)
        form_tags = resolved.tags
        form_tags.update(clip.existing_tags())
        self.ui.tagForm.write_tags(form_tags)
        self.ui.tagForm.refresh_required_fields()
        self._describe_conflicts(resolved)
        self._open_video(clip.path)
        self._refresh_add_title()
        self._refresh_next()
        # Last, so nothing above can fail while a worker thread is already
        # running -- a `QThread` destroyed while running aborts the process, and a
        # constructor that raises leaves its window with nothing to close it.
        self._start_probe(clip)

    def _describe_conflicts(self, resolved):
        """Say which tag could not be filled, and why."""
        if resolved.resolved:
            self.ui.labelConflicts.setText("")
            return
        lines = ["These could not be filled in, because one folder claimed the "
                 "same tag twice. Type them yourself:"]
        lines.extend(f"  - {c.namespace}: '{c.existing_value}' or "
                     f"'{c.incoming_value}' (from '{c.existing_name}' and "
                     f"'{c.incoming_name}')" for c in resolved.conflicts)
        self.ui.labelConflicts.setText("\n".join(lines))

    # -- the video --------------------------------------------------------

    def _open_video(self, path):
        """Open the clip in mpv. The player is optional.

        The title comes out of the file **name**, not out of the picture, so a
        clip mpv cannot open is still a clip the user can tag -- and refusing to
        open the window at all would leave them with nothing to do. A player that
        fails to build is logged and the queue carries on with playback dead.

        This returns rather than raises for the same reason. An earlier version
        let a bad signal name escape, and `open_safely` then reported a window
        that could not be opened -- over a mistake that would not have stopped
        anyone tagging anything.
        """
        self._close_video()
        try:
            player = create_mpv_player(self.ui.videoContainer)
            self._bridge = MpvBridge(player)
            self._bridge.positionChanged.connect(self._on_position_changed)
            self._bridge.durationChanged.connect(self._on_duration_changed)
            self._bridge.pauseChanged.connect(self._on_paused_changed)
            self._bridge.load_file(path)
        except Exception as error:  # noqa: BLE001 - a player is not a document
            log_exception("the queue could not open a player", error)
            self._close_video()
            self.ui.labelPosition.setText("no preview")

    def _start_probe(self, clip):
        self._cancel = threading.Event()
        worker = ProbeWorker(clip, cancel_event=self._cancel)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        # `thread.quit` is what actually ends the thread. `started` runs
        # `run()` inside the thread's `exec()` loop, and a slot returning does
        # not leave that loop, so without this the thread spins forever and
        # `thread.finished` never arrives.
        worker.finished.connect(thread.quit)
        worker.measured.connect(self._on_measured)
        thread.finished.connect(self._on_probe_stopped)
        self._worker = worker
        self._thread = thread
        thread.start()

    def _stop_probe(self):
        if self._thread is not None:
            self._cancel.set()

    def _on_measured(self, clip, duration, problem):
        clip.duration = duration
        clip.problem = problem
        if problem:
            self.unprobeable.append((clip.relative_path, problem))

    def _on_probe_stopped(self):
        """The thread has really stopped, so the guard can come off.

        Only now is it safe to clear `_thread`: `closeEvent` uses it to refuse
        closing, and clearing it one signal early lets the window be destroyed
        while the thread it owns is still alive.
        """
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.deleteLater()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()

    def _close_video(self):
        if self._bridge is not None:
            self._bridge.shutdown()
            self._bridge = None

    def on_play_pause(self):
        if self._bridge is not None:
            self._bridge.toggle_play()

    def _on_position_changed(self, position):
        self.ui.labelPosition.setText(_format_seconds(position))
        if not self._bridge or not self._bridge.duration:
            return
        span = self._bridge.duration
        self.ui.sliderPosition.blockSignals(True)
        self.ui.sliderPosition.setRange(0, int(span * 1000))
        self.ui.sliderPosition.setValue(int(position * 1000))
        self.ui.sliderPosition.blockSignals(False)

    def _on_duration_changed(self, duration):
        self.ui.sliderPosition.setRange(0, int(max(0.0, duration) * 1000))

    def _on_paused_changed(self, paused):
        self.ui.buttonPlay.setText("Play" if paused else "Pause")

    def _on_seek(self):
        if self._bridge is not None:
            self._bridge.seek_exact(self.ui.sliderPosition.value() / 1000.0)

    # -- the title --------------------------------------------------------

    def _selected_text(self) -> str:
        cursor = self.ui.textFileName.textCursor()
        return " ".join(cursor.selectedText().split())

    def _refresh_add_title(self):
        """Add Title is available exactly when something is selected.

        It is a shortcut into the title field, not a replacement for it:
        `clip01.mp4` has no title in its name and the user still has to type one,
        so the field stays visible and editable and this only ever overwrites it.
        """
        self.ui.buttonAddTitle.setEnabled(bool(self._selected_text()))

    def on_add_title(self):
        selected = self._selected_text()
        if not selected:
            return
        self.ui.tagForm.write_tags({**self.ui.tagForm.read_tags(),
                                    "title": selected})
        self.ui.tagForm.refresh_required_fields()
        self._refresh_next()

    def on_manage_rules(self):
        clip = self._current_clip()
        dialog = RulesDialog(self.session,
                            filename=clip.filename if clip else "",
                            parent=self)
        dialog.exec()
        dialog.deleteLater()
        # The new rules apply from here on, so the clip in front of the user is
        # re-resolved -- a rule taught on clip 20 should visibly work here.
        if clip is not None:
            self._load_current()

    # -- answering --------------------------------------------------------

    def _current_clip(self):
        if self.position >= len(self.clips):
            return None
        return self.clips[self.position]

    def _refresh_next(self):
        self.ui.buttonNext.setEnabled(not self.ui.tagForm.missing_required_labels())

    def on_next(self):
        """Settle this clip: write its record, and move on."""
        clip = self._current_clip()
        if clip is None or self.ui.tagForm.missing_required_labels():
            return
        tags = {
            key: value.strip()
            for key, value in self.ui.tagForm.read_tags().items()
            if value.strip()
        }
        try:
            clip.write_record(tags, provenance=clip.filename)
        except (OSError, ValueError) as error:
            log_exception(f"could not record {clip.name}", error)
            QMessageBox.warning(
                self, "That clip could not be saved",
                f"{clip.filename} could not be recorded:\n\n{error}")
            return
        record_use(tags, self.ui.tagForm.vocabulary_path)
        self.done += 1
        self.position += 1
        self._load_current()

    def on_skip_delete(self):
        """Delete this clip from `import/` and move on.

        The one destructive action in the feature, so the confirmation names the
        file and states the consequence rather than asking "are you sure" about
        something whose button already says what it does. `import/` is a staging
        folder: a file deleted here is not in the library and will not be.
        """
        clip = self._current_clip()
        if clip is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete this clip from the import folder?",
            f"{clip.filename}\n\n"
            f"It will be deleted from the import folder. It is not in your "
            f"library and will not be imported, and this cannot be undone — so "
            f"if this is the only copy of it, keep it somewhere else first.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        _delete_clip(clip)
        self.deleted.append(clip.relative_path)
        self.done += 1
        self.position += 1
        self._load_current()

    # -- finishing --------------------------------------------------------

    def _show_report(self):
        self._close_video()
        self._stop_probe()
        self._set_editing(False)
        self.ui.textFileName.setPlainText("")
        self.ui.labelProgress.setText("Every clip in the import folder has been "
                                      "dealt with.")
        self.ui.textReport.setPlainText(self._report_text())
        self.ui.textReport.setVisible(True)
        self.ui.buttonImport.setVisible(True)
        self.ui.buttonReopen.setVisible(bool(self.deleted or self.unprobeable))
        self._refresh_import_button()

    def _report_text(self) -> str:
        lines = [f"Read {len(self.clips) + self.done} clip(s) from {self.root}",
                 f"  {self.done} dealt with",
                 f"  {self._remaining()} left to do"]
        if self.deleted:
            lines.append("")
            lines.append(f"Deleted from the import folder ({len(self.deleted)}):")
            lines.extend(f"  - {name}" for name in self.deleted)
        if self.unprobeable:
            lines.append("")
            lines.append(
                f"Left alone, because their record could not be written "
                f"({len(self.unprobeable)}):")
            lines.extend(f"  - {name}: {why}" for name, why in self.unprobeable)
        lines.append("")
        lines.append("The tags for every clip you settled are written beside "
                     "their videos. Import Now hands them to the importer.")
        return "\n".join(lines)

    def _refresh_import_button(self):
        self.ui.buttonImport.setEnabled(
            build_catalog(self.root).clips != ())

    def on_reopen_unfinished(self):
        """Back to the first clip that is not finished.

        Unresolved clips have no record, so the import cannot see them; they stay
        in `import/` and this puts the user back on the first one.
        """
        self._set_editing(True)
        self.ui.textReport.setVisible(False)
        self.ui.buttonImport.setVisible(False)
        self.ui.buttonReopen.setVisible(False)
        self.position = 0
        self._load_current()

    def _set_editing(self, editing: bool):
        for widget in (self.ui.buttonNext, self.ui.buttonSkipDelete,
                       self.ui.buttonAddTitle, self.ui.buttonManageRules,
                       self.ui.tagForm):
            widget.setVisible(editing)

    # -- the import -------------------------------------------------------

    def on_import_now(self):
        """Hand the tagged library to the importer, unchanged.

        This is the whole of untagged import's back half: every settled clip has a
        `.cnfo` beside it, so the tagged path takes over exactly as it would for a
        library exported from another commcut.
        """
        self._close_video()
        catalog = build_catalog(self.root)
        if not catalog.clips:
            QMessageBox.information(
                self, "Nothing to import",
                "No clip in the import folder has a readable record yet.")
            return

        progress = QProgressDialog("Importing...", None, 0, 0, self)
        progress.setWindowTitle("Import")
        progress.setParent(None)
        progress.setModal(False)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        try:
            result = execute_import(
                plan_import(candidates_from_catalog(catalog),
                            _schemes(), self.library_root,
                            existing=build_catalog(self.library_root)),
                on_progress=lambda done, path: progress.setLabelText(
                    f"Imported {done} clip(s)\n{path}"),
            )
        finally:
            progress.deleteLater()

        body = [
            f"Imported {len(result.written)} clip(s) into {self.library_root}.",
        ]
        if result.already_present:
            body.append(f"{len(result.already_present)} were already there.")
        if result.failed:
            body.append(f"{len(result.failed)} failed:")
            body.extend(f"  - {skip}" for skip in result.failed)
        if result.cancelled:
            body.append("The run was cancelled. Everything it had already "
                        "written was kept; running it again finishes the rest.")
        QMessageBox.information(self, "Import finished", "\n".join(body))

    # -- closing ----------------------------------------------------------

    def closeEvent(self, event):
        """Refuse to close while a probe is running.

        A `QThread` still running when its owner is destroyed aborts the process.
        And the player has to be shut down before the window dies, because mpv is
        embedded into the video frame's native handle — `destroyed` is too late.
        """
        if self._thread is not None:
            self._cancel.set()
            event.ignore()
            return
        self._close_video()
        super().closeEvent(event)


def _delete_clip(clip) -> None:
    """Remove a clip's video and any record beside it.

    Both: a partial record from an abandoned session is litter, and
    `build_catalog` ignores a record with no sibling video but the folder should
    not accumulate them.
    """
    for path in (clip.path, _record_path(clip.path)):
        try:
            os.remove(path)
        except OSError:
            pass
    directory = os.path.dirname(clip.path)
    while directory and os.path.isdir(directory) and not os.listdir(directory):
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        try:
            os.rmdir(directory)
        except OSError:
            break
        directory = parent


def _format_seconds(value: float) -> str:
    total = int(max(0.0, value))
    return f"{total // 60}:{total % 60:02d}"


def _schemes() -> ExportSchemes:
    """The user's export schemes, for the import to render destinations with.

    The same snapshot the editor exports through, so a clip that lands in the
    library from `import/` and one exported from a compilation are named and
    filed by identical rules.
    """
    return load_export_schemes(os.path.join(PROJECT_ROOT, "settings.json"))


def create(app, session: MeshSession = None, root: str | None = None):
    """Build the queue. Returns the window, unscaled; the shell shows it.

    `session` is the Mesh Wizard's, carried across by the shell so the folder
    answers are not asked twice. Omitted, it is rebuilt — which re-reads the
    library and re-syncs the vocabulary, and is what a direct launch does.
    """
    return QueueWindow(session=session, root=root)
