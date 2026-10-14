"""The Library Sharing window: filter the library, share it as a zip.

Applies to: `sharing/sharing.py`, `sharing/sharingwindow.ui`,
`shared/sharing.py`.

A share is the export library, or a tag-filtered part of it, as a
zip a recipient drops into their `import/` folder. This window is
the Library Manager's shape with the tag form replaced by the
share filter: a read-only tree of what a share would hold on the
left, an mpv preview on the right, and the filter below the
player. Everything on screen is derived state -- the tree is
rebuilt from a fresh `build_catalog` walk filtered by the rows,
so what the tree shows is always what the share would hold.

The filter runs on the GUI thread on every row change. That is
the point: the tree is the oracle, and a row the user is typing
into is answered immediately by the clips it matches. The walk
and the zip both run on worker `QThread`s behind `LoadingDialog`s,
and both teardowns hang off `QThread.finished` (invariant 14).
Cancelling the walk closes the window -- there is nothing to show
-- and cancelling the zip leaves it up with the reason in the
status label. `closeEvent` refuses to close over either thread,
deferring the real close to the teardown that follows the thread
actually stopping.

The zip itself is `shared/sharing.py:write_share_zip`, which owns
the naming, the no-clobber numbering, and the `.part`-then-rename
write. This window owns only the threads and the screens around
it.
"""

import os
import sys
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.environment import (
    resource_path,
    setup_environment,
    share_folder,
)

SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

from PySide6.QtCore import QFile, QObject, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QStyle,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from shared.catalog import Catalog, build_catalog, problem_summary
from shared.diagnostics import log_exception
from shared.exporting import export_folder
from shared.loading import LoadingDialog
from shared.mpv import MpvBridge, create_mpv_player
from shared.scheme import canonical_tag_name
from shared.sharing import (
    filter_clips,
    share_plan,
    share_zip_name,
    write_share_zip,
)
from shared.tag_form import SUGGESTED_TAG_FIELDS
from shared.ui_loader import UiLoader, adopt_title


class FilterRow(QWidget):
    """One filter row: a namespace, a value, and a remove button.

    Both combos are editable and neither validates: the vocabulary
    is advisory (invariant 13), and a filter is a question about
    the library, not a claim the library has to answer. The
    namespace combo offers the nine suggestable fields; the value
    combo offers the catalog's values for the chosen namespace and
    is repopulated when that namespace changes.
    """

    #: The namespace or the value changed: re-run the filter.
    changed = Signal()
    #: The namespace changed: repopulate the value combo.
    namespace_changed = Signal()
    #: The remove button was clicked.
    remove_requested = Signal()

    def __init__(self, namespaces, parent=None):
        super().__init__(parent)
        self._namespace = QComboBox()
        self._namespace.setEditable(True)
        self._namespace.setInsertPolicy(QComboBox.NoInsert)
        for namespace in namespaces:
            self._namespace.addItem(namespace)
        self._value = QComboBox()
        self._value.setEditable(True)
        self._value.setInsertPolicy(QComboBox.NoInsert)
        self._configure_completer(self._namespace)
        self._configure_completer(self._value)
        self._remove = QPushButton("Remove")
        self._remove.setToolTip(
            "Remove this row. The share widens back to what it "
            "matched without it.")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._namespace, 2)
        layout.addWidget(self._value, 3)
        layout.addWidget(self._remove, 0)

        self._namespace.editTextChanged.connect(self._on_namespace_text)
        self._value.editTextChanged.connect(self._on_value_text)
        self._remove.clicked.connect(self._on_remove_clicked)

    def namespace(self) -> str:
        """The namespace this row filters on, trimmed."""
        return self._namespace.currentText().strip()

    def value(self) -> str:
        """The value this row filters on, trimmed."""
        return self._value.currentText().strip()

    def namespace_value(self) -> tuple[str, str]:
        """This row as a `(namespace, value)` rule."""
        return (self.namespace(), self.value())

    def set_namespace(self, namespace: str) -> None:
        """Set the namespace, as a pick or a typed correction."""
        self._namespace.setEditText(namespace)

    def set_value(self, value: str) -> None:
        """Set the value, as a pick or a typed correction."""
        self._value.setEditText(value)

    def set_values(self, values) -> None:
        """Repopulate the value combo, preserving typed text.

        Signals are blocked for the repopulation, so clearing and
        refilling the list cannot read as an edit and re-run the
        filter; the text the user typed is put back afterwards.
        """
        typed = self._value.currentText()
        self._value.blockSignals(True)
        self._value.clear()
        for value in values:
            self._value.addItem(value)
        self._value.setEditText(typed)
        self._value.blockSignals(False)

    def _configure_completer(self, combo) -> None:
        """Make an editable combo suggest from its own list."""
        completer = combo.completer()
        if completer is None:
            return
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        combo.lineEdit().textEdited.connect(
            lambda text: completer.complete()
            if text and combo.count() else None)

    def _on_namespace_text(self, _text):
        self.namespace_changed.emit()
        self.changed.emit()

    def _on_value_text(self, _text):
        self.changed.emit()

    def _on_remove_clicked(self):
        self.remove_requested.emit()


