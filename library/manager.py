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

from PySide6.QtCore import (
    QEvent,
    QFile,
    QObject,
    QPoint,
    QThread,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
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
    DuplicateScanResult,
    align_misaligned_clips,
    execute_bulk_retag,
    execute_retag,
    find_duplicate_clip_ids,
    find_misaligned_clips,
    plan_bulk_retag,
    plan_retag,
    retag_is_in_place,
)
from shared.session import shell
from shared.tag_form import (
    SUGGESTED_TAG_FIELDS,
    TAG_FIELDS,
    TagForm,
    field_change_signal,
    field_text,
    set_field_text,
)
from shared.tree_view import (
    build_tree,
    clip_items,
    set_subtree_checks,
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


class MaintenanceScanWorker(QObject):
    """Scans the library for clips whose path doesn't match their tags.

    `find_misaligned_clips` is a per-clip render-and-compare over an
    in-memory catalog — CPU-bound but not destructive — and it reports
    progress per clip and stops whenever the cancel event is set.
    The outcome is emitted once — `done` with a `ScanResult`, or
    `failed` with a reason — and `finished` always follows, which is
    the signal the thread's teardown hangs off.
    """

    done = Signal(object)
    failed = Signal(str)
    progressed = Signal(int, str)
    finished = Signal()

    def __init__(self, catalog, folder_scheme, filename_scheme,
                 cancel_event=None):
        super().__init__()
        self.catalog = catalog
        self.folder_scheme = folder_scheme
        self.filename_scheme = filename_scheme
        self.cancel_event = cancel_event or threading.Event()

    @Slot()
    def run(self):
        try:
            result = find_misaligned_clips(
                self.catalog, self.folder_scheme, self.filename_scheme,
                on_progress=self._progress,
                should_cancel=self.cancel_event.is_set,
            )
            self.done.emit(result)
        except Exception as error:
            log_exception("scanning the library failed", error)
            self.failed.emit(f"{type(error).__name__}: {error}")
        self.finished.emit()

    def _progress(self, clips_found, relative_path):
        self.progressed.emit(clips_found, relative_path)


class MaintenanceDuplicateScanWorker(QObject):
    """Scans the library for clips that share the same clip ID.

    `find_duplicate_clip_ids` groups clips by their record's clip ID and
    reports any ID carried by more than one clip. It is read-only and
    reports progress per clip checked. The outcome is emitted once —
    `done` with a `DuplicateScanResult`, or `failed` with a reason — and
    `finished` always follows, which is the signal the thread's teardown
    hangs off.
    """

    done = Signal(object)
    failed = Signal(str)
    progressed = Signal(int, str)
    finished = Signal()

    def __init__(self, catalog, cancel_event=None):
        super().__init__()
        self.catalog = catalog
        self.cancel_event = cancel_event or threading.Event()

    @Slot()
    def run(self):
        try:
            result = find_duplicate_clip_ids(
                self.catalog,
                on_progress=self._progress,
                should_cancel=self.cancel_event.is_set,
            )
            self.done.emit(result)
        except Exception as error:
            log_exception("scanning for duplicate clip IDs failed", error)
            self.failed.emit(f"{type(error).__name__}: {error}")
        self.finished.emit()

    def _progress(self, clips_checked, relative_path):
        self.progressed.emit(clips_checked, relative_path)


class MaintenanceAlignWorker(QObject):
    """Moves misaligned clips to where their tags put them.

    `align_misaligned_clips` runs the retag transaction per clip — the
    record published at the destination before the video moves — and
    reports progress per clip aligned. The outcome is emitted once —
    `done` with an `AlignmentResult`, or `failed` with a reason — and
    `finished` always follows, which is the signal the thread's
    teardown hangs off.
    """

    done = Signal(object)
    failed = Signal(str)
    progressed = Signal(int, str)
    finished = Signal()

    def __init__(self, misaligned, catalog, cancel_event=None):
        super().__init__()
        self.misaligned = misaligned
        self.catalog = catalog
        self.cancel_event = cancel_event or threading.Event()

    @Slot()
    def run(self):
        try:
            result = align_misaligned_clips(
                self.misaligned, self.catalog,
                on_progress=self._progress,
                should_cancel=self.cancel_event.is_set,
            )
            self.done.emit(result)
        except Exception as error:
            log_exception("aligning the clips failed", error)
            self.failed.emit(f"{type(error).__name__}: {error}")
        self.finished.emit()

    def _progress(self, clips_aligned, relative_path):
        self.progressed.emit(clips_aligned, relative_path)


#: The labels the pre-flight dialog shows for the nine suggestable tags,
#: matching the form's own labels.
_BULK_FIELD_LABELS = {
    "network": "Network",
    "filler_type": "Filler Type",
    "time_period": "Time Period",
    "show": "Show",
    "block": "Block",
    "year": "Year",
    "special": "Special",
    "length": "Length",
    "information": "Information",
}


class BulkRetagWorker(QObject):
    """Executes a bulk re-tag plan off the GUI thread.

    `execute_bulk_retag` is a per-clip transaction — the same
    record-published-before-video ordering the singular save uses — so it
    is safe to cancel at a clip boundary; what it already committed stays
    committed. The outcome is emitted once — `done` with the
    `BulkRetagResult`, or `failed` with a reason — and `finished` always
    follows, which is the signal the thread's teardown hangs off.
    """

    done = Signal(object)
    failed = Signal(str)
    progressed = Signal(int, str)
    finished = Signal()

    def __init__(self, plan, cancel_event=None):
        super().__init__()
        self.plan = plan
        self.cancel_event = cancel_event or threading.Event()

    @Slot()
    def run(self):
        try:
            result = execute_bulk_retag(
                self.plan,
                on_progress=self._progress,
                should_cancel=self.cancel_event.is_set,
            )
            self.done.emit(result)
        except Exception as error:
            log_exception("the bulk re-tag failed", error)
            self.failed.emit(f"{type(error).__name__}: {error}")
        self.finished.emit()

    def _progress(self, clips_retagged, relative_path):
        self.progressed.emit(clips_retagged, relative_path)


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
        self._scan_thread = None
        self._scan_worker = None
        self._scan_dialog = None
        self._scan_cancel = None
        self._scan_result = None
        self._scan_error = None
        self._close_after_scan = False
        self._align_thread = None
        self._align_worker = None
        self._align_dialog = None
        self._align_cancel = None
        self._align_result = None
        self._align_error = None
        self._close_after_align = False
        self._dup_scan_thread = None
        self._dup_scan_worker = None
        self._dup_scan_dialog = None
        self._dup_scan_cancel = None
        self._dup_scan_result = None
        self._dup_scan_error = None
        self._close_after_dup_scan = False
        self._bulk_thread = None
        self._bulk_worker = None
        self._bulk_dialog = None
        self._bulk_cancel = None
        self._bulk_result = None
        self._bulk_error = None
        self._bulk_plan = None
        self._close_after_bulk = False
        self._checked_ids = set()
        self._mode = "singular"
        self._bulk_touched = set()
        self._bulk_uniform: dict[str, str | None] = {}
        self._updating_checks = False
        self._last_press_pos = None
        self._clips_by_id: dict[str, object] = {}
        self._bulk_delta_values: dict[str, str | None] | None = None

        self.ui.tagForm.set_locks_visible(False)
        self.ui.tagForm.init_vocabulary(vocabulary_path())
        self.ui.treeLibrary.setHeaderHidden(True)
        self.ui.tabWidget.setTabText(0, "Library")
        self.ui.tabWidget.setTabText(1, "Maintenance")
        self.ui.buttonSave.setEnabled(False)

        self.ui.buttonPlay.clicked.connect(self.on_play_pause)
        self.ui.buttonSave.clicked.connect(self.on_save)
        self.ui.buttonMenu.clicked.connect(self.on_back_to_menu)
        self.ui.sliderPosition.sliderPressed.connect(
            self._on_scrub_began)
        self.ui.sliderPosition.sliderReleased.connect(
            self._on_scrub_ended)
        self.ui.sliderPosition.sliderReleased.connect(self._on_seek)
        self.ui.treeLibrary.viewport().installEventFilter(self)
        self.ui.treeLibrary.itemClicked.connect(
            lambda item, column: self._on_item_clicked(
                item, column, self._viewport_click_pos()))
        self.ui.treeLibrary.currentItemChanged.connect(
            self._on_current_item_changed)
        for namespace in TAG_FIELDS:
            field_change_signal(
                self.ui.tagForm.field(namespace)).connect(
                    self._on_tag_edited)
            field_change_signal(
                self.ui.tagForm.field(namespace)).connect(
                    lambda *_, key=namespace: self._on_bulk_field_touched(key))
        self.ui.treeLibrary.itemChanged.connect(self._on_check_changed)

        self.ui.buttonScanLibrary.clicked.connect(self.on_scan_library)
        self.ui.buttonScanDuplicateIds.clicked.connect(
            self.on_scan_duplicate_ids)

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
        would find. Only folders that contain clips exist -- the tree
        is clip-driven -- and every level shows folders first, then
        clips, each casefolded alphabetically.

        Every row is checkable (the bulk-edit selection); the walk
        discards any checked selection, because the rebuilt tree is a
        fresh set of rows.
        """
        self._exit_bulk_mode()
        self._checked_ids = set()
        self._bulk_touched = set()
        self._clips_by_id = {
            clip.record.clip_id: clip
            for clip in catalog.clips
            if clip.record.clip_id
        }
        tree = self.ui.treeLibrary
        tree.blockSignals(True)
        try:
            build_tree(
                tree, catalog.clips,
                self._clips_by_path, self._clip_items,
                checkboxes=True)
        finally:
            tree.blockSignals(False)

    def _is_folder_row(self, item):
        """Whether `item` is a folder row (folders are never selectable)."""
        return not bool(item.flags() & Qt.ItemIsSelectable)

    def _viewport_click_pos(self):
        if self._last_press_pos is not None:
            return self._last_press_pos
        tree = self.ui.treeLibrary
        return tree.viewport().mapFromGlobal(QCursor.pos())

    def eventFilter(self, obj, event):
        """Remember where the last press landed, so the row click it
        produces can tell the checkbox indicator from the row body."""
        if (obj is self.ui.treeLibrary.viewport()
                and event.type() == QEvent.Type.MouseButtonPress):
            pos = event.position()
            self._last_press_pos = QPoint(int(pos.x()), int(pos.y()))
        return super().eventFilter(obj, event)

    def _on_indicator(self, item, pos) -> bool:
        """Whether a click at viewport `pos` landed on `item`'s checkbox.

        The checkbox is the first thing in the row's content region,
        one indent past the depth of its parent — expander at depth
        insets, then the indicator.
        """
        tree = self.ui.treeLibrary
        depth = 0
        parent = item.parent()
        while parent is not None:
            depth += 1
            parent = parent.parent()
        indent = tree.indentation()
        width = self.style().pixelMetric(
            QStyle.PixelMetric.PM_IndicatorWidth, None, tree)
        x0 = indent * (depth + 1)
        return pos.x() >= x0 and pos.x() < x0 + width + 4

    def _toggle_leaf(self, leaf):
        """Flip a leaf's check — the whole of what a clip-row click
        means in bulk mode."""
        state = leaf.checkState(0)
        leaf.setCheckState(
            0, Qt.Unchecked if state == Qt.Checked else Qt.Checked)

    def _on_item_clicked(self, item, column, pos=None):
        """A click on a row.

        The checkbox indicator toggles the check itself, and the row
        does not toggle it — whether the row is a folder or a clip.
        itemClicked carries no coordinates, so the position comes from
        the cursor unless the caller passes one in. Folder rows expand
        or collapse on any other click; a clip leaf in bulk mode toggles
        its check on any other click. A singular click on a clip loads
        nothing here, because the current-item machinery does it.
        """
        on_indicator = pos is not None and self._on_indicator(item, pos)
        if self._is_folder_row(item):
            if on_indicator:
                return
            item.setExpanded(not item.isExpanded())
            return
        if self._mode == "bulk":
            if on_indicator:
                return
            self._toggle_leaf(item)

    def _update_all_folder_checks(self, item):
        """Recompute every folder row's check from its descendants,
        deepest folders first.

        Folders whose state Qt or the application just set to partial
        are not a command to touch their children; this function is the
        only writer of parent state once the leaves are set.
        """
        for index in range(item.childCount()):
            self._update_all_folder_checks(item.child(index))
        if not self._is_folder_row(item) or item.childCount() == 0:
            return
        states = [
            item.child(index).checkState(0)
            for index in range(item.childCount())
        ]
        if all(state == Qt.Checked for state in states):
            new = Qt.Checked
        elif all(state == Qt.Unchecked for state in states):
            new = Qt.Unchecked
        else:
            new = Qt.PartiallyChecked
        if item.checkState(0) != new:
            item.setCheckState(0, new)

    def _refresh_all_folder_checks(self):
        for index in range(self.ui.treeLibrary.topLevelItemCount()):
            self._update_all_folder_checks(
                self.ui.treeLibrary.topLevelItem(index))

    def _on_check_changed(self, item, column):
        """A check state changed: cascade and re-evaluate the mode.

        Folder rows cycle to checked or unchecked and write their whole
        subtree; a partial state is a report, not a command, and moves
        nothing. Leaves carry their own state. After any change the
        folder rows recompute theirs, the selection set is rebuilt from
        the leaves, and the mode is re-evaluated — entering bulk at two
        videos toggled on, leaving it below two.
        """
        if self._updating_checks:
            return
        before = self._checked_ids
        self._updating_checks = True
        try:
            if self._is_folder_row(item):
                state = item.checkState(0)
                if state in (Qt.Checked, Qt.Unchecked):
                    set_subtree_checks(item, state == Qt.Checked)
            self._refresh_all_folder_checks()
            self._rebuild_checked_ids()
        finally:
            self._updating_checks = False
        self._sync_mode(before)

    def _rebuild_checked_ids(self):
        ids: set[str] = set()
        for leaf in clip_items(self.ui.treeLibrary):
            if leaf.checkState(0) == Qt.Checked:
                ids.add(self._selection_key(leaf))
        self._checked_ids = ids

    def _selection_key(self, leaf) -> str:
        clip_id = leaf.data(0, Qt.UserRole) or ""
        if clip_id:
            return clip_id
        return "path:" + (leaf.data(0, Qt.UserRole + 1) or "")

    def _apply_check_state(self, ids):
        """Set every leaf's check from its key, folders recomputed.

        The undo for a rejected mode transition: restore the selection
        as it was, tri-state folders and all.
        """
        ids = set(ids)
        self._updating_checks = True
        try:
            for leaf in clip_items(self.ui.treeLibrary):
                leaf.setCheckState(
                    0, Qt.Checked if self._selection_key(leaf) in ids
                    else Qt.Unchecked)
            self._refresh_all_folder_checks()
        finally:
            self._updating_checks = False
        self._checked_ids = ids

    def _checked_leaves(self):
        return [
            leaf for leaf in clip_items(self.ui.treeLibrary)
            if leaf.checkState(0) == Qt.Checked
        ]

    def _checked_clip_objects(self):
        clips = []
        for leaf in self._checked_leaves():
            relative = leaf.data(0, Qt.UserRole + 1) or ""
            clip = self._clips_by_path.get(relative)
            if clip is not None:
                clips.append(clip)
        return clips

    # -- the bulk mode -----------------------------------------------

    def _sync_mode(self, previous_ids):
        """Enter or leave bulk mode at the one-video threshold.

        Any clip toggled on is intent enough to enter bulk editing;
        unchecking the last one leaves it. Both transitions are gated
        on unsaved edits: entering gates on the singular form's pending
        edits (Save handles them, Discard keeps them), leaving gates on
        the bulk form's. A cancelled gate restores the checks as they
        were.
        """
        bulk = len(self._checked_ids) >= 1
        if self._mode == "bulk" and not bulk:
            if self._bulk_touched:
                answer = QMessageBox.warning(
                    self, "Unsaved changes",
                    "The selected clips have unsaved tag edits. "
                    "Save them before switching?",
                    QMessageBox.Save | QMessageBox.Discard
                    | QMessageBox.Cancel,
                    QMessageBox.Save)
                if answer == QMessageBox.Cancel:
                    self._apply_check_state(previous_ids)
                    return
                if answer == QMessageBox.Save:
                    # Save the selection as it was before this toggle —
                    # the toggle zeroed it, and a bulk save with no
                    # targets is not what the user asked for.
                    self._apply_check_state(previous_ids)
                    self.on_save()
                    return
            self._exit_bulk_mode()
            return
        if self._mode != "bulk" and bulk:
            if not self._prompt_unsaved():
                self._apply_check_state(previous_ids)
                return
            if len(self._checked_ids) >= 1:
                self._enter_bulk_mode()
            return
        if self._mode == "bulk":
            self._blended_feed(initial=False)
            self._update_bulk_header()

    def _enter_bulk_mode(self):
        """Switch the pane to bulk editing: the form blends the
        checked clips, the preview shuts down, and the Title field
        goes away — it is unique per clip and can never be common."""
        self._mode = "bulk"
        self._close_video()
        for widget in (self.ui.videoContainer, self.ui.buttonPlay,
                       self.ui.sliderPosition, self.ui.labelPosition,
                       self.ui.labelDestination):
            widget.hide()
        self.ui.treeLibrary.setSelectionMode(QAbstractItemView.NoSelection)
        self.ui.tagForm.field("title").setEnabled(False)
        self.ui.tagForm.refresh_required_fields(exempt=True)
        self._blended_feed(initial=True)

    def _exit_bulk_mode(self):
        """Leave bulk editing: restore the singular pane to its
        idle, nothing-loaded state."""
        if self._mode != "bulk":
            return
        self._mode = "singular"
        self._bulk_touched.clear()
        self._bulk_uniform = {}
        self._clear_blend_artifacts()
        self.ui.treeLibrary.setSelectionMode(QAbstractItemView.SingleSelection)
        for widget in (self.ui.videoContainer, self.ui.buttonPlay,
                       self.ui.sliderPosition, self.ui.labelPosition,
                       self.ui.labelDestination):
            widget.show()
        self.ui.tagForm.field("title").setEnabled(True)
        self.ui.tagForm.write_tags({})
        self.ui.labelClipName.setText("")
        self.ui.labelStatus.setText("")
        self.ui.labelDestination.setText("")
        self.ui.buttonSave.setText("Save")
        self.ui.buttonSave.setEnabled(False)
        self.ui.tagForm.refresh_required_fields(exempt=True)

    def _clear_blend_artifacts(self):
        """Unwind mixed-state placeholders and tooltips on the nine
        suggestable fields. Title exposes none of these."""
        for key in SUGGESTED_TAG_FIELDS:
            field = self.ui.tagForm.field(key)
            line = field.lineEdit()
            line.setPlaceholderText("")
            field.setToolTip("")

    def _blended_feed(self, initial=False):
        """Blend the checked clips into the form.

        A field whose clips all agree shows its common value; a field
        with ≥2 distinct values renders empty with a placeholder, and
        only the fields the user edited are ever read back. Uniform
        values that equal an already-typed edit leave the field alone,
        so re-feeding after a selection change never overwrites one.
        """
        if self._mode != "bulk":
            return
        clips = self._checked_clip_objects()
        self._bulk_uniform = {}
        for key in SUGGESTED_TAG_FIELDS:
            field = self.ui.tagForm.field(key)
            line = field.lineEdit()
            line.setPlaceholderText("")
            field.setToolTip("")
            values = {
                clip.tag_dict().get(key, "").strip() for clip in clips}
            uniform = values.pop() if len(values) == 1 else None
            self._bulk_uniform[key] = uniform
            if not initial and key in self._bulk_touched:
                continue
            field.blockSignals(True)
            set_field_text(field, uniform if uniform is not None else "")
            field.blockSignals(False)
            if uniform is None:
                line.setPlaceholderText("(multiple values)")
                distinct = sorted(values, key=str.casefold)
                tooltip = (f"{len(distinct)} different values: "
                           + ", ".join(distinct[:8]))
                if len(distinct) > 8:
                    tooltip += f" … and {len(distinct) - 8} more"
                field.setToolTip(tooltip)
        self._update_bulk_header()

    def _update_bulk_header(self):
        count = len(self._checked_ids)
        clips_word = "clip" if count == 1 else "clips"
        self.ui.labelClipName.setText(f"{count} {clips_word} selected")
        self.ui.buttonSave.setText(f"Apply to {count} {clips_word}")
        self.ui.buttonSave.setEnabled(True)

    def _on_bulk_field_touched(self, key):
        """Track which fields the user actually edited in bulk mode —
        a field reverted to its uniform value un-tracks itself."""
        if self._mode != "bulk":
            return
        field = self.ui.tagForm.field(key)
        text = field_text(field).strip()
        uniform = self._bulk_uniform.get(key)
        if uniform is not None and text == uniform:
            self._bulk_touched.discard(key)
            return
        self._bulk_touched.add(key)

    def _bulk_delta(self) -> dict[str, str | None]:
        """The touched fields' outcomes: a value, or None for removed."""
        delta: dict[str, str | None] = {}
        for key in SUGGESTED_TAG_FIELDS:
            if key not in self._bulk_touched:
                continue
            value = field_text(self.ui.tagForm.field(key)).strip()
            delta[key] = value if value else None
        return delta

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
        if self._mode == "bulk":
            # A click in bulk mode toggles the leaf's check; the
            # singular load only happens once the mode leaves bulk.
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
        self._clear_blend_artifacts()
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
        if self._mode == "bulk":
            # The form is the blended form; there is no single destination.
            return
        self._refresh_destination()

    # -- saving ------------------------------------------------------

    def on_save(self) -> bool:
        """Save the loaded clip's tags. Returns whether a save
        happened."""
        if self._mode == "bulk":
            return self._on_save_bulk()
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

    # -- bulk saving -------------------------------------------------

    def _on_save_bulk(self) -> bool:
        """Plan, confirm and run a bulk re-tag of the checked clips."""
        delta = self._bulk_delta()
        if not delta:
            self.ui.labelStatus.setText("No tags to apply.")
            return True
        clips = self._checked_clip_objects()
        try:
            plan = plan_bulk_retag(
                delta=delta, clips=clips,
                folder_scheme=self._folder_scheme,
                filename_scheme=self._filename_scheme,
                catalog=self._catalog,
            )
        except (ExportPlanError, ValueError) as error:
            QMessageBox.warning(self, "The batch cannot be saved", str(error))
            return False
        if not plan.items:
            QMessageBox.information(
                self, "Nothing to change",
                "Every selected clip already carries these tags; "
                "no record needs to be rewritten.")
            return True
        if not self._confirm_bulk_save(plan, delta):
            return False
        self._begin_bulk_run(plan, delta)
        return True

    def _confirm_bulk_save(self, plan, delta) -> bool:
        """The pre-flight: what will be written, how many will move,
        and how many will be refused — before anything touches disk."""
        lines = []
        for key, value in delta.items():
            label = _BULK_FIELD_LABELS.get(key, key)
            lines.append(f"{label} → "
                         + (value if value is not None else "(removed)"))
        counts = (f"{len(plan.items)} clips will be re-tagged: "
                  f"{plan.moves} moves, {plan.in_place} update in place")
        if plan.skipped:
            counts += (f", {plan.skipped} already carry these tags "
                       "and will be skipped")
        counts += "."
        if plan.refused:
            counts += (f"\n{len(plan.refused)} will be refused and "
                       "left alone:")
            counts += "\n" + "\n".join(
                f"{label}: {reason}"
                for label, reason in plan.refused[:10])
            if len(plan.refused) > 10:
                counts += f"\n… and {len(plan.refused) - 10} more."
        answer = QMessageBox.question(
            self, "Apply tags?",
            "\n".join([*lines, "", counts]),
            QMessageBox.Apply | QMessageBox.Cancel,
            QMessageBox.Cancel)
        return answer == QMessageBox.Apply

    def _begin_bulk_run(self, plan, delta):
        """Run the plan on a worker thread, behind a cancellable dialog."""
        self._bulk_cancel = threading.Event()
        self._bulk_delta_values = delta
        worker = BulkRetagWorker(plan, cancel_event=self._bulk_cancel)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_bulk_done)
        worker.failed.connect(self._on_bulk_failed)
        worker.progressed.connect(self._on_bulk_progressed)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._on_bulk_stopped)
        self._bulk_worker = worker
        self._bulk_thread = thread

        self.setEnabled(False)
        dialog = LoadingDialog(
            "Applying tags…", cancellable=True, modal=False,
            title="Library Manager")
        dialog.set_range(0, len(plan.items))
        dialog.canceled.connect(self._on_bulk_cancel_requested)
        dialog.show()
        self._bulk_dialog = dialog
        self._bulk_plan = plan

        thread.start()

    def _on_bulk_cancel_requested(self):
        if self._bulk_cancel is not None:
            self._bulk_cancel.set()

    def _on_bulk_progressed(self, retagged, relative_path):
        dialog = self._bulk_dialog
        if dialog is not None:
            dialog.set_message(
                f"Applying tags ({retagged} — {relative_path})")
            dialog.set_value(retagged)

    def _on_bulk_done(self, result):
        self._bulk_result = result
        self._bulk_error = None
        dialog, self._bulk_dialog = self._bulk_dialog, None
        if dialog is not None:
            dialog.close()

    def _on_bulk_failed(self, message):
        self._bulk_error = message
        self._bulk_result = None
        log(f"bulk re-tag failed: {message}")
        dialog, self._bulk_dialog = self._bulk_dialog, None
        if dialog is not None:
            dialog.close()

    def _on_bulk_stopped(self):
        """Tear the run down, report the outcome, then rebuild the
        tree with the selection dumped (the bulk edit is
        one-and-done)."""
        thread, self._bulk_thread = self._bulk_thread, None
        if thread is not None:
            thread.deleteLater()
        worker, self._bulk_worker = self._bulk_worker, None
        if worker is not None:
            worker.deleteLater()
        dialog, self._bulk_dialog = self._bulk_dialog, None
        if dialog is not None:
            dialog.deleteLater()
        self.setEnabled(True)
        self._bulk_cancel = None
        if self._close_after_bulk:
            self._close_after_bulk = False
            self._close_video()
            self.close()
            return

        plan = self._bulk_plan
        result = self._bulk_result
        error = self._bulk_error
        self._bulk_plan = None
        self._bulk_result = None
        self._bulk_error = None

        set_values = {
            key: value
            for key, value in (self._bulk_delta_values or {}).items()
            if value
        }
        if error is not None:
            QMessageBox.warning(
                self, "The bulk save could not finish", error)
        elif result is not None and plan is not None:
            self._show_bulk_summary(plan, result)
            self._note_confirmed_tags(set_values)
            saved_message = f"Saved tags on {len(result.written)} clips."

        self._bulk_delta_values = None
        self._exit_bulk_mode()
        self._checked_ids = set()
        if result is not None and plan is not None and error is None:
            self.ui.labelStatus.setText(saved_message)
        self._begin_walk(rebuild=True)

    def _show_bulk_summary(self, plan, result):
        """The after-action: what was re-tagged, and who was refused."""
        total = len(result.written)
        parts = [
            f"Re-tagged {total} of {len(plan.items)} clips:",
            f"  moved: {result.moved}",
            f"  update in place: {result.in_place}",
        ]
        if result.cancelled:
            parts.append(
                f"  cancelled after {total} clips; "
                "everything committed stays committed.")
        if plan.skipped:
            parts.append(
                f"  {plan.skipped} clips already carried these tags "
                "and were skipped.")
        refused = [*plan.refused, *result.refused]
        if refused:
            parts.append(f"{len(refused)} refused:")
            parts.extend(f"  {label}: {reason}" for label, reason in refused)
        QMessageBox.warning(self, "Bulk re-tag complete", "\n".join(parts))

    def _report_move(self, clip, destination):
        """Say where the clip now lives, in the status label."""
        if retag_is_in_place(clip, destination):
            self.ui.labelStatus.setText(f"Saved {clip.relative_path}.")
            return
        self.ui.labelStatus.setText(
            f"Saved. Moved {clip.relative_path} → "
            f"{destination.relative_path}")

    # -- maintenance ---------------------------------------------------

    def on_scan_library(self):
        """Scan the library for clips whose path doesn't match their tags."""
        if self._thread is not None:
            return
        if self._scan_thread is not None:
            return
        self._begin_scan()

    def on_scan_duplicate_ids(self):
        """Scan the library for clips that share the same clip ID."""
        if self._thread is not None:
            return
        if self._dup_scan_thread is not None:
            return
        self._begin_duplicate_scan()

    def _begin_scan(self):
        """Scan the library on a worker thread, behind a dialog.

        The window is frozen for the scan's duration, and the dialog
        is deliberately parentless, because `setEnabled(False)` cascades
        to child widgets and would disable the dialog's Cancel. The
        thread's teardown hangs off `QThread.finished` (invariant 14).
        """
        self._scan_cancel = threading.Event()
        total = len(self._catalog.clips)
        worker = MaintenanceScanWorker(
            self._catalog, self._folder_scheme,
            self._filename_scheme, cancel_event=self._scan_cancel)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_scan_done)
        worker.failed.connect(self._on_scan_failed)
        worker.progressed.connect(self._on_scan_progressed)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._on_scan_stopped)
        self._scan_worker = worker
        self._scan_thread = thread

        self.setEnabled(False)
        dialog = LoadingDialog(
            "Scanning the library…", cancellable=True, modal=False,
            title="Library Manager")
        dialog.set_range(0, total)
        dialog.canceled.connect(self._on_scan_cancel_requested)
        dialog.show()
        self._scan_dialog = dialog

        thread.start()

    def _begin_duplicate_scan(self):
        """Scan the library for duplicate clip IDs on a worker thread, behind a
        dialog.

        The window is frozen for the scan's duration, and the dialog
        is deliberately parentless, because `setEnabled(False)` cascades
        to child widgets and would disable the dialog's Cancel. The
        thread's teardown hangs off `QThread.finished` (invariant 14).
        """
        self._dup_scan_cancel = threading.Event()
        total = len(self._catalog.clips)
        worker = MaintenanceDuplicateScanWorker(
            self._catalog, cancel_event=self._dup_scan_cancel)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_duplicate_scan_done)
        worker.failed.connect(self._on_duplicate_scan_failed)
        worker.progressed.connect(self._on_duplicate_scan_progressed)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._on_duplicate_scan_stopped)
        self._dup_scan_worker = worker
        self._dup_scan_thread = thread

        self.setEnabled(False)
        dialog = LoadingDialog(
            "Scanning for duplicate clip IDs…", cancellable=True, modal=False,
            title="Library Manager")
        dialog.set_range(0, total)
        dialog.canceled.connect(self._on_duplicate_scan_cancel_requested)
        dialog.show()
        self._dup_scan_dialog = dialog

        thread.start()

    def _on_scan_cancel_requested(self):
        """The user gave up on the scan: stop it."""
        if self._scan_cancel is not None:
            self._scan_cancel.set()

    def _on_scan_progressed(self, clips_checked, relative_path):
        """Update the scan dialog's progress and message."""
        dialog = self._scan_dialog
        if dialog is not None:
            dialog.set_message(f"Scanned {clips_checked} clip(s)")
            dialog.set_value(clips_checked)

    def _on_scan_done(self, result):
        """The scan finished: close the dialog and store the result."""
        self._scan_result = result
        self._scan_error = None
        dialog, self._scan_dialog = self._scan_dialog, None
        if dialog is not None:
            dialog.close()

    def _on_scan_failed(self, message):
        """The scan could not run. The window stays up with the reason."""
        self._scan_error = message
        self._scan_result = None
        dialog, self._scan_dialog = self._scan_dialog, None
        if dialog is not None:
            dialog.close()

    def _on_scan_stopped(self):
        """The scan thread has stopped; clean up and report results."""
        thread, self._scan_thread = self._scan_thread, None
        if thread is not None:
            thread.deleteLater()
        worker, self._scan_worker = self._scan_worker, None
        if worker is not None:
            worker.deleteLater()
        dialog, self._scan_dialog = self._scan_dialog, None
        if dialog is not None:
            dialog.deleteLater()
        self.setEnabled(True)

        if self._close_after_scan:
            self._close_after_scan = False
            self._close_video()
            self.close()
            return

        if self._scan_error is not None:
            error, self._scan_error = self._scan_error, None
            QMessageBox.warning(self, "Scan failed", error)
            return

        result = self._scan_result
        self._scan_result = None
        if result is None or result.cancelled:
            return

        self._show_scan_results(result)

    def _show_scan_results(self, result):
        """Report what the scan found, and offer to align."""
        misaligned = result.misaligned
        errors = result.errors
        clips_found = result.clips_found

        parts = []
        if misaligned:
            parts.append(
                f"{len(misaligned)} clip(s) can be aligned — their "
                f"tags render to a different path than they currently "
                f"live in.")
        if errors:
            parts.append(
                f"{len(errors)} clip(s) could not be checked — their "
                f"tags need to be fixed by hand.")
        if not misaligned and not errors:
            parts.append(f"No issues found across {clips_found} clip(s).")

        message = "\n".join(parts)

        if not misaligned:
            QMessageBox.warning(self, "Scan complete", message)
            return

        message += "\n\nAlign them now?"
        answer = QMessageBox.warning(
            self, "Scan complete", message,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer == QMessageBox.Yes:
            self._begin_align(misaligned)

    def _on_duplicate_scan_cancel_requested(self):
        """The user gave up on the duplicate ID scan: stop it."""
        if self._dup_scan_cancel is not None:
            self._dup_scan_cancel.set()

    def _on_duplicate_scan_progressed(self, clips_checked, relative_path):
        """Update the duplicate ID scan dialog's progress and message."""
        dialog = self._dup_scan_dialog
        if dialog is not None:
            dialog.set_message(f"Scanned {clips_checked} clip(s)")
            dialog.set_value(clips_checked)

    def _on_duplicate_scan_done(self, result):
        """The duplicate ID scan finished: close the dialog and store the result."""
        self._dup_scan_result = result
        self._dup_scan_error = None
        dialog, self._dup_scan_dialog = self._dup_scan_dialog, None
        if dialog is not None:
            dialog.close()

    def _on_duplicate_scan_failed(self, message):
        """The duplicate ID scan could not run. The window stays up with the reason."""
        self._dup_scan_error = message
        self._dup_scan_result = None
        dialog, self._dup_scan_dialog = self._dup_scan_dialog, None
        if dialog is not None:
            dialog.close()

    def _on_duplicate_scan_stopped(self):
        """The duplicate ID scan thread has stopped; clean up and report results."""
        thread, self._dup_scan_thread = self._dup_scan_thread, None
        if thread is not None:
            thread.deleteLater()
        worker, self._dup_scan_worker = self._dup_scan_worker, None
        if worker is not None:
            worker.deleteLater()
        dialog, self._dup_scan_dialog = self._dup_scan_dialog, None
        if dialog is not None:
            dialog.deleteLater()
        self.setEnabled(True)

        if self._close_after_dup_scan:
            self._close_after_dup_scan = False
            self._close_video()
            self.close()
            return

        if self._dup_scan_error is not None:
            error, self._dup_scan_error = self._dup_scan_error, None
            QMessageBox.warning(self, "Scan failed", error)
            return

        result = self._dup_scan_result
        self._dup_scan_result = None
        if result is None or result.cancelled:
            return

        self._show_duplicate_scan_results(result)

    def _show_duplicate_scan_results(self, result):
        """Report what the duplicate ID scan found. Flag only — no fix action."""
        groups = result.groups
        clips_found = result.clips_found

        parts = []
        if groups:
            total_dup_clips = sum(len(group.clips) for group in groups)
            parts.append(
                f"{len(groups)} clip ID(s) are shared by "
                f"{total_dup_clips} clip(s):")
            for group in groups:
                parts.append(f"  ID {group.clip_id}:")
                for clip in group.clips:
                    parts.append(f"    - {clip.relative_path}")
        if not groups:
            parts.append(
                f"No duplicate clip IDs found across {clips_found} clip(s).")

        QMessageBox.warning(self, "Duplicate Clip ID Scan Complete",
                            "\n".join(parts))

    def _begin_align(self, misaligned):
        """Align the misaligned clips on a worker thread, behind a dialog."""
        self._align_cancel = threading.Event()
        worker = MaintenanceAlignWorker(
            misaligned, self._catalog, cancel_event=self._align_cancel)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_align_done)
        worker.failed.connect(self._on_align_failed)
        worker.progressed.connect(self._on_align_progressed)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._on_align_stopped)
        self._align_worker = worker
        self._align_thread = thread

        self.setEnabled(False)
        dialog = LoadingDialog(
            "Aligning misaligned clips…", cancellable=True, modal=False,
            title="Library Manager")
        dialog.set_range(0, len(misaligned))
        dialog.canceled.connect(self._on_align_cancel_requested)
        dialog.show()
        self._align_dialog = dialog

        thread.start()

    def _on_align_cancel_requested(self):
        """The user gave up on the align: stop it."""
        if self._align_cancel is not None:
            self._align_cancel.set()

    def _on_align_progressed(self, clips_aligned, relative_path):
        """Update the align dialog's progress and message."""
        dialog = self._align_dialog
        if dialog is not None:
            dialog.set_message(f"Aligned {clips_aligned} clip(s)")
            dialog.set_value(clips_aligned)

    def _on_align_done(self, result):
        """The align finished: close the dialog and store the result."""
        self._align_result = result
        self._align_error = None
        dialog, self._align_dialog = self._align_dialog, None
        if dialog is not None:
            dialog.close()

    def _on_align_failed(self, message):
        """The align could not run. The window stays up with the reason."""
        self._align_error = message
        self._align_result = None
        dialog, self._align_dialog = self._align_dialog, None
        if dialog is not None:
            dialog.close()

    def _on_align_stopped(self):
        """The align thread has stopped; clean up and report results."""
        thread, self._align_thread = self._align_thread, None
        if thread is not None:
            thread.deleteLater()
        worker, self._align_worker = self._align_worker, None
        if worker is not None:
            worker.deleteLater()
        dialog, self._align_dialog = self._align_dialog, None
        if dialog is not None:
            dialog.deleteLater()
        self.setEnabled(True)

        if self._close_after_align:
            self._close_after_align = False
            self._close_video()
            self.close()
            return

        if self._align_error is not None:
            error, self._align_error = self._align_error, None
            QMessageBox.warning(self, "Align failed", error)
            return

        result = self._align_result
        self._align_result = None
        if result is None or result.cancelled:
            return

        self._show_align_results(result)

        if result.aligned > 0:
            self._select_after_walk = ""
            self._begin_walk(rebuild=True)

    def _show_align_results(self, result):
        """Report what the align did."""
        aligned = result.aligned
        errors = result.errors

        parts = []
        if aligned:
            parts.append(
                f"Aligned {aligned} clip(s) to where their tags "
                f"put them.")
        if errors:
            for label, reason in errors:
                parts.append(f"  {label}: {reason}")
        if not aligned and not errors:
            parts.append("No clips needed aligning.")

        QMessageBox.warning(self, "Align complete", "\n".join(parts))

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
        if self._scan_thread is not None:
            # The scan is still running. Ask it to stop and defer the
            # close to `_on_scan_stopped`, which runs once the thread
            # has really stopped.
            if self._scan_cancel is not None:
                self._scan_cancel.set()
            self._close_after_scan = True
            event.ignore()
            return
        if self._align_thread is not None:
            # The align is still running. Ask it to stop and defer the
            # close to `_on_align_stopped`, which runs once the thread
            # has really stopped.
            if self._align_cancel is not None:
                self._align_cancel.set()
            self._close_after_align = True
            event.ignore()
            return
        if self._dup_scan_thread is not None:
            # The duplicate ID scan is still running. Ask it to stop and
            # defer the close to `_on_duplicate_scan_stopped`, which runs
            # once the thread has really stopped.
            if self._dup_scan_cancel is not None:
                self._dup_scan_cancel.set()
            self._close_after_dup_scan = True
            event.ignore()
            return
        if self._bulk_thread is not None:
            # The bulk re-tag is still running. Ask it to stop and
            # defer the close to `_on_bulk_stopped`, which runs once
            # the thread has really stopped.
            if self._bulk_cancel is not None:
                self._bulk_cancel.set()
            self._close_after_bulk = True
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
