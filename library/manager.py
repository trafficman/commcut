"""The Library Manager: browse, preview, and re-tag the export library.

Applies to: `library/manager.py`, `library/librarywindow.ui`,
`shared/retagging.py`.

The export library is the one place a finished clip can still be
changed: its tags live in its record, and its filename and folder
are a projection of them (invariant 12), so a clip whose tags are
wrong is a clip in the wrong place. This window is the whole
library in one tree — every clip the catalog finds, with the tag
form and a preview player beside it — so a clip can be re-tagged
and saved without opening the editor for it.

Everything on screen is derived state. The tree is rebuilt from a
fresh `build_catalog` walk every time it is shown, because a save
changes what that walk would find; the form is written from the
selected clip's record; and the destination label is re-rendered
through `plan_clip_destination` on every field change, so what
saving would do is visible before it happens. Nothing is edited in
place, and nothing is cached across a save.

The walk runs on a worker `QThread` behind an indeterminate
dialog, the editor's export pattern: the window is frozen, and the
dialog is deliberately parentless, because `setEnabled(False)`
cascades to child widgets and would disable the dialog's Cancel.
The thread's teardown hangs off `QThread.finished` (invariant 14),
and cancelling the walk closes the window — there is nothing to
show — with the close deferred to that teardown, because
`closeEvent` refuses to close over a thread that is still running.

Saving is `plan_retag` then `execute_retag`: the plan is pure and
refuses by name (a missing required tag, a destination another
clip holds), and the execution is the ordered transaction that
publishes the record before it moves the video. On success the
nine suggestable tags are recorded (the queue's commit points),
the library is walked again, and the clip is re-selected by its
clip ID — never by path, which is the one thing that just changed.
"""

import os
import sys
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.environment import (
    resource_path,
    settings_path,
    setup_environment,
)

SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

from PySide6.QtCore import QFile, QObject, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QMainWindow,
    QMessageBox,
    QStyle,
    QTreeWidgetItem,
)

from shared.catalog import Catalog, build_catalog, problem_summary
from shared.diagnostics import log, log_exception
from shared.exporting import (
    ExportPlanError,
    export_folder,
    load_export_schemes,
    missing_required_tags,
    plan_clip_destination,
)
from shared.loading import LoadingDialog
from shared.mpv import MpvBridge, create_mpv_player
from shared.naming import compile_filename_scheme
from shared.paths import compile_folder_scheme
from shared.retagging import (
    execute_retag,
    plan_retag,
    retag_is_in_place,
)
from shared.session import shell
from shared.tag_form import (
    SUGGESTED_TAG_FIELDS,
    TAG_FIELDS,
    TagForm,
    field_change_signal,
)
from shared.ui_loader import UiLoader, adopt_title
from shared.vocabulary import record_use, vocabulary_path


class CatalogWorker(QObject):
    """Walks the export library off the GUI thread.

    `build_catalog` is a walk that reports progress per record and
    stops between directories when the cancel event is set, so a
    walk the user gave up on does not have to finish. The outcome
    is emitted once — `done` with the catalog, or `failed` with a
    reason — and `finished` always follows, which is the signal
    the thread's teardown hangs off.
    """

    done = Signal(object)
    failed = Signal(str)
    progressed = Signal(int, str)
    finished = Signal()

    def __init__(self, root, cancel_event=None):
        super().__init__()
        self.root = root
        self.cancel_event = cancel_event or threading.Event()

    @Slot()
    def run(self):
        try:
            catalog = build_catalog(
                self.root,
                on_progress=self._progress,
                should_cancel=self.cancel_event.is_set,
            )
            self.done.emit(catalog)
        except Exception as error:
            log_exception("reading the library failed", error)
            self.failed.emit(f"{type(error).__name__}: {error}")
        self.finished.emit()

    def _progress(self, clips_found, relative_path):
        self.progressed.emit(clips_found, relative_path)