class ShareDialog(QDialog):
    """The share confirmation: what it holds, and what it is called.

    The filename preview is the one place the suffix is shown
    before it is applied, so the name the user types and the file
    that lands in the share folder cannot disagree.
    """

    def __init__(self, clips, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Share the library")
        self.setModal(True)
        self.setMinimumWidth(420)

        folders, videos, size = share_plan(clips)
        self._line = QLineEdit()
        self._line.setPlaceholderText("Name the share")
        self._line.setClearButtonEnabled(True)
        self._preview = QLabel()
        self._preview.setWordWrap(True)
        self._preview.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._line.textChanged.connect(self._on_text_changed)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"The share holds {folders} folder(s), "
            f"{videos} video(s), {_format_size(size)}."))
        layout.addWidget(QLabel("Name the share:"))
        layout.addWidget(self._line)
        layout.addWidget(self._preview)
        layout.addWidget(QLabel(
            "The zip is written to the share folder beside commcut. "
            "It holds each clip's video and its .cnfo record, so a "
            "recipient drops it into their import folder and imports "
            "it through the usual pipeline."))
        layout.addWidget(buttons)
        self._on_text_changed("")

    def _on_text_changed(self, _text):
        self._preview.setText(share_zip_name(self._line.text()))

    def zip_name(self) -> str:
        """The zip's filename, with the suffix applied."""
        return share_zip_name(self._line.text())


