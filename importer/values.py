"""The Tagged Library Mesh: one tag value at a time, until they are all mine.

Applies to: `importer/values.py`, `importer/valueswindow.ui`, `importer/importrun.py`,
`shared/values.py`, `shared/importing.py` (`match_value`).

A library that arrived with records already has its **tags** right — the Untagged
Library Mesh decided that, or the other commcut that exported it did. What it does not
have right is the **values**: `network: CN` is correct in the vocabulary it came from
and is not the word this library uses. This window asks, once per distinct value,
whether each one is already one of mine, and if not what it should become or whether
the tag should go entirely.

One question per **value** rather than per clip is the whole design, and it is the
same asymmetry the untagged path turns on: a title is a *region* of a file name and
has to be asked per clip, while a value is a whole token shared by however many clips
carry it. `CN` on 214 clips is one question. That is also why this window is a table
and the Library Mesh Tag Editor is a queue, and why the clips this window leaves
unfinished — one missing a tag it needs — are handed back to that queue rather than
given a second per-clip pass here.

Answers are collected and written **once**, at the end, by
`shared.values.execute_translation`. Only records are rewritten; no video is opened,
moved or touched, so invariant 12 (`video present` implies `record present`) holds
throughout. Nothing is written at all if the session is not complete, because an
unanswered value resolves to itself and a half-finished run would write the folder's
own vocabulary straight back into its records.

The window makes no decisions of its own: `ValueSession.next_prompt()` answers every
question and the window renders it and reports the answer back, which is the same
division of labour `importer/mesh.py` is built on.
"""

import os
import sys
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.environment import (
    import_folder,
    resource_path,
    settings_path,
    setup_environment,
)

SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import QMainWindow, QMessageBox, QProgressDialog

from shared.catalog import build_catalog, problem_summary
from shared.diagnostics import log_exception
from shared.exporting import export_folder
from shared.session import shell
from shared.ui_loader import UiLoader, adopt_title
from shared.values import (
    ValueSession,
    execute_translation,
    plan_translation,
)
from shared.vocabulary import get_vocabulary, vocabulary_path

from importer.importrun import confirm_and_import, leftover_count


class ValueWorker(QObject):
    """Reads the import folder and the library, off the GUI thread.

    Two catalog walks, and the library is a network share as often as it is a local
    folder — the reason `importer/mesh.py` puts the same work on a worker, and the
    reason this window has one too rather than building its session in `__init__`.

    **No vocabulary sync here.** The Untagged Library Mesh already runs it when it
    opens, and it *prunes*: a value the user typed and the library does not use would
    be deleted by a second run. And the whole point of this window is that foreign
    values have not been confirmed yet, so there is nothing here worth adding to the
    file either.
    """

    ready = Signal(object, str)
    failed = Signal(str)
    advanced = Signal(str)
    finished = Signal()

    def __init__(self, root: str, library_root: str, cancel_event=None):
        super().__init__()
        self.root = root
        self.library_root = library_root
        self.cancel_event = cancel_event or threading.Event()

    @Slot()
    def run(self):
        try:
            self.advanced.emit("Reading the import folder...")
            catalog = build_catalog(self.root)
            if self.cancel_event.is_set():
                self.failed.emit("Cancelled while reading the import folder.")
                return

            self.advanced.emit("Reading your library...")
            library = build_catalog(self.library_root)
            session = ValueSession(
                self.root, catalog, library=library,
                vocabulary=get_vocabulary(vocabulary_path()),
            )
            summary = problem_summary(catalog.problems + library.problems)
        except Exception as error:  # noqa: BLE001 - reported, never raised
            log_exception("the tagged mesh could not read its input", error)
            self.failed.emit(f"{type(error).__name__}: {error}")
            return
        finally:
            self.finished.emit()
        self.ready.emit(session, summary)