class LibraryManagerWindow(QMainWindow):
    """Every clip in the export library, in one tree, with the
    tag form and a preview beside it."""

    def __init__(self):
        super().__init__()
        self.root = export_folder()
        schemes = load_export_schemes(settings_path())
        self._folder_scheme = compile_folder_scheme(schemes.folder_scheme)
        self._filename_scheme = compile_filename_scheme(schemes.file_scheme)

        loader = UiLoader()
        loader.register_widget(TagForm)
        self.ui = loader.load(
            QFile(resource_path("library", "librarywindow.ui")), self)
        adopt_title(self, self.ui)
        self.setCentralWidget(self.ui)

        self._catalog = Catalog()
        self._clips_by_path: dict[str, object] = {}
        self._clip_items: dict[str, QTreeWidgetItem] = {}
        self._current_leaf = None
        self._clip = None
        self._bridge = None
        self._thread = None
        self._worker = None
        self._dialog = None
        self._cancel = None
        self._close_after_walk = False
        self._select_after_walk = ""
        self._move_report = None
        self._scrubbing = False

        self.ui.tagForm.set_locks_visible(False)
        self.ui.tagForm.init_vocabulary(vocabulary_path())
        self.ui.treeLibrary.setHeaderHidden(True)
        self.ui.buttonSave.setEnabled(False)

        self.ui.buttonPlay.clicked.connect(self.on_play_pause)
        self.ui.buttonSave.clicked.connect(self.on_save)
        self.ui.buttonMenu.clicked.connect(self.on_back_to_menu)
        self.ui.sliderPosition.sliderPressed.connect(
            self._on_scrub_began)
        self.ui.sliderPosition.sliderReleased.connect(
            self._on_scrub_ended)
        self.ui.sliderPosition.sliderReleased.connect(self._on_seek)
        self.ui.treeLibrary.itemClicked.connect(self._on_item_clicked)
        self.ui.treeLibrary.currentItemChanged.connect(
            self._on_current_item_changed)
        for namespace in TAG_FIELDS:
            field_change_signal(
                self.ui.tagForm.field(namespace)).connect(
                    self._on_tag_edited)

        self._begin_walk()

    # -- the catalog walk ---------------------------------------------

    def _begin_walk(self, rebuild=False):
        """Walk the library on a worker thread, behind a dialog.

        The window is frozen for the walk's duration — an open has
        nothing to show yet, and a rebuild has already committed a
        save — and the dialog is deliberately parentless, because
        `setEnabled(False)` cascades to child widgets and a disabled
        dialog's Cancel button does nothing. A rebuild's dialog is
        modal and cannot be cancelled: the save it follows is
        already on disk, so the walk has to finish.
        """
        self._cancel = threading.Event()
        worker = CatalogWorker(self.root, cancel_event=self._cancel)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_catalog_built)
        worker.failed.connect(self._on_walk_failed)
        worker.progressed.connect(self._on_walk_progressed)
        # `thread.quit` is what actually ends the thread: a slot
        # returning does not leave the thread's `exec()` loop, so
        # the worker's own `finished` signal is the one that quits
        # it, and the teardown hangs off the thread's `finished`.
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._on_walk_stopped)
        self._worker = worker
        self._thread = thread

        self.setEnabled(False)
        dialog = LoadingDialog(
            "Reading the library…", cancellable=not rebuild,
            modal=False, title="Library Manager")
        dialog.set_range(0, 0)
        dialog.canceled.connect(self._on_walk_cancel_requested)
        dialog.show()
        self._dialog = dialog

        thread.start()
        if rebuild and dialog.isVisible():
            # The walk is still running, so this blocks until
            # its `done` handler closes the dialog. A walk that
            # already finished — a synchronous one, in a test —
            # closed it inside `thread.start()`, and there is
            # nothing left to wait for.
            dialog.exec()

    def _on_walk_cancel_requested(self):
        """The user gave up on the walk: stop it, and close the
        window once it has really stopped."""
        if self._cancel is not None:
            self._cancel.set()

    def _on_walk_progressed(self, clips_found, relative_path):
        dialog = self._dialog
        if dialog is not None:
            plural = "" if clips_found == 1 else "s"
            dialog.set_message(
                f"{clips_found} clip{plural} — {relative_path}")

    def _on_catalog_built(self, catalog):
        """The walk finished: show what it found.

        The dialog is closed before anything else, so no mpv-backed
        widget is constructed while a top-level window is foreground
        (invariant 6). A cancelled walk has nothing to show, so the
        window closes — deferred to `_on_walk_stopped`, because the
        thread it owns is still running here and `closeEvent`
        refuses to close over it.
        """
        dialog, self._dialog = self._dialog, None
        if dialog is not None:
            dialog.close()
        if catalog.cancelled:
            self._close_after_walk = True
            return
        self._catalog = catalog
        self._build_tree(catalog)
        self._show_problems(catalog)
        # The walk invalidated what was loaded: every item the tree
        # held is gone, and a save just changed the clip's tags. Drop
        # both before re-selecting, so the switch the re-selection is
        # sees no clip to be dirty against and loads the clip outright
        # instead of asking about edits that were just saved.
        self._clip = None
        self._current_leaf = None
        clip_id, self._select_after_walk = self._select_after_walk, ""
        if clip_id:
            self._select_clip_id(clip_id)
        report, self._move_report = self._move_report, None
        if report is not None:
            self._report_move(*report)

    def _on_walk_failed(self, message):
        """The walk could not run at all. The window stays up,
        empty, with the reason in the problems area."""
        self._show_problems_text(f"The library could not be read:\n{message}")

    def _on_walk_stopped(self):
        """The thread has really stopped, so the guard can come off.

        Only now is it safe to clear `_thread`: `closeEvent` uses it
        to refuse closing, and clearing it one signal early would let
        the window be destroyed while the thread it owns is still
        alive.
        """
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.deleteLater()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()
        dialog, self._dialog = self._dialog, None
        if dialog is not None:
            dialog.deleteLater()
        self.setEnabled(True)
        if self._close_after_walk:
            self._close_after_walk = False
            self.close()

    # -- the tree ----------------------------------------------------

    def _build_tree(self, catalog):
        """Rebuild the sidebar tree from a fresh catalog.

        The tree is derived state: it is never edited in place, only
        rebuilt from a walk, because a save changes what the walk
        would find. Only folders that contain clips exist — the tree
        is clip-driven — and every level is sorted casefolded, so a
        folder named `B` sorts before a clip named `b.mp4` however
        the catalog's path order interleaved them.
        """
        self.ui.treeLibrary.clear()
        self._clips_by_path = {}
        self._clip_items = {}
        folders: dict[tuple[str, ...], QTreeWidgetItem] = {}
        for clip in catalog.clips:
            components = clip.relative_path.split("/")
            parent = None
            for depth in range(len(components) - 1):
                key = tuple(components[:depth + 1])
                node = folders.get(key)
                if node is None:
                    node = QTreeWidgetItem(parent, [components[depth]])
                    if parent is None:
                        self.ui.treeLibrary.addTopLevelItem(node)
                    node.setFlags(node.flags() & ~Qt.ItemIsSelectable)
                    folders[key] = node
                parent = node
            leaf = QTreeWidgetItem(parent, [components[-1]])
            if parent is None:
                self.ui.treeLibrary.addTopLevelItem(leaf)
            leaf.setData(0, Qt.UserRole, clip.record.clip_id)
            leaf.setData(0, Qt.UserRole + 1, clip.relative_path)
            self._clips_by_path[clip.relative_path] = clip
            if clip.record.clip_id:
                self._clip_items[clip.record.clip_id] = leaf
        self._sort_tree()

    def _sort_tree(self):
        """Sort every level of the tree casefolded."""
        tree = self.ui.treeLibrary
        top_level = [
            tree.topLevelItem(index)
            for index in range(tree.topLevelItemCount())
        ]
        top_level.sort(key=lambda item: item.text(0).casefold())
        for item in top_level:
            tree.takeTopLevelItem(tree.indexOfTopLevelItem(item))
        for item in top_level:
            tree.addTopLevelItem(item)
        for item in top_level:
            self._sort_subtree(item)

    def _sort_subtree(self, item):
        children = [
            item.child(index) for index in range(item.childCount())
        ]
        children.sort(key=lambda child: child.text(0).casefold())
        for child in children:
            item.removeChild(child)
        for child in children:
            item.addChild(child)
        for child in children:
            self._sort_subtree(child)

    def _on_item_clicked(self, item, column):
        """A folder row expands or collapses on click.

        Qt toggles a click on the row's own expand/collapse indicator
        itself and does not emit this signal for it, so the toggle
        here is the one that fires, whichever part of the row was
        clicked.
        """
        if item.data(0, Qt.UserRole) is None:
            item.setExpanded(not item.isExpanded())

    def _on_current_item_changed(self, current, previous):
        """Selecting a clip leaf loads it.

        Folder rows are not selectable, so the current item is always
        a clip leaf or nothing. A switch away from a clip with
        unsaved edits asks about them first, and a cancelled switch
        puts the current item back where it was.
        """
        if current is None or current.childCount() != 0:
            return
        if current is self._current_leaf:
            return
        relative = current.data(0, Qt.UserRole + 1) or ""
        clip = self._clips_by_path.get(relative)
        if clip is None:
            return
        if not self._prompt_unsaved():
            leaf = self._current_leaf
            if leaf is not None:
                self.ui.treeLibrary.blockSignals(True)
                self.ui.treeLibrary.setCurrentItem(leaf)
                self.ui.treeLibrary.blockSignals(False)
            return
        self._current_leaf = current
        self._load_clip(clip)

    def _select_clip_id(self, clip_id):
        """Make the clip with this ID the selected leaf.

        The ID, never the path: a save just changed the path, and
        the ID is the one thing that did not.
        """
        leaf = self._clip_items.get(clip_id)
        if leaf is not None:
            self.ui.treeLibrary.setCurrentItem(leaf)

    def _show_problems(self, catalog):
        self._show_problems_text(
            problem_summary(catalog.problems) if catalog.problems else "")

    def _show_problems_text(self, text):
        self.ui.textProblems.setPlainText(text)
        self.ui.textProblems.setVisible(bool(text))

    # -- the selected clip -------------------------------------------

    def _load_clip(self, clip):
        """Show one clip: its name, its tags, its preview, and
        where saving would put it."""
        self._clip = clip
        self.ui.labelClipName.setText(clip.relative_path)
        self.ui.tagForm.write_tags(clip.tag_dict())
        self.ui.tagForm.refresh_required_fields()
        self.ui.buttonSave.setEnabled(True)
        self._open_video(clip.path)
        self._refresh_destination()
        self.ui.labelStatus.setText("")

    def _current_clip(self):
        return self._clip

    def _form_tags(self) -> dict[str, str]:
        """The form's values, stripped, empties dropped."""
        return {
            key: value.strip()
            for key, value in self.ui.tagForm.read_tags().items()
            if value.strip()
        }

    def _is_dirty(self) -> bool:
        """Whether the form holds edits the loaded clip does not."""
        clip = self._current_clip()
        if clip is None:
            return False
        return self._form_tags() != clip.tag_dict()

    def _prompt_unsaved(self) -> bool:
        """Ask about unsaved edits, if there are any.

        Returns whether to go ahead: Save commits them, Discard drops
        them, and Cancel stops whatever was about to happen.
        """
        if not self._is_dirty():
            return True
        answer = QMessageBox.warning(
            self, "Unsaved changes",
            "This clip has unsaved changes. "
            "Save them before moving on?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save)
        if answer == QMessageBox.Cancel:
            return False
        if answer == QMessageBox.Save:
            return self.on_save()
        return True

    def _refresh_destination(self):
        """Re-render the live destination label.

        Pure computation over the form's current values, so it runs on
        every field change: what saving would do is visible before it
        happens. A raise — an incomplete tag set, a render error —
        clears the label rather than showing a stale destination.
        """
        clip = self._current_clip()
        if clip is None:
            self.ui.labelDestination.setText("")
            return
        try:
            destination = plan_clip_destination(
                tags=self._form_tags(),
                folder_scheme=self._folder_scheme,
                filename_scheme=self._filename_scheme,
                label=os.path.basename(clip.path) or clip.path,
            )
        except ExportPlanError:
            self.ui.labelDestination.setText("")
            return
        if retag_is_in_place(clip, destination):
            self.ui.labelDestination.setText(
                "Saving updates this clip's record in place.")
        else:
            self.ui.labelDestination.setText(
                f"Saving moves this clip to: {destination.relative_path}")

    def _on_tag_edited(self):
        self._refresh_destination()

    # -- saving ------------------------------------------------------

    def on_save(self) -> bool:
        """Save the loaded clip's tags. Returns whether a save
        happened."""
        clip = self._current_clip()
        if clip is None:
            return False
        missing = missing_required_tags(self._form_tags())
        if missing:
            self.ui.tagForm.refresh_required_fields()
            QMessageBox.warning(
                self, "Missing tags",
                "This clip is missing required tags: "
                + ", ".join(missing))
            return False
        tags = self._form_tags()
        try:
            destination = plan_retag(
                clip=clip, tags=tags,
                folder_scheme=self._folder_scheme,
                filename_scheme=self._filename_scheme,
                catalog=self._catalog,
            )
        except ExportPlanError as error:
            QMessageBox.warning(
                self, "That clip cannot be saved", str(error))
            return False
        try:
            record = execute_retag(clip, destination, tags)
        except (OSError, ValueError) as error:
            log_exception("saving the clip failed", error)
            QMessageBox.warning(
                self, "The clip could not be saved", str(error))
            return False
        self._note_confirmed_tags(tags)
        self._select_after_walk = record.clip_id
        # The status is reported once the walk this save triggers
        # has re-selected the clip, not here: a real thread is still
        # walking, and the re-selection reloads the clip, which
        # clears the status label. Deferred, it lands after that.
        self._move_report = (clip, destination)
        self._begin_walk(rebuild=True)
        return True

    def _note_confirmed_tags(self, tags) -> None:
        """Record the values the user just confirmed, the queue's
        commit points: the nine suggestable tags go into the
        vocabulary, and the form's most-recently-used order follows."""
        suggested = {
            key: value
            for key, value in tags.items()
            if key in SUGGESTED_TAG_FIELDS
        }
        reordered = self.ui.tagForm.note_recent_tags(suggested)
        if record_use(suggested, self.ui.tagForm.vocabulary_path) or reordered:
            self.ui.tagForm.refresh_combos()

    def _report_move(self, clip, destination):
        """Say where the clip now lives, in the status label."""
        if retag_is_in_place(clip, destination):
            self.ui.labelStatus.setText(f"Saved {clip.relative_path}.")
            return
        self.ui.labelStatus.setText(
            f"Saved. Moved {clip.relative_path} → "
            f"{destination.relative_path}")

    # -- playback ----------------------------------------------------

    def on_play_pause(self):
        if self._bridge is None:
            return
        self._bridge.toggle_play()

    def _on_scrub_began(self):
        self._scrubbing = True

    def _on_scrub_ended(self):
        self._scrubbing = False

    def _on_seek(self):
        if self._bridge is None:
            return
        self._bridge.seek_exact(self.ui.sliderPosition.value())

    def _on_position_changed(self, seconds):
        if self._scrubbing:
            return
        self.ui.sliderPosition.setValue(int(seconds))
        self.ui.labelPosition.setText(_format_time(seconds))

    def _on_duration_changed(self, seconds):
        self.ui.sliderPosition.setRange(0, int(seconds))

    def _on_paused_changed(self, paused):
        style = self.style()
        if paused:
            self.ui.buttonPlay.setIcon(
                style.standardIcon(QStyle.SP_MediaPlay))
            self.ui.buttonPlay.setText("Play")
        else:
            self.ui.buttonPlay.setIcon(
                style.standardIcon(QStyle.SP_MediaPause))
            self.ui.buttonPlay.setText("Pause")

    def _open_video(self, path):
        """Build a player for `path` in the video frame.

        A player that fails to build is logged and the window carries
        on with playback dead — the queue's pattern — because a clip
        that will not preview is still a clip whose tags can be
        edited and saved.
        """
        self._close_video()
        try:
            player = create_mpv_player(self.ui.videoContainer)
            self._bridge = MpvBridge(player)
            self._bridge.positionChanged.connect(
                self._on_position_changed)
            self._bridge.durationChanged.connect(
                self._on_duration_changed)
            self._bridge.pauseChanged.connect(self._on_paused_changed)
            self._bridge.load_file(path)
        except Exception as error:
            log_exception("the library manager could not open a player",
                          error)
            self._close_video()
            self.ui.labelPosition.setText("no preview")

    def _close_video(self):
        if self._bridge is not None:
            self._bridge.shutdown()
            self._bridge = None

    # -- leaving -----------------------------------------------------

    def on_back_to_menu(self):
        self.close()

    def closeEvent(self, event):
        if self._thread is not None:
            # The walk is still running. Ask it to stop and refuse
            # the close: the thread's teardown hangs off its
            # `finished` signal, and a `QThread` destroyed while it
            # is still running aborts the whole process.
            if self._cancel is not None:
                self._cancel.set()
            event.ignore()
            return
        self._close_video()
        super().closeEvent(event)


def _format_time(seconds: float) -> str:
    """`m:ss`, the transport's clock."""
    total = max(0, int(seconds))
    return f"{total // 60}:{total % 60:02d}"


def create(app):
    """Build the Library Manager window. Returns the window,
    unscaled.

    `app` is the process's QApplication, owned by main.py — this
    window does not make one and does not run an event loop,
    because it shares the loop with the menu and the editor. The
    shell shows the window.

    An export folder that cannot be used raises here, and the shell
    reports it rather than raising into the event loop.
    """
    return LibraryManagerWindow()
