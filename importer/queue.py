"""The Library Mesh Tag Editor: one clip at a time, until the library is done.

Applies to: `importer/queue.py`, `importer/queuewindow.ui`, `importer/rules.py`,
`importer/importrun.py`, `importer/values.py`, `shared/mesh.py`,
`shared/importing.py`, `shared/values.py`, `shared/tag_form.py`.

The Untagged Library Mesh has already said what every **folder name** in `import/`
means. What is left is what it cannot say: a **title**, which is a *region* of a
file name rather than a whole token, and so the one tag per clip that has to come
from a person. This window is that per-clip pass, and it is the whole of untagged
import — the "auto import and fix up the rest" options are retired, because no
clip can be finished without a title.

It also serves the **tagged** path. `shared/values.py` answers one question per
distinct tag value and rewrites the records; the clips it leaves unfinished — one
missing a tag it needs, because a required tag was removed — come back here, and
`is_already_done` picks up exactly them. So this window is reached from either
mesh, and `docs/importing.md` says which endings each offers.

Five decisions shape it:

- **A settled clip's `.cnfo` is written to `import/` immediately.** The tagged
  import then takes over unchanged — `build_catalog`, `candidates_from_catalog`,
  `plan_import`, `execute_import` — so the untagged path converges on the tagged
  one rather than forking it. It also means an eight-hundred-clip session has a
  save point per clip. Resume asks `missing_required_tags(record)`, *not* "is
  there a record", because a half-tagged record exists and is not finished.
- **Settling a clip does not touch `vocabulary.json`.** The values in an imported
  library are somebody else's, and this window is where the user finds that out,
  not where they are confirmed. They enter the file when `sync_vocabulary` reads
  them back out of `export/`, which is the one owner of that file's contents and
  the only source they have any business coming from. Recording them here wrote
  foreign spellings into the user's tag history and then deleted them on the next
  open, because the prune only ever counts the library.
- **The rules are learned from file names and can never set a title.** A rule is
  a standing instruction; a title is per clip. That distinction is the reason the
  title is the only thing still asked for here, and it is enforced in
  `MeshSession.learn_rule` rather than here.
- **"Skip - Delete" removes the file.** `import/` is a staging folder, not a
  library: anything in it is a copy from somewhere else or expendable. So there
  is a destructive option, labelled as one, confirmed with the file's name, kept
  off `Next`'s side of the button row, and never a default button.
- **A rule never overwrites a value the user typed.** Closing the rules modal
  re-resolves the clip, which is how a newly taught rule reaches the clip in
  front of you. The reload therefore keeps what the form already holds and lets
  the rules fill only the gaps — otherwise opening the modal to look at it and
  closing it again silently discarded the title, which is the one tag no rule
  can fill and so the one with nowhere else to come from.

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
from PySide6.QtWidgets import QMainWindow, QMessageBox

from shared.catalog import build_catalog, sync_vocabulary
from shared.diagnostics import log, log_exception
from shared.exporting import export_folder, missing_required_tags
from shared.importing import find_videos, import_folder
from shared.mesh import MeshSession
from shared.mpv import MpvBridge, create_mpv_player
from shared.records import (
    RECORD_EXTENSION,
    ClipRecord,
    load_record,
    write_record,
)
from shared.segments import probe_duration
from shared.session import shell
from shared.tag_form import TAG_FIELDS, TagForm, field_change_signal
from shared.ui_loader import UiLoader, adopt_title
from shared.values import TITLE_TAG, ValueSession
from shared.vocabulary import get_vocabulary, vocabulary_path

from importer.importrun import confirm_and_import
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
    """Every clip in `import/` that is not finished yet, one at a time, with the
    folder answers applied when there is nothing in the record."""

    def __init__(self, session: MeshSession = None, root: str | None = None,
                 parent=None):
        super().__init__()
        loader = UiLoader()
        loader.register_widget(TagForm)
        self.ui = loader.load(
            QFile(resource_path("importer", "queuewindow.ui")), self)
        adopt_title(self, self.ui)
        self.setCentralWidget(self.ui)

        self.root = root or import_folder()
        self.library_root = export_folder()
        self.session = session or self._build_session()
        self.clips: list[QueueClip] = []
        self.position = 0
        self.done = 0
        self.deleted: list[str] = []
        self.unprobeable: list[tuple[str, str]] = []
        #: Distinct tag values in the records, for the report's offer to review them.
        self._pending_values = 0

        self._thread: QThread | None = None
        self._worker: ProbeWorker | None = None
        self._bridge: MpvBridge | None = None
        self._cancel = threading.Event()
        #: Whether the mouse is on the position slider, which makes the handle
        #: the user's rather than a readout. See `_on_scrub_began`.
        self._scrubbing = False

        self.ui.tagForm.set_locks_visible(False)
        self.ui.tagForm.init_vocabulary(vocabulary_path())
        self.ui.buttonNext.clicked.connect(self.on_next)
        self.ui.buttonSkipDelete.clicked.connect(self.on_skip_delete)
        self.ui.buttonAddTitle.clicked.connect(self.on_add_title)
        self.ui.buttonManageRules.clicked.connect(self.on_manage_rules)
        self.ui.buttonPlay.clicked.connect(self.on_play_pause)
        self.ui.buttonValues.clicked.connect(self.on_review_values)
        self.ui.buttonImport.clicked.connect(self.on_import_now)
        self.ui.buttonMenu.clicked.connect(self.on_back_to_menu)
        # `cursorPositionChanged` rather than a "selection changed" signal,
        # because QPlainTextEdit has none: a selection is a cursor range, and
        # moving either end moves the position. Together with `textChanged` it
        # covers drag-selecting, click-selecting, and the programmatic
        # `selectAll` a fresh clip is shown with.
        self.ui.textFileName.cursorPositionChanged.connect(
            self._refresh_add_title)
        self.ui.textFileName.textChanged.connect(self._refresh_add_title)
        self.ui.sliderPosition.sliderPressed.connect(self._on_scrub_began)
        self.ui.sliderPosition.sliderReleased.connect(self._on_seek)
        self.ui.sliderPosition.sliderReleased.connect(self._on_scrub_ended)
        # Every field re-derives what the required-tag rule decides: the outline
        # on the fields still empty, and whether Next can be pressed. Both come
        # from the one handler, so neither can answer for a later state of the
        # form than the other.
        for namespace in TAG_FIELDS:
            field_change_signal(self.ui.tagForm.field(namespace)).connect(
                self._refresh_tag_state)

        self._enumerate()
        self._load_current()

    # -- input ------------------------------------------------------------

    def _build_session(self) -> MeshSession:
        """Read the library and the import folder the way the Untagged Library Mesh
        did."""
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

    def _load_current(self, keep_entered=False):
        """Show the clip at this position, autofilled from the record or the rules.

        `keep_entered` is for the re-resolve that follows the rules dialog. A rule
        taught there has to reach the clip in front of the user, but a value
        *they* put in is not an answer a rule may replace — so the fresh
        resolution is written first and only the fields still empty take from it.
        Everywhere else the form is written whole, because advancing a clip must
        not carry the last clip's typing into the next one.
        """
        if self.position >= len(self.clips):
            self._show_report()
            return

        entered = {}
        if keep_entered:
            entered = {key: value
                       for key, value in self.ui.tagForm.read_tags().items()
                       if value.strip()}

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
        # **The record is authoritative when there is one.** It was not always: the
        # folder answers used to sit underneath it, which resurrected a tag the Tagged
        # Library Mesh had just removed, whenever this window was reopened carrying a
        # live `MeshSession`. A clip's tags live in its record — invariant 12 — and
        # the folder answers only have something to add on a clip that has never been
        # settled at all.
        settled = clip.existing_tags()
        resolved = self.session.tags_for_clip(clip.relative_path)
        # Entered values last, so they win over both. They are the only tags
        # nothing but the user has an opinion about, and the rules were consulted
        # to fill gaps rather than to settle arguments.
        self.ui.tagForm.write_tags({**(settled or resolved.tags), **entered})
        self._describe_conflicts(resolved)
        self._open_video(clip.path)
        self._refresh_add_title()
        self._refresh_tag_state()
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

    def _on_scrub_began(self):
        """From here until the release, the handle belongs to the user.

        mpv reports `time-pos` many times a second and every report was written
        into the slider. That is right until the mouse is down on it: the reports
        then arrive faster than a hand can move, the handle is pulled back to
        wherever the video has got to, and the release seeks there — so the
        scrub did nothing. Whether the video was playing only decided how fast the
        drag lost that race, which is why it looked like a rule about the play
        state: paused, `time-pos` stops moving, the handle is left alone, and the
        same drag works.
        """
        self._scrubbing = True

    def _on_scrub_ended(self):
        """The mouse is off the handle, so the slider is a readout again.

        Connected *after* `_on_seek`, so the flag stays set for the whole of the
        gesture rather than coming off halfway through it.
        """
        self._scrubbing = False

    def _on_position_changed(self, position):
        self.ui.labelPosition.setText(_format_seconds(position))
        if self._scrubbing:
            # The label keeps counting, because it reports where the video is.
            # The handle does not move, because it says where the user is going.
            return
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
        self._refresh_tag_state()

    def on_manage_rules(self):
        clip = self._current_clip()
        dialog = RulesDialog(self.session,
                            filename=clip.filename if clip else "",
                            parent=self)
        dialog.exec()
        dialog.deleteLater()
        # The new rules apply from here on, so the clip in front of the user is
        # re-resolved -- a rule taught on clip 20 should visibly work here.
        # Closing the dialog without teaching anything still reloads, which is
        # harmless because `keep_entered` means the only thing a reload can change
        # is a field that was empty.
        if clip is not None:
            self._load_current(keep_entered=True)

    # -- answering --------------------------------------------------------

    def _current_clip(self):
        if self.position >= len(self.clips):
            return None
        return self.clips[self.position]

    def _refresh_tag_state(self):
        """Re-derive everything the required-tag rule decides on this clip.

        The outline and the Next button are that rule asked twice — once as a
        red box on each field that is still empty, once as a button that cannot
        be pressed yet — so both are answered together and neither can describe
        a different moment than the other.

        Recomputed on every keystroke, because an outline that outlives the
        value it complained about is a complaint about a state the user has
        already left.
        """
        self.ui.tagForm.refresh_required_fields()
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
        # **Before** the report is written. The report quotes the count, so building
        # it first would mean two walks and — worse — a button and a sentence that
        # could disagree, which is the one thing they exist to prevent.
        has_values = self._has_values_to_review()
        self.ui.textReport.setPlainText(self._report_text())
        self.ui.textReport.setVisible(True)
        self.ui.buttonValues.setVisible(has_values)
        self.ui.buttonImport.setVisible(True)
        self.ui.buttonReopen.setVisible(bool(self.deleted or self.unprobeable))
        # The report is an **ending**, not a step. Without this the screen offered
        # only two ways on — to another window, or back to unfinished clips — and a
        # user who had just imported everything was left holding the window manager's
        # X. Same button, same wording as the editor's export summary, because it is
        # the same decision.
        self.ui.buttonMenu.setVisible(True)
        self._refresh_import_button()

    def _has_values_to_review(self) -> bool:
        """Whether the Tagged Library Mesh has anything to ask about.

        One walk and one session, built here and shared by the button's visibility
        and the report's closing line, so the screen cannot offer a window that has
        nothing to do and then say otherwise three lines lower. Counting the session's
        entries rather than the tags is what makes the number mean something: two
        hundred clips that all say `network: CN` is one question, not two hundred.
        """
        catalog = build_catalog(self.root)
        self._pending_values = (
            len(ValueSession(self.root, catalog).entries()) if catalog.clips else 0)
        return bool(self._pending_values)

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
                     "their videos. Import Now hands them to the importer as they "
                     "are.")
        if self._pending_values:
            lines.append("")
            lines.append(
                f"There are {self._pending_values} tag value(s) in those records "
                f"that came from somebody else's library. Review Tag Values asks "
                f"about each one, once, however many clips carry it — which is what "
                f"turns their words into yours before they are filed.")
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
        self.ui.buttonValues.setVisible(False)
        self.ui.buttonMenu.setVisible(False)
        self.ui.buttonReopen.setVisible(False)
        self.position = 0
        self._load_current()

    def _set_editing(self, editing: bool):
        for widget in (self.ui.buttonNext, self.ui.buttonSkipDelete,
                       self.ui.buttonAddTitle, self.ui.buttonManageRules,
                       self.ui.tagForm):
            widget.setVisible(editing)

    # -- onward -----------------------------------------------------------

    def on_review_values(self):
        """Ask about the tag values in the records before anything is imported.

        The other half of the fork this report offers. A value is a *whole token*
        shared by however many clips carry it, so it is asked once here rather than
        per clip — which is the mirror image of why the title was asked per clip,
        and why the two are two windows rather than one with two modes.
        """
        self._close_video()
        shell().open_safely('values', root=self.root)

    def on_import_now(self):
        """Hand the tagged library to the importer, unchanged.

        This is the whole of untagged import's back half: every settled clip has a
        `.cnfo` beside it, so the tagged path takes over exactly as it would for a
        library exported from another commcut — with whatever values those records
        carry, which is the user's call to have skipped the value mesh.
        """
        self._close_video()
        confirm_and_import(self, self.root, self.library_root,
                           os.path.join(PROJECT_ROOT, "settings.json"))

    def on_back_to_menu(self):
        """Close this window, and let the shell bring the menu back.

        `close()` rather than `shell().open('menu')`: the menu is **hidden** while
        another window is up, not closed, so closing the child is what reveals it —
        and this window's `closeEvent` is what knows whether it still owns a
        player that must not be destroyed yet.

        Deliberately not a confirmation. Nothing is lost by leaving: the records
        written so far stay in `import/`, unfinished clips stay unfinished, and
        `is_already_done` picks the run back up where it stopped. Which is why the
        button's tooltip says so rather than the press asking about it.
        """
        self._close_video()
        self._stop_probe()
        self.close()

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


def create(app, session: MeshSession = None, root: str | None = None):
    """Build the queue. Returns the window, unscaled; the shell shows it.

    `session` is the Untagged Library Mesh's, carried across by the shell so the
    folder answers are not asked twice. Omitted, it is rebuilt — which re-reads the
    library and re-syncs the vocabulary, and is what both a direct launch and the
    Tagged Library Mesh's residue hand-off do. Carrying no clip list across is
    deliberate: `is_already_done` re-derives the residue from the folder, which is
    where the truth lives.
    """
    return QueueWindow(session=session, root=root)