def _question_text(entry, matches) -> str:
    """The one question, with its counts attached.

    Counts rather than a bare value because the answer's weight is how many clips it
    moves: `CN` on 214 clips is worth getting right and a one-clip oddity is not, and
    the screen should say so before the user answers rather than after.
    """
    where = ""
    if matches:
        where = (f" Your library calls this <b>{matches[0].value}</b> in this tag"
                 f" ({matches[0].clip_count} clip(s)).")
    return (
        f"<b>{entry.namespace}</b> is <b>{entry.value}</b> on "
        f"<b>{entry.clip_count}</b> clip(s). Is that the same thing your library "
        f"already has?{where}"
    )


def _evidence_text(matches, vocabulary) -> str:
    """The ranked, counted evidence, the same shape the folder mesh and the rules
    modal show. An empty result says the value is new here and the user has to answer — it
    is never permission to guess."""
    if not matches:
        if vocabulary is not None and vocabulary.values("network"):
            return "Nothing in your library or your tag history uses this yet, so it "\
                   "is a new value here. Type what you want to call it, remove the "\
                   "tag, or keep it as it is."
        return "Nothing in your library uses this yet, so it is a new value here."
    lines = []
    for match in matches:
        if match.in_library:
            lines.append(f"{match.clip_count} clip(s) in your library use this")
        else:
            lines.append("your tag history has used this")
    return " · ".join(lines)