class CatalogWorker(QObject):
    """Walks the export library off the GUI thread.

    `build_catalog` is a walk that reports progress per record and
    stops between directories when the cancel event is set, so a
    walk the user gave up on does not have to finish. The outcome
    is emitted once -- `done` with the catalog, or `failed` with a
    reason -- and `finished` always follows, which is the signal
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


class ShareWorker(QObject):
    """Writes the share zip off the GUI thread.

    `write_share_zip` reports progress with a real denominator and
    checks the cancel event before each clip, so a share the user
    gave up on does not have to finish. `done` carries the path
    written, `cancelled` a cancel, and `failed` a reason;
    `finished` always follows, which is the signal the thread's
    teardown hangs off.
    """

    done = Signal(str)
    failed = Signal(str)
    cancelled = Signal()
    progressed = Signal(int, int, str)
    finished = Signal()

    def __init__(self, clips, target, cancel_event=None):
        super().__init__()
        self.clips = clips
        self.target = target
        self.cancel_event = cancel_event or threading.Event()

    @Slot()
    def run(self):
        try:
            path = write_share_zip(
                self.clips,
                self.target,
                on_progress=self._progress,
                should_cancel=self.cancel_event.is_set,
            )
            if path is None:
                self.cancelled.emit()
            else:
                self.done.emit(path)
        except Exception as error:
            log_exception("writing the share failed", error)
            self.failed.emit(f"{type(error).__name__}: {error}")
        self.finished.emit()

    def _progress(self, done, total, relative_path):
        self.progressed.emit(done, total, relative_path)


class SharingWindow(QMainWindow):
    """The export library as a share: filter it, preview it, zip it."""

    def __init__(self):
        super().__init__()
        self.root = export_folder()

        loader = UiLoader()
        self.ui = loader.load(
            QFile(resource_path("sharing", "sharingwindow.ui")), self)
        adopt_title(self, self.ui)
        self.setCentralWidget(self.ui)

        self._catalog = Catalog()
        self._filtered = ()
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
        self._scrubbing = False
        self._included_rows: list[FilterRow] = []
        self._excluded_rows: list[FilterRow] = []
        self._included_layout = self.ui.includedRows.layout()
        self._excluded_layout = self.ui.excludedRows.layout()
        self._zip_thread = None
        self._zip_worker = None
        self._zip_dialog = None
        self._zip_cancel = None
        self._close_after_zip = False

        self.ui.treeLibrary.setHeaderHidden(True)
        self.ui.buttonShare.setEnabled(False)

        self.ui.buttonPlay.clicked.connect(self.on_play_pause)
        self.ui.buttonMenu.clicked.connect(self.on_back_to_menu)
        self.ui.buttonShare.clicked.connect(self.on_share)
        self.ui.buttonAddIncluded.clicked.connect(
            lambda: self._add_row(excluded=False))
        self.ui.buttonAddExcluded.clicked.connect(
            lambda: self._add_row(excluded=True))
        self.ui.sliderPosition.sliderPressed.connect(
            self._on_scrub_began)
        self.ui.sliderPosition.sliderReleased.connect(
            self._on_scrub_ended)
        self.ui.sliderPosition.sliderReleased.connect(self._on_seek)
        self.ui.treeLibrary.itemClicked.connect(self._on_item_clicked)
        self.ui.treeLibrary.currentItemChanged.connect(
            self._on_current_item_changed)

        self._begin_walk()

    # -- the catalog walk ---------------------------------------------

    def _begin_walk(self):
        """Walk the library on a worker thread, behind a dialog.

        The window is frozen for the walk's duration -- an open has
        nothing to show yet -- and the dialog is deliberately
        parentless, because `setEnabled(False)` cascades to child
        widgets and a disabled dialog's Cancel button does nothing.
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
            "Reading the library…", cancellable=True,
            modal=False, title="Library Sharing")
        dialog.set_range(0, 0)
        dialog.canceled.connect(self._on_walk_cancel_requested)
        dialog.show()
        self._dialog = dialog

        thread.start()

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
        """The walk finished: filter what it found and show it.

        The dialog is closed before anything else, so no mpv-backed
        widget is constructed while a top-level window is foreground
        (invariant 6). A cancelled walk has nothing to show, so the
        window closes -- the close deferred to `_on_walk_stopped`,
        because the thread it owns is still running here and
        `closeEvent` refuses to close over it.
        """
        dialog, self._dialog = self._dialog, None
        if dialog is not None:
            dialog.close()
        if catalog.cancelled:
            self._close_after_walk = True
            return
        self._catalog = catalog
        self._clip = None
        self._clear_preview()
        self._apply_filter()
        self._show_problems(catalog)

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

    # -- the filter --------------------------------------------------

    def _apply_filter(self):
        """Filter the catalog's clips by the rows and rebuild the tree.

        The filter is synchronous: the tree is the oracle, and a row
        the user is typing into is answered immediately by the clips
        it matches. The Share button follows the result, so a filter
        that matches nothing cannot be shared.
        """
        includes = [row.namespace_value() for row in self._included_rows]
        excludes = [row.namespace_value() for row in self._excluded_rows]
        self._filtered = filter_clips(
            self._catalog.clips, includes, excludes)
        self._build_tree(self._filtered)
        self.ui.buttonShare.setEnabled(bool(self._filtered))

    def _add_row(self, excluded):
        """Add a filter row to the included or excluded group."""
        row = FilterRow(tuple(SUGGESTED_TAG_FIELDS))
        if excluded:
            self._excluded_rows.append(row)
            self._excluded_layout.addWidget(row)
        else:
            self._included_rows.append(row)
            self._included_layout.addWidget(row)
        row.changed.connect(self._on_filter_changed)
        row.namespace_changed.connect(
            lambda row=row: self._on_namespace_changed(row))
        row.remove_requested.connect(
            lambda row=row: self._on_remove_row(row))
        self._on_namespace_changed(row)
        self._apply_filter()

    def _on_filter_changed(self):
        """A row's content changed: re-run the filter."""
        self._apply_filter()

    def _on_namespace_changed(self, row):
        """A row's namespace changed: repopulate its value combo."""
        row.set_values(self._values_for(row.namespace()))

    def _on_remove_row(self, row):
        """A row's remove button: drop it and re-run the filter."""
        for rows in (self._included_rows, self._excluded_rows):
            if row in rows:
                rows.remove(row)
                break
        row.setParent(None)
        row.deleteLater()
        self._apply_filter()

    def _values_for(self, namespace):
        """The catalog's values for a namespace, canonicalized.

        The namespace combo is editable, so the spelling the user
        left in it is canonicalized before the catalog's index is
        asked: `tag_index` is keyed by the canonical keys the
        records hold.
        """
        canonical = canonical_tag_name(namespace) or namespace
        return self._catalog.tag_index.get(canonical, ())

    # -- the tree ----------------------------------------------------

    def _build_tree(self, clips):
        """Rebuild the sidebar tree from the filtered clips.

        The tree is derived state: it is never edited in place, only
        rebuilt, because a row change alters what a share would hold.
        Only folders that contain clips exist -- the tree is
        clip-driven -- and every level is sorted casefolded, so a
        folder named `B` sorts before a clip named `b.mp4` however
        the catalog's path order interleaved them.
        """
        self._current_leaf = None
        self.ui.treeLibrary.clear()
        self._clips_by_path = {}
        self._clip_items = {}
        folders: dict[tuple[str, ...], QTreeWidgetItem] = {}
        for clip in clips:
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
            leaf = QTreeWidgetItem(parent, [_leaf_display(components)])
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
        """Selecting a clip leaf loads its preview.

        Folder rows are not selectable, so the current item is always
        a clip leaf or nothing. There is nothing to save here, so a
        switch is never asked about.
        """
        if current is None or current.childCount() != 0:
            return
        if current is self._current_leaf:
            return
        relative = current.data(0, Qt.UserRole + 1) or ""
        clip = self._clips_by_path.get(relative)
        if clip is None:
            return
        self._current_leaf = current
        self._load_clip(clip)

    def _show_problems(self, catalog):
        self._show_problems_text(
            problem_summary(
                catalog.problems,
                consequence="so they are not in the share"))

    def _show_problems_text(self, text):
        self.ui.textProblems.setPlainText(text)
        self.ui.textProblems.setVisible(bool(text))

    # -- the selected clip -------------------------------------------

    def _load_clip(self, clip):
        """Show one clip: its name and its preview."""
        self._clip = clip
        self.ui.labelClipName.setText(clip.relative_path)
        self._open_video(clip.path)

    def _clear_preview(self):
        """Close the preview and reset the transport."""
        self._close_video()
        self.ui.labelClipName.setText("")
        self.ui.labelPosition.setText("0:00")
        self.ui.sliderPosition.setValue(0)

    # -- the share ---------------------------------------------------

    def on_share(self):
        """The Share button: confirm the name, then write the zip."""
        if not self._filtered:
            return
        dialog = ShareDialog(self._filtered, self)
        if not dialog.exec():
            return
        self._begin_zip(self._filtered, dialog.zip_name())

    def _begin_zip(self, clips, zip_name):
        """Write the share zip on a worker thread, behind a dialog.

        The dialog is non-modal and the window stays up: a share is
        a copy, not a move, so there is nothing to freeze the user
        out of while it writes. The progress bar takes a real
        denominator from the first progress report, which carries the
        clip count.
        """
        self._zip_cancel = threading.Event()
        target = os.path.join(share_folder(), zip_name)
        worker = ShareWorker(clips, target, cancel_event=self._zip_cancel)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_zip_done)
        worker.failed.connect(self._on_zip_failed)
        worker.cancelled.connect(self._on_zip_cancelled)
        worker.progressed.connect(self._on_zip_progressed)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._on_zip_stopped)
        self._zip_worker = worker
        self._zip_thread = thread

        dialog = LoadingDialog(
            "Writing the share…", cancellable=True,
            modal=False, title="Library Sharing")
        dialog.set_range(0, 0)
        dialog.canceled.connect(self._on_zip_cancel_requested)
        dialog.show()
        self._zip_dialog = dialog

        self.ui.buttonShare.setEnabled(False)
        thread.start()

    def _on_zip_cancel_requested(self):
        """The user gave up on the zip: stop it."""
        if self._zip_cancel is not None:
            self._zip_cancel.set()

    def _on_zip_progressed(self, done, total, relative_path):
        dialog = self._zip_dialog
        if dialog is None:
            return
        if total > 0:
            dialog.set_range(0, total)
            dialog.set_value(done)
        if relative_path:
            plural = "" if done == 1 else "s"
            dialog.set_message(
                f"{done} of {total} clip{plural} — {relative_path}")

    def _on_zip_done(self, path):
        """The zip was written: say where it is."""
        self.ui.labelStatus.setText(
            f"Wrote {os.path.basename(path)} to the share folder.")

    def _on_zip_failed(self, message):
        """The zip could not be written: say why."""
        self.ui.labelStatus.setText(
            f"The share could not be written:\n{message}")

    def _on_zip_cancelled(self):
        """The user gave up on the zip: say nothing was written."""
        self.ui.labelStatus.setText("The share was cancelled.")

    def _on_zip_stopped(self):
        """The zip thread has really stopped, so the guard can come off.

        Only now is it safe to clear `_zip_thread`: `closeEvent` uses
        it to refuse closing, and clearing it one signal early would
        let the window be destroyed while the thread it owns is still
        alive.
        """
        thread, self._zip_thread = self._zip_thread, None
        if thread is not None:
            thread.deleteLater()
        worker, self._zip_worker = self._zip_worker, None
        if worker is not None:
            worker.deleteLater()
        dialog, self._zip_dialog = self._zip_dialog, None
        if dialog is not None:
            dialog.deleteLater()
        self.ui.buttonShare.setEnabled(bool(self._filtered))
        if self._close_after_zip:
            self._close_after_zip = False
            self.close()

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
        on with playback dead -- the library manager's pattern --
        because a clip that will not preview is still a clip whose
        place in a share can be decided.
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
            log_exception("the sharing window could not open a player",
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
            self._close_after_walk = True
            event.ignore()
            return
        if self._zip_thread is not None:
            # The zip is still running, for the same reason.
            if self._zip_cancel is not None:
                self._zip_cancel.set()
            self._close_after_zip = True
            event.ignore()
            return
        self._close_video()
        super().closeEvent(event)


def _format_time(seconds: float) -> str:
    """`m:ss`, the transport's clock."""
    total = max(0, int(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _format_size(size: int) -> str:
    """A human-readable size, the way a file manager shows one."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 ** 2:
        return f"{size / 1024:.1f} KB"
    if size < 1024 ** 3:
        return f"{size / 1024 ** 2:.1f} MB"
    return f"{size / 1024 ** 3:.1f} GB"


def _leaf_display(components):
    """The text a clip's row shows: its filename, with any
    folder value it leads with struck from it.

    A clip's filename is a projection of its tags, and the folder
    above the row is those same tags rendered again, so a
    `Cartoon Network` clip in a `Cartoon Network` folder shows a
    name that starts with `Cartoon Network` -- the folder said
    twice. Striking the folder values from the front of the name
    hands the horizontal room back. The row still carries the real
    path in its `Qt.UserRole + 1` data, so nothing that reads the
    tree is affected, and the clip's full path is the name label
    above the preview.

    Only a *leading* value is struck, and only when something is
    left: a name that is nothing but its folder values stays whole
    rather than showing an empty row, and a value that appears
    mid-name is left alone.
    """
    stem, extension = os.path.splitext(components[-1])
    for folder in components[:-1]:
        if not stem.lower().startswith(folder.lower()):
            continue
        remainder = stem[len(folder):].lstrip(" -_.")
        if remainder:
            stem = remainder
    return stem + extension


def create(app):
    """Build the Library Sharing window. Returns the window,
    unscaled.

    `app` is the process's QApplication, owned by main.py -- this
    window does not make one and does not run an event loop,
    because it shares the loop with the menu and the editor. The
    shell shows the window.

    An export folder that cannot be used raises here, and the shell
    reports it rather than raising into the event loop.
    """
    return SharingWindow()
