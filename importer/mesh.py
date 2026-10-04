"""The Untagged Library Mesh window.

Applies to: `importer/mesh.py`, `importer/meshwindow.ui`, `shared/mesh.py`
(`MeshSession`), `shared/importing.py` (`find_videos`, `sync_vocabulary`),
`shared/catalog.py` (`build_catalog`), `shared/exporting.py` (`export_folder`).

One page per folder name, rendered from whatever `MeshSession.next_prompt()`
returns. The window makes no decisions of its own: it shows a question and calls
`assign` or `reject` with the answer. `tests/test_mesh.py` therefore *is* the
design, and the tests here are about rendering it — plus the two things only a
window can do: running the vocabulary sync off the GUI thread, and asking before a
conflict is committed.

The slow work happens before the first page appears: syncing the vocabulary against
the library, walking the import folder, and walking the library to build the
evidence the dropdowns are ranked by. All three are a worker, because the library
walk is a network share as often as it is a local folder and
`docs/status.md` names that as the thing that hangs the app.

**The worker also decides which half of the import this window is.** `find_videos`
reports `has_record` per clip, and that is the one fact that separates the two modes
`docs/importing.md` describes: a clip with no `.cnfo` has nobody's idea of what it
is, and a clip with one already has an answer. So a folder where every clip is
already tagged is offered straight to the Tagged Library Mesh rather than asked about
folder names that are somebody's *rendered* folders — which carry no information
worth meshing, and whose folder names are the user's or nobody's rather than a tag's.
That branch is why the entry point is here and not in a window of its own: the scan
has already happened, off the GUI thread, and asking for it again would be the one
thing a network share cannot afford.

The window ends at a report. It imports nothing itself: the Tag Editor settles the
clips into records, and the import is offered from there.
"""

import os
import sys
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.environment import resource_path, setup_environment

SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import QMainWindow, QMessageBox

from shared.catalog import build_catalog, problem_summary, sync_vocabulary
from shared.diagnostics import log, log_exception
from shared.exporting import export_folder
from shared.importing import find_videos, import_folder
from shared.mesh import COLOURS, MESHED, REJECTED, MeshSession, namespace_choices
from shared.session import shell
from shared.ui_loader import UiLoader, adopt_title
from shared.vocabulary import get_vocabulary, record_use, vocabulary_path


#: Readable foreground/background pairs for the path bar, indexed by the matching
#: entry in `shared.mesh.COLOURS`. The colour key travels in the prompt so the
#: model can decide *which* two names must differ; the paint is a window's job.
_SEGMENT_STYLES = (
    "background:#dce9ff; color:#12305c;",
    "background:#ffe6cc; color:#6b3400;",
    "background:#d8f3dd; color:#12452a;",
    "background:#ffdcdc; color:#5c1010;",
    "background:#ece0ff; color:#3b1b6b;",
    "background:#d3f2f2; color:#0f4a4a;",
    "background:#f2e6d0; color:#4a3a1c;",
    "background:#e2e2ee; color:#2a2a44;",
)

#: Appended to a path segment's label so an answered folder reads as answered.
_STATE_MARK = {MESHED: " ✓", REJECTED: " ✗"}