class ValuesWindow(QMainWindow):
    """Every tag value in `import/`, one at a time, with the library's words on hand."""

    def __init__(self, session: ValueSession = None, root: str | None = None,
                 library_root: str | None = None):
        super().__init__()
        loader = UiLoader()
        self.ui = loader.load(
            resource_path("importer", "valueswindow.ui"), self)
        adopt_title(self, self.ui)
        self.setCentralWidget(self.ui)

        self.root = root or import_folder()
        self.library_root = library_root or export_folder()
        self.session = session
        self._intro = ""
        #: Clips the current plan would rewrite. Read by `on_import_now` so it can
        #: name what importing without applying would discard.
        self._pending_clips = 0

        self._thread: QThread | None = None
        self._worker: ValueWorker | None = None
        self._cancel = threading.Event()

        self.ui.comboValue.currentTextChanged.connect(self._refresh_translate)
        self.ui.buttonTranslate.clicked.connect(self.on_translate)
        self.ui.buttonDelete.clicked.connect(self.on_delete)
        self.ui.buttonKeep.clicked.connect(self.on_keep)
        self.ui.buttonApply.clicked.connect(self.on_apply)
        self.ui.buttonImportPlan.clicked.connect(self.on_import_now)
        self.ui.buttonClosePlan.clicked.connect(self.close)
        self.ui.buttonQueue.clicked.connect(self.on_queue)
        self.ui.buttonImport.clicked.connect(self.on_import_now)
        self.ui.buttonClose.clicked.connect(self.close)
        for widget in (self.ui.listPending, self.ui.textReport,
                       self.ui.labelStatus):
            widget.setVisible(False)

        if session is None:
            self._set_question_enabled(False)
            self._start_worker()
        else:
            self._on_ready(session, "")

    # -- loading ----------------------------------------------------------

    def _start_worker(self) -> None:
        self._cancel = threading.Event()
        worker = ValueWorker(self.root, self.library_root, self._cancel)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        # `thread.quit` is what ends the thread: `started` runs the slot inside the
        # thread's exec() loop, and a slot returning does not leave it.
        worker.finished.connect(thread.quit)
        worker.ready.connect(self._on_ready)
        worker.failed.connect(self._on_failed)
        worker.advanced.connect(self.ui.labelProgress.setText)
        thread.finished.connect(self._on_stopped)
        self._worker = worker
        self._thread = thread
        thread.start()

    def _on_ready(self, session, summary: str) -> None:
        self.session = session
        self._intro = summary
        if not session.entries():
            self._show_empty()
            return
        self._set_question_enabled(True)
        self._show_next_prompt()

    def _on_failed(self, message: str) -> None:
        self._set_question_enabled(False)
        QMessageBox.warning(self, "The Tagged Library Mesh could not start", message)
        self.ui.labelProgress.setText(message)
        self.ui.buttonClose.setVisible(True)

    def _on_stopped(self) -> None:
        """The thread has really stopped, so the close guard can come off.

        Only now is it safe to clear `_thread`: `closeEvent` uses it to refuse
        closing, and clearing it one signal early lets the window be destroyed while
        the thread it owns is still alive.
        """
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.deleteLater()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()

    def _set_question_enabled(self, enabled: bool) -> None:
        for widget in (self.ui.comboValue, self.ui.buttonTranslate,
                       self.ui.buttonDelete, self.ui.buttonKeep):
            widget.setEnabled(enabled)

    def _set_answer_row_shown(self, shown: bool) -> None:
        """Whether there is a question on screen.

        Separate from `_set_question_enabled` because a finished session is not a
        *disabled* question — there is no question, and leaving Keep As It Is sitting
        there inviting a click that cannot do anything is a screen that lies about
        what it will do.
        """
        for widget in (self.ui.labelValuePrompt, self.ui.comboValue,
                       self.ui.buttonTranslate, self.ui.buttonDelete,
                       self.ui.buttonKeep):
            widget.setVisible(shown)

    def _show_empty(self) -> None:
        """Nothing in the folder is tagged, so there is nothing to mesh."""
        self._set_question_enabled(False)
        self._set_answer_row_shown(False)
        self.ui.labelQuestion.setText(
            f"No clip in:\n{self.root}\n\nhas a record beside it, so there are no "
            f"tag values to review. Clips without records are what the Untagged "
            f"Library Mesh is for."
        )
        self.ui.labelProgress.setText("")
        self.ui.labelEvidence.setText(self._intro)
        self.ui.buttonClose.setVisible(True)

    # -- one question -----------------------------------------------------

    def _show_next_prompt(self) -> None:
        prompt = self.session.next_prompt()
        if prompt is None:
            self._show_plan()
            return
        entry = prompt.entry
        self._set_question_enabled(True)
        self._set_answer_row_shown(True)
        self.ui.labelQuestion.setText(
            _question_text(entry, prompt.matches))
        self.ui.labelEvidence.setText(_evidence_text(
            prompt.matches, self.session.vocabulary))
        self._reset_value_box(entry.namespace, prompt.matches)
        self.ui.labelProgress.setText(self._progress_text(prompt))
        self._refresh_pending_list()
        self._refresh_translate()

    def _reset_value_box(self, namespace: str, matches) -> None:
        """Repopulate the value box for a new tag.

        Reset rather than remembered: a value pre-selected from the *last* tag's
        evidence would be a suggestion about a tag the user is not looking at. The
        pre-selection is a suggestion and nothing more — the user still presses a
        button, and `Keep As It Is` is always live beside it.
        """
        was_blocked = self.ui.comboValue.blockSignals(True)
        self.ui.comboValue.clear()
        vocabulary = self.session.vocabulary
        if vocabulary is not None:
            self.ui.comboValue.addItems(vocabulary.values(namespace))
        self.ui.comboValue.blockSignals(was_blocked)
        if matches:
            self.ui.comboValue.setCurrentText(matches[0].value)

    def _progress_text(self, prompt) -> str:
        return (f"{prompt.translated} translated · {prompt.deleted} removed · "
                f"{prompt.kept} kept · {prompt.untranslated} to go")

    def _refresh_pending_list(self) -> None:
        """What is still waiting, so the user can see the size of what is left."""
        if not self.session:
            return
        self.ui.listPending.setVisible(True)
        self.ui.listPending.clear()
        for key in self.session.pending():
            entry = self.session.entry(*key)
            self.ui.listPending.addItem(f"{entry.describe()}   "
                                        f"({entry.clip_count} clip(s))")

    def _value_text(self) -> str:
        return self.ui.comboValue.currentText().strip()

    def _refresh_translate(self, *_args) -> None:
        """Use This Value needs somewhere to go.

        The "never written without being tied" rule as a UI fact: there is no way to
        press a button that translates a value onto nothing. An empty box is not an
        answer — `Remove This Tag` is.
        """
        if self.session is None:
            return
        self.ui.buttonTranslate.setEnabled(
            bool(self._value_text()) and self.session.pending() != ())

    # -- answering --------------------------------------------------------

    def _answer(self, action) -> None:
        """Run one answer and show the next question, or report a refusal in place.

        A refusal is a label, never a raise: it is a thing about the value the user
        just typed, and replacing the whole window with an error would throw away
        every answer given so far.
        """
        prompt = self.session.next_prompt()
        if prompt is None:
            return
        entry = prompt.entry
        try:
            action(entry)
        except (ValueError, KeyError) as error:
            self.ui.labelStatus.setText(str(error))
            return
        self.ui.labelStatus.setText("")
        self._show_next_prompt()

    def on_translate(self) -> None:
        """Make this value one of ours."""
        new_value = self._value_text()
        self._answer(lambda entry: self.session.translate(
            entry.namespace, entry.value, new_value))

    def on_delete(self) -> None:
        """This tag has no equivalent here, so it goes."""
        prompt = self.session.next_prompt()
        if prompt is None:
            return
        entry = prompt.entry
        answer = QMessageBox.question(
            self,
            "Remove this tag from every clip that has it?",
            f"<b>{entry.namespace}</b> will be removed from the "
            f"{entry.clip_count} clip(s) in {self.root} that carry "
            f"<b>{entry.value}</b>.\n\n"
            f"Any clip left without a tag it needs will be sent to the Library Mesh "
            f"Tag Editor afterwards, where you can fill it in by hand.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self._answer(lambda held: self.session.delete_tag(
            held.namespace, held.value))

    def on_keep(self) -> None:
        """This value is already one of ours."""
        self._answer(lambda entry: self.session.keep(
            entry.namespace, entry.value))

    # -- the plan ---------------------------------------------------------

    def _show_plan(self) -> None:
        """Every value answered: what will be written, and what will merge."""
        self._set_question_enabled(False)
        self._set_answer_row_shown(False)
        self.ui.listPending.setVisible(False)
        self.ui.labelQuestion.setText("Every tag value has been dealt with.")
        self.ui.labelEvidence.setText(self._intro)
        self.ui.labelProgress.setText("")

        plan = self._plan()
        self.ui.textReport.setPlainText(self._plan_text(plan))
        self.ui.textReport.setVisible(True)
        # **Hidden rather than disabled** when there is nothing to write. A greyed
        # button on a screen whose text says "nothing needs writing" is a control
        # that looks broken, and it used to be the only other thing here besides
        # Close — so a user who had kept every value, read that the clips were ready
        # to import, and found no way to import them.
        self.ui.buttonApply.setVisible(bool(plan and plan.clips))
        self.ui.buttonApply.setEnabled(True)
        self.ui.buttonImportPlan.setVisible(True)
        self.ui.buttonImportPlan.setDefault(True)
        self.ui.buttonClosePlan.setVisible(True)
        #: What Import Now would discard, so the confirmation can name it.
        self._pending_clips = len(plan.clips) if plan else 0

    def _plan(self):
        try:
            return plan_translation(self.session)
        except ValueError as error:
            QMessageBox.warning(self, "These changes cannot be written", str(error))
            return None

    def _plan_text(self, plan) -> str:
        lines = [self.session.report()]
        lines.append("")
        if plan is None:
            lines.append("Nothing can be written until this is resolved.")
            return "\n".join(lines)
        if not plan.clips:
            lines.append(
                "Nothing needs writing: every value you kept is already what the "
                "records say. The clips are ready to import as they are.")
        else:
            lines.append(
                f"Applying these changes will rewrite {len(plan.clips)} record(s) "
                f"in {self.root}. The videos themselves are not touched.")
            for skip in plan.skipped:
                lines.append(f"  - {skip}")
        return "\n".join(lines)

    def on_apply(self) -> None:
        """Write the records, then say what is left for the Tag Editor."""
        plan = self._plan()
        if plan is None:
            return
        if not plan.clips:
            self._show_done(0, ())
            return

        progress = QProgressDialog("Rewriting records...", None, 0, 0, self)
        progress.setWindowTitle("Tagged Library Mesh")
        progress.setParent(None)
        progress.setModal(False)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        try:
            result = execute_translation(
                plan,
                on_progress=lambda done, path: progress.setLabelText(
                    f"Rewrote {done} record(s)\n{path}"),
            )
        except Exception as error:  # noqa: BLE001 - reported, never raised
            log_exception("the translated records could not be written", error)
            QMessageBox.warning(
                self, "The changes could not be written",
                f"{type(error).__name__}: {error}\n\nNo further records were "
                f"written.")
            return
        finally:
            progress.deleteLater()

        self._show_done(result.committed, result.failed + plan.skipped)

    def _show_done(self, written: int, failures) -> None:
        """After writing: what changed, and whether the Tag Editor is needed."""
        self._set_question_enabled(False)
        self._set_answer_row_shown(False)
        for widget in (self.ui.textReport, self.ui.buttonApply,
                       self.ui.buttonImportPlan, self.ui.buttonClosePlan):
            widget.setVisible(False)
        self._pending_clips = 0
        self.ui.labelQuestion.setText(
            f"{written} record(s) rewritten in {self.root}."
            if written else
            "Nothing needed writing — every value you kept is already what the "
            "records say.")

        lines = []
        if failures:
            lines.append(f"{len(failures)} could not be written:")
            lines.extend(f"  - {skip}" for skip in failures)
            lines.append("")

        leftover = leftover_count(self.root)
        self.ui.textReport.setPlainText("\n".join(lines))
        self.ui.textReport.setVisible(bool(lines))
        self.ui.buttonQueue.setVisible(leftover > 0)
        self.ui.buttonQueue.setText(
            f"Review {leftover} Clip(s) Still Needing Tags" if leftover
            else "Review the Clips Still Needing Tags")
        self.ui.buttonImport.setVisible(True)
        self.ui.buttonClose.setVisible(True)

    # -- onward -----------------------------------------------------------

    def on_queue(self) -> None:
        """The residue, on the window that already does per-clip work.

        `QueueClip.is_already_done` asks `missing_required_tags`, so re-opening the
        queue picks up exactly the clips this window left unfinished and none of the
        ones it finished. No clip list travels with it: it re-derives from the folder,
        which is where the truth lives.
        """
        shell().open_safely('queue', root=self.root)

    def on_import_now(self) -> None:
        """Hand the folder to the importer, unchanged.

        Reachable from **both** ending screens, which is the fix for the plan screen
        being a dead end: it used to offer Apply and Close, and Import Now only
        appeared after Apply — so a user who had answered every question and decided
        they did not actually want the changes written had no route to an import at
        all.

        **Applying is never implied.** Importing under the untranslated values while
        the answers sit unwritten would put them in the library, where nothing parses
        a name back into a tag to fix them later (invariant 12). So when there is
        something to write, importing asks first and names how many records would be
        left as they are. When there is nothing to write it asks nothing, because
        there is nothing to discard.
        """
        if self._pending_clips and QMessageBox.question(
            self,
            "Import without writing these changes?",
            f"{self._pending_clips} record(s) in {self.root} would be left carrying "
            f"the values they arrived with, and the {len(self.session.entries())} "
            f"answer(s) given here would not be written.\n\n"
            f"Anything already imported cannot be changed this way later — the tags "
            f"live in the library, not in a name.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        confirm_and_import(self, self.root, self.library_root, settings_path())

    # -- closing ----------------------------------------------------------

    def closeEvent(self, event):
        """Refuse to close while the folder is still being read.

        A `QThread` still running when its owner is destroyed aborts the process, and
        `docs/status.md` records what that looks like: a `qFatal` with nothing in the
        log.
        """
        if self._thread is not None:
            self._cancel.set()
            event.ignore()
            return
        super().closeEvent(event)


def create(app, session: ValueSession = None, root: str | None = None,
           library_root: str | None = None):
    """Build the Tagged Library Mesh. Returns the window, unscaled; the shell shows it.

    `app` is the process's QApplication and this window neither makes one nor runs an
    event loop — it shares the loop with every other window, which is what
    `shared/session.py` requires of every builder.
    """
    return ValuesWindow(session=session, root=root, library_root=library_root)