class MeshWorker(QObject):
    """Syncs the vocabulary and builds the session, off the GUI thread.

    Plain data in, signals out, never a widget — the same shape as the editor's
    `ExportWorker` and the Settings window's `SyncWorker`. The broad
    `except Exception` is deliberate: an unhandled exception in a `QThread` slot
    reaches PySide6's abort path and `install_excepthook` does not stop it.

    The vocabulary sync is *shown*, not merely run. It prunes values no clip uses,
    so it cannot happen invisibly in a constructor: the user opens a wizard to look
    around and must not lose a tag they typed without being told.

    `finished` is what ends the thread, and it is emitted on *every* exit
    including the cancelled and failed ones. Without it the `QThread` never
    terminates: `started` runs this slot inside the thread's `exec()` loop, and a
    slot that returns does not leave that loop, so `thread.finished` is never
    emitted and `MeshWindow._thread` is never cleared. The window then refuses to
    close forever, and the thread is still spinning when Qt destroys it at exit.
    """

    ready = Signal(object, str, str, int)
    failed = Signal(str)
    advanced = Signal(str)
    finished = Signal()

    def __init__(self, root: str, cancel_event=None):
        super().__init__()
        self.root = root
        self.cancel_event = cancel_event or threading.Event()

    @Slot()
    def run(self):
        try:
            vocabulary = get_vocabulary(vocabulary_path())
            self.advanced.emit("Syncing the tag vocabulary with your library...")
            sync = sync_vocabulary(
                export_folder(), vocabulary,
                on_progress=lambda _found, path: self.advanced.emit(
                    f"Reading {path}"),
                should_cancel=self.cancel_event.is_set,
                pending_root=self.root,
            )
            if self.cancel_event.is_set():
                self.failed.emit("Cancelled while reading your library.")
                return

            self.advanced.emit("Reading the import folder...")
            videos = find_videos(self.root)
            # The one fact that separates the two import modes. A clip whose record
            # is already there has somebody's answer; the folder names around it are
            # how *that* library chose to render, not a tag waiting to be tied.
            untagged = [video for video in videos if not video.has_record]
            tagged = len(videos) - len(untagged)

            self.advanced.emit("Reading your library...")
            library = build_catalog(export_folder())

            session = MeshSession(self.root, untagged, library=library,
                                  vocabulary=vocabulary)
            summary = vocabulary_sync_summary(sync)
            problems = problem_summary(
                library.problems, "so they were not used as evidence:")
        except Exception as error:  # noqa: BLE001 - reported, never raised
            log_exception("the untagged mesh could not read its input", error)
            self.failed.emit(f"{type(error).__name__}: {error}")
            return
        finally:
            self.finished.emit()
        self.ready.emit(session, summary, problems, tagged)


def vocabulary_sync_summary(sync) -> str:
    """What the vocabulary sync did, in words.

    A pure function of the result, and the same words the Settings button's dialog
    uses for the same operation — one description of one behaviour, whichever
    window ran it.
    """
    if sync.skipped_prune:
        return ("The tag vocabulary was not changed: no clips were found in your "
                "library, so nothing could be compared against it.")
    lines = [f"Tag vocabulary synced from your library: {sync.clips_found} "
             f"clip(s), {sync.values_added} value(s) added."]
    if sync.values_removed:
        removed = ", ".join(f"{namespace}: {value}"
                            for namespace, value in sync.values_removed[:8])
        more = ("" if len(sync.values_removed) <= 8
                else f", and {len(sync.values_removed) - 8} more")
        lines.append(f"{len(sync.values_removed)} unused value(s) were removed: "
                     f"{removed}{more}.")
    return " ".join(lines)


class MeshWindow(QMainWindow):
    """Asks, once per folder name, what that folder means."""

    def __init__(self, root: str | None = None):
        super().__init__()
        self.ui = UiLoader().load(resource_path("importer", "meshwindow.ui"))
        adopt_title(self, self.ui)
        self.setCentralWidget(self.ui)

        self.root = root or import_folder()
        self.session: MeshSession | None = None
        self._intro = ""
        #: Clips in the folder that already had a record when it was scanned.
        self._tagged = 0

        self.ui.comboNamespace.addItems(namespace_choices())
        self.ui.comboNamespace.currentTextChanged.connect(
            self._on_namespace_changed)
        self.ui.comboValue.currentTextChanged.connect(self._refresh_assign)
        self.ui.buttonAssign.clicked.connect(self.on_assign)
        self.ui.buttonReject.clicked.connect(self.on_reject)
        self.ui.buttonClose.clicked.connect(self.close)
        self.ui.buttonQueue.clicked.connect(self.on_queue)
        self.ui.buttonValues.clicked.connect(self.on_review_values)
        self.ui.textReport.setVisible(False)
        self.ui.buttonClose.setVisible(False)
        self.ui.buttonQueue.setVisible(False)
        self.ui.buttonValues.setVisible(False)

        self._thread: QThread | None = None
        self._worker: MeshWorker | None = None
        self._cancel = threading.Event()

        self._set_question_enabled(False)
        self._start_worker()

    # -- loading ----------------------------------------------------------

    def _start_worker(self) -> None:
        self._cancel = threading.Event()
        worker = MeshWorker(self.root, cancel_event=self._cancel)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.ready.connect(self._on_ready)
        worker.failed.connect(self._on_failed)
        worker.advanced.connect(self.ui.labelProgress.setText)
        thread.finished.connect(self._on_stopped)
        self._worker = worker
        self._thread = thread
        self.ui.labelProgress.setText(f"Reading {self.root}...")
        thread.start()

    def _on_ready(self, session, summary: str, problems: str,
                  tagged: int = 0) -> None:
        self.session = session
        self._tagged = tagged
        self._intro = "\n\n".join(part for part in (summary, problems) if part)
        if not len(session.entries()):
            self._show_empty()
            return
        self._set_question_enabled(True)
        self._show_next_prompt()

    def _on_failed(self, message: str) -> None:
        self._set_question_enabled(False)
        QMessageBox.warning(
            self, "The Untagged Library Mesh could not start", message)
        self.ui.labelQuestion.setText("")
        self.ui.labelPathBar.setText("")
        self.ui.labelProgress.setText(message)
        self.ui.buttonClose.setVisible(True)

    def _on_stopped(self) -> None:
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.deleteLater()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()

    def _set_question_enabled(self, enabled: bool) -> None:
        self.ui.comboNamespace.setEnabled(enabled)
        self.ui.comboValue.setEnabled(enabled)
        self.ui.buttonAssign.setEnabled(enabled)
        self.ui.buttonReject.setEnabled(enabled)

    # -- one page ---------------------------------------------------------

    def _show_empty(self) -> None:
        """Nothing here needs a folder name meshed.

        Two quite different situations land here, and they are told apart by the one
        fact the scan produced. An **empty folder** has nothing in it at all. A
        folder where **every clip already has a record** has nothing to mesh because
        its tags are already known — and that is a job already waiting, so it is
        offered rather than reported as an absence.
        """
        self._set_question_enabled(False)
        self.ui.labelPathBar.setText("")
        if self._tagged:
            self._show_all_tagged()
            return
        self.ui.labelQuestion.setText(
            f"No videos were found in:\n{self.root}\n\n"
            f"Put some finished clips in there, or point this at another folder."
        )
        self.ui.labelEvidence.setText(self._intro)
        self.ui.labelProgress.setText("")
        self.ui.buttonClose.setVisible(True)

    def _show_all_tagged(self) -> None:
        """Every clip here already has a record: nothing to mesh, something to do.

        The front door for a library exported from another commcut. The folder names
        here are that install's *rendered* output — `Cartoon Network/2000s/Promo/` is
        three tags' worth of information in somebody else's scheme, and asking what
        each one means would produce answers about a rendering rather than about the
        clips. So the question is not asked at all.
        """
        self.ui.labelQuestion.setText(
            f"Every one of the {self._tagged} clip(s) in:\n{self.root}\n\n"
            f"already has a record beside it, so its tags are already known. What is "
            f"left is whether the values in those records are the ones your library "
            f"uses — which is one question per value, however many clips carry it."
        )
        self.ui.labelEvidence.setText(self._intro)
        self.ui.labelProgress.setText("")
        self.ui.buttonValues.setVisible(True)
        self.ui.buttonClose.setVisible(True)

    def _show_next_prompt(self) -> None:
        prompt = self.session.next_prompt()
        if prompt is None:
            self._show_report()
            return

        self._prompt = prompt
        self.ui.labelQuestion.setText(
            f"<b>{prompt.name}</b> is a folder name in this library. "
            f"What does it mean?"
        )
        self.ui.labelPathBar.setText(self._path_bar_html(prompt))
        self.ui.labelEvidence.setText(self._intro)
        self._reset_comboboxes(prompt)
        self.ui.labelProgress.setText(self._progress_text(prompt))
        self._refresh_assign()

    def _reset_comboboxes(self, prompt) -> None:
        """Repopulate both questions for the new folder name.

        Reset rather than remembered: a namespace pre-selected from the *last*
        folder's evidence would be a suggestion the user never asked for, applied
        to a name that may have nothing to do with it.

        Signals are blocked while repopulating, because `currentTextChanged` would
        otherwise repopulate the value list once per item.
        """
        was_blocked = self.ui.comboNamespace.blockSignals(True)
        self.ui.comboNamespace.clear()
        self.ui.comboNamespace.addItems(namespace_choices())
        if prompt.suggested_namespace:
            self.ui.comboNamespace.setCurrentText(prompt.suggested_namespace)
        self.ui.comboNamespace.blockSignals(was_blocked)

        self._set_value_candidates(self.ui.comboNamespace.currentText())
        if prompt.suggested_values:
            self.ui.comboValue.setCurrentText(prompt.suggested_values[0].value)
        self._refresh_assign()

    def _on_namespace_changed(self, namespace: str) -> None:
        """Repopulate the value list for the namespace just chosen.

        Ordered by namespace, so a user who changes their mind about where a folder
        name belongs sees the other namespace's values rather than a list that
        silently kept the old ones.
        """
        if not self.session or not namespace:
            return
        self._set_value_candidates(namespace)
        self._refresh_assign()

    def _set_value_candidates(self, namespace: str) -> None:
        was_blocked = self.ui.comboValue.blockSignals(True)
        self.ui.comboValue.clear()
        vocabulary = get_vocabulary(vocabulary_path())
        for value in vocabulary.values(namespace):
            self.ui.comboValue.addItem(value)
        self.ui.comboValue.blockSignals(was_blocked)

    def _value_text(self) -> str:
        return self.ui.comboValue.currentText().strip()

    def _refresh_assign(self, *_args) -> None:
        """Assign needs both halves of the answer.

        This is the "never written without being tied" rule as a UI fact: there is
        no way to press a button that meshes a folder name onto nothing.
        """
        if self.session is None:
            return
        complete = bool(self.ui.comboNamespace.currentText()) and bool(
            self._value_text())
        self.ui.buttonAssign.setEnabled(complete and self.session.pending()
                                        != ())

    def _progress_text(self, prompt) -> str:
        meshed, rejected, left = self.session.counts()
        return (f"{meshed} meshed · {rejected} not a tag · {left} to go "
                f"· {prompt.unmeshed_paths} unmeshed name(s) in this path")

    def _path_bar_html(self, prompt) -> str:
        """The chosen path, one coloured span per folder name.

        The colour is the only thing that makes two names on one path tellable
        apart, and a state mark says which are already answered — so the user can
        see where they are in a tree rather than being handed questions blind.
        """
        spans = []
        for segment in prompt.segments:
            style = _SEGMENT_STYLES[COLOURS.index(segment.colour)]
            weight = "700" if segment.is_current else "400"
            mark = _STATE_MARK.get(segment.state, "")
            spans.append(
                f'<span style="{style}">&nbsp;'
                f'<b style="font-weight:{weight}">{segment.name}</b>{mark}'
                f'&nbsp;</span>'
            )
        return "&nbsp;&nbsp;/&nbsp;&nbsp;".join(spans)

    # -- answering --------------------------------------------------------

    def on_assign(self) -> None:
        """Tie the current folder name to the namespace and value on screen."""
        if self.session is None:
            return
        prompt = self.session.next_prompt()
        if prompt is None:
            return
        namespace = self.ui.comboNamespace.currentText()
        value = self._value_text()
        if not namespace or not value:
            return

        # Asked *before* committing, so declining leaves the table untouched.
        conflict = self.session.preview_conflict(prompt.name, namespace, value)
        if conflict is not None and not self._confirm_conflict(conflict):
            return
        self.session.assign(prompt.name, namespace, value)
        self._note_confirmed_tag(namespace, value)
        self._show_next_prompt()

    def _note_confirmed_tag(self, namespace: str, value: str) -> None:
        """Record the answer the user just gave, so it can be picked next time.

        Assigning is this window's confirmation — the namespace and value are on
        screen and the press is deliberate — so the value joins
        `vocabulary.json` at that moment, the way a Stage does in the editor.
        Without it a folder name answered here has to be answered the same way
        again on the next run, which is the whole cost this feature exists to
        remove.

        The path is resolved the same way the worker resolved it, so this is the
        same cached instance the sync at open already reconciled. A file that
        cannot be written is logged and dropped: the answer is in the alias
        table either way, and the value being missing from a suggestion list is
        an inconvenience rather than a failure.
        """
        record_use({namespace: value}, vocabulary_path())

    def _confirm_conflict(self, conflict) -> bool:
        """Ask before committing a mapping that will need a person later.

        Titled by consequence rather than by category: "two folder names, one
        namespace" reads as though a namespace is something a folder takes, and a
        library legitimately has a dozen folders meaning `filler_type`. What is
        actually worth interrupting someone for is that one clip cannot hold two
        values for one tag.
        """
        answer = QMessageBox.question(
            self,
            "One clip would get two values for one tag",
            conflict.describe() + "\n\nMesh it anyway?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return answer == QMessageBox.Yes

    def on_reject(self) -> None:
        """Decide the current folder name is not a tag."""
        if self.session is None:
            return
        prompt = self.session.next_prompt()
        if prompt is None:
            return
        self.session.reject(prompt.name)
        self._show_next_prompt()

    # -- finishing --------------------------------------------------------

    def _show_report(self) -> None:
        self._set_question_enabled(False)
        self.ui.labelPathBar.setText("")
        self.ui.labelQuestion.setText("Every folder name has been dealt with.")
        self.ui.labelEvidence.setText("")
        self.ui.labelProgress.setText("")
        self.ui.textReport.setPlainText(self._report_text())
        self.ui.textReport.setVisible(True)
        self.ui.buttonQueue.setVisible(True)
        self.ui.buttonQueue.setEnabled(True)
        # Both endings offered: the queue is the next step, and stopping here is
        # legitimate — the folder answers are worth keeping even if the titles
        # are not wanted yet.
        self.ui.buttonClose.setVisible(True)

    def _report_text(self) -> str:
        """The session's report, plus what the folder held that this window ignored.

        The second half matters: in a mixed folder the clips that already had
        records were deliberately not meshed here, and a user who cannot see that
        would reasonably wonder where they went. They have not gone anywhere — the
        Tagged Library Mesh picks them up — but the screen is the only place that can
        say so.
        """
        text = self.session.report()
        if not self._tagged:
            return text
        return (
            f"{text}\n\n{self._tagged} clip(s) in this folder already had a record "
            f"and were not meshed here. Their tags are already known; the Tagged "
            f"Library Mesh will ask about their values once the untagged clips are "
            f"settled."
        )

    def on_queue(self):
        """Open the Library Mesh Tag Editor on this session.

        The alias table crosses with the window rather than being written to disk:
        it is a statement about *this* folder tree, and a saved copy would be
        stale the moment the user reorganises. Re-running this window rebuilds it
        from the tree, which is where its truth lives.
        """
        shell().open_safely('queue', session=self.session, root=self.root)

    def on_review_values(self):
        """Straight to the Tagged Library Mesh, for a folder that is all records."""
        shell().open_safely('values', root=self.root)

    def closeEvent(self, event):
        """Refuse to close while the worker is reading.

        A `QThread` still running when its owner is destroyed aborts the process,
        so there is no path that lets this window go first — the same guard the
        editor and the Settings window use.
        """
        if self._thread is not None:
            self._cancel.set()
            event.ignore()
            return
        super().closeEvent(event)


def create(app, root: str | None = None) -> MeshWindow:
    """Build the Untagged Library Mesh. Returns the window, unscaled; the shell shows it.

    `app` is the process's QApplication and this window does not make one or run an
    event loop — it shares the loop with the menu, the scanner, the editor and the
    Settings window, which is what `shared/session.py` requires of every builder.

    `root` defaults to `import/` and exists so a test can point this at a temporary
    library. It is not a control in the window: choosing a folder to import is a
    decision the import window will make, and this one is standalone.
    """
    return MeshWindow(root)