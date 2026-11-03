"""Running the import, and saying what it did.

Applies to: `importer/importrun.py`, `importer/queue.py`, `importer/values.py`,
`shared/importing.py`, `shared/catalog.py`.

The Library Mesh Tag Editor and the Tagged Library Mesh both end at the same place:
every clip in `import/` has a record, and the user can import them. One of them
running the import and the other not would mean two progress dialogs, two transfer
questions and two result summaries that drift, so the screen lives here and both
windows call it.

What is here is only the **presentation** — the transfer question, the progress
dialog, the summary, and the planning and execution calls around them. Every rule
about what an import does belongs to `shared/importing.py`, which holds no Qt type;
this module adds none of its own.

**The import runs off the GUI thread.** Reading the folders, planning the
destinations and copying the clips are all on a worker thread behind a progress
dialog: a run over eight hundred clips is hours of copying, and a blocked GUI
thread cannot even show the user how far along it is. The thread belongs to the
*run* rather than to the window that started it, so neither importer window grows
a thread guard, and an import survives the window being closed half way through —
a half-written library is worse than a closed window.

**The transfer question is asked every time, and defaults to copy.** Not remembered,
because the safe default and the remembered one are the same answer on every run
except the ones where the user remembered something else — and on those the user is
standing right there. `move` is a deletion of the user's media from a folder they may
not have a backup of, so it says what it will do to the folder rather than leaving
the user to work it out from the word "move".
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass

from PySide6.QtCore import (
    Q_ARG,
    QEventLoop,
    QMetaObject,
    QObject,
    Qt,
    QThread,
    Signal,
    Slot,
)
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from shared.catalog import build_catalog
from shared.diagnostics import log, log_exception
from shared.exporting import load_export_schemes, missing_required_tags
from shared.importing import (
    REASON_ALREADY_PRESENT,
    TRANSFER_COPY,
    TRANSFER_LINK,
    TRANSFER_MOVE,
    TRANSFERS,
    ImportResult,
    candidates_from_catalog,
    execute_import,
    plan_import,
)
from shared.loading import CancelOnCloseDialog
from shared.session import widget_is_alive

def leftover_count(root: str) -> int:
    """Clips in `root` whose record is still missing a tag they need.

    **Not** "how many have no record." A clip whose required tag was just removed has
    a record and is still unfinished, and treating that as done would strand it
    forever. This is the same distinction `QueueClip.is_already_done` is built on,
    asked through the same owner, `missing_required_tags`.
    """
    return sum(
        1 for clip in build_catalog(root).clips
        if missing_required_tags(clip.tag_dict())
    )


def _transfer_choices(source_root: str):
    """Build the transfer radio group.

    Split out so a test can build the real thing without exec'ing it. The wording
    is the point of this function, not a detail of it: `move` deletes the user's
    media out of a folder, and the difference between "move" meaning that and "move"
    meaning nothing to a user is the whole reason the question is asked at all.
    """
    heading = QLabel(
        f"<b>What should happen to the videos in {source_root}?</b>"
    )
    heading.setWordWrap(True)

    copy = QRadioButton(
        "Copy them into your library, and leave them where they are"
    )
    copy.setToolTip(
        "The safest option. Nothing is deleted, so a cancelled import, a wrong tag "
        "or a wrong folder can all be undone by deleting the copy."
    )

    link = QRadioButton(
        "Link them into your library, and leave them where they are"
    )
    link.setToolTip(
        "Like a copy, but takes no extra room on disk. A link cannot be made across "
        "drives or on a FAT or exFAT drive, so any clip that cannot be linked is "
        "copied instead, one clip at a time."
    )

    move = QRadioButton(
        f"Move them into your library, and empty {source_root} as it goes"
    )
    move.setToolTip(
        "The videos leave the import folder for good, and any folder this leaves "
        "empty is removed. Choose this only if the import folder holds copies you "
        "would not mind losing."
    )

    warning = QLabel(
        "Moving does not delete anything in your library. It removes the videos "
        "from the import folder, and there is no undo."
    )
    warning.setWordWrap(True)

    group = QGroupBox("Transfer")
    layout = QVBoxLayout()
    for widget in (copy, link, move):
        layout.addWidget(widget)
    layout.addSpacing(6)
    layout.addWidget(warning)
    group.setLayout(layout)

    copy.setChecked(True)
    return heading, group, (copy, link, move)


def ask_transfer(parent, source_root: str) -> str | None:
    """Ask what to do with the files in `source_root`, or None if cancelled.

    **Copy is pre-selected on every run.** Not because it is the best answer but
    because `shared/importing.py` already fixes it as the only transfer safe to
    assume — a cancelled run, a wrong tag or a wrong destination must not destroy
    the user's media — and a dialog that remembered the last answer would quietly
    un-fix that on the second import.

    Two things about the wiring here are load-bearing, and both were wrong first:

    - The buttons are connected to **`dialog.accept` / `dialog.reject`**, the bound
      methods. Connecting them to `QDialog.accept` passes the *unbound* method to
      PySide6, which calls it with nothing and raises
      `TypeError: unbound method QDialog.accept() needs an argument` — from inside a
      button handler, so it goes to the excepthook and the dialog never closes.
    - The choice is read **inside the accept handler**, not after `exec()` returns.
      The radio buttons are children of the dialog, which is a local here: reading
      `isChecked()` after `exec()` can hit a widget Python has already collected,
      which is a second way for this one function to fail on an empty folder.
    """
    heading, group, (copy, link, move) = _transfer_choices(source_root)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                               QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Import")
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancel")

    layout = QVBoxLayout()
    layout.addWidget(heading)
    layout.addWidget(group)
    layout.addWidget(buttons)

    dialog = QDialog(parent)
    dialog.setWindowTitle("Import")
    dialog.setLayout(layout)

    chosen = {"transfer": TRANSFER_COPY}

    def accept():
        for button, transfer in zip((copy, link, move), TRANSFERS):
            if button.isChecked():
                chosen["transfer"] = transfer
                break
        dialog.accept()

    buttons.accepted.connect(accept)
    buttons.rejected.connect(dialog.reject)

    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    dialog.deleteLater()
    return chosen["transfer"] if accepted else None


def _summary_body(result: ImportResult, plan, library_root: str) -> list[str]:
    """Everything the run did not do, which is the part a user cannot work out.

    Three separate lists, because they mean different things and lumping them is how
    a refusal reads as a failure: a clip **already in the library** with the same
    tags is a no-op and the run worked; a clip **refused** because that destination
    holds something different is a decision the user may want to revisit; a clip that
    **failed** is a problem. All of them are named.

    And what happened to the **import folder**, which is the half of a transfer the
    word "imported" says nothing about: a `copy` leaves every video where it was, and
    a user who chose copy and then sees the folder unchanged may reasonably wonder
    whether anything happened.
    """
    refusals = [skip for skip in plan.skipped
                if skip.reason != REASON_ALREADY_PRESENT]
    body = [f"Imported {len(result.written)} clip(s) into {library_root}."]
    body.append(_source_fate(plan))
    if result.already_present:
        body.append(f"{len(result.already_present)} were already in your library, "
                    f"so there was nothing to do.")
    if refusals:
        body.append(f"{len(refusals)} were left alone:")
        body.extend(f"  - {skip}" for skip in refusals)
    if result.failed:
        body.append(f"{len(result.failed)} failed:")
        body.extend(f"  - {skip}" for skip in result.failed)
    if result.cancelled:
        body.append("The run was cancelled. Everything it had already written was "
                    "kept; running it again finishes the rest.")
    if not result.written and not refusals and not result.failed:
        body.append("Nothing needed importing.")
    return body


def _source_fate(plan) -> str:
    """One sentence saying what the import folder looks like now."""
    if plan.transfer == TRANSFER_MOVE:
        return ("The videos were moved out of the import folder, and any folder "
                "that emptied was removed.")
    if plan.transfer == TRANSFER_LINK:
        return ("The videos are still in the import folder, and the ones in your "
                "library are links to them — deleting one leaves the other.")
    return "The videos are still in the import folder; this run copied them."


class ImportWorker(QObject):
    """Runs one import off the GUI thread, in two phases.

    Plain data in, signals out, never a widget — the shape of
    `settings/settings.py:213` and `editor/editor.py:271`. The worker holds the
    two catalogs and the schemes it read and the plan it resolved, and nothing a
    widget could reach into.

    **The two phases are what keep the transfer question where it belongs.** The
    first phase reads the import folder and the library; the second one plans and
    copies. The question is asked on the GUI thread between them, so it comes
    *after* the read (there is no point asking what to do with eight hundred
    videos that are not there) and *before* the plan (so `move` can never be
    chosen into an out-of-space failure).

    `finished` is emitted exactly once on every path, including a refused
    preflight and a failed copy, so the thread has one place that tears it down.
    The broad `except Exception` is deliberate: an unhandled exception in a
    `QThread` slot reaches PySide6's abort path, and `install_excepthook` does not
    stop it.
    """

    #: Which folder is being read, the clips read so far, and the record now
    #: being read.
    read_progress = Signal(str, int, str)
    #: The import folder has been read and holds something to import.
    read_done = Signal(object)
    #: The plan is resolved, and the copy is about to start.
    planned = Signal(object)
    #: Clips started so far, and the clip now running.
    progressed = Signal(int, str)
    #: The run finished: the `ImportResult`, and the `ImportPlan` it came from.
    done = Signal(object, object)
    #: The run could not be done, and why.
    failed = Signal(str)
    #: The import folder holds no clip with a readable record.
    nothing_to_import = Signal()
    #: The plan resolved to no clips. Carries the plan, for the skip list.
    nothing_importable = Signal(object)
    #: The read was cancelled.
    cancelled = Signal()
    #: Emitted on every path. This is the signal the thread's teardown hangs off.
    finished = Signal()

    def __init__(self, root, library_root, settings_path, cancel_event=None):
        super().__init__()
        self.root = root
        self.library_root = library_root
        self.settings_path = settings_path
        self.cancel_event = cancel_event or threading.Event()
        self._catalog = None
        self._library_catalog = None
        self._schemes = None

    @Slot()
    def read(self):
        """Phase 1: read the import folder and the library.

        The library is read here rather than at plan time, so both folders are
        read in one phase — and so the walks are off the GUI thread, which
        matters because the library is a network share as often as it is a local
        folder.
        """
        try:
            self._catalog = build_catalog(
                self.root,
                on_progress=self._read_progress("the import folder"),
                should_cancel=self.cancel_event.is_set,
            )
            self._library_catalog = build_catalog(
                self.library_root,
                on_progress=self._read_progress("your library"),
                should_cancel=self.cancel_event.is_set,
            )
            self._schemes = load_export_schemes(self.settings_path)
        except Exception as error:  # noqa: BLE001 - reported, never raised
            log_exception("the import could not be read", error)
            self.failed.emit(f"{type(error).__name__}: {error}")
            self.finished.emit()
            return

        if self.cancel_event.is_set() or self._catalog.cancelled:
            self.cancelled.emit()
            self.finished.emit()
            return
        if not self._catalog.clips:
            self.nothing_to_import.emit()
            self.finished.emit()
            return
        if self._catalog.problems:
            log(f"{len(self._catalog.problems)} record(s) in {self.root} could not "
                f"be read, so those clips were not imported")
        self.read_done.emit(self._catalog)

    def _read_progress(self, label):
        """A progress callback that says which folder it is walking.

        `build_catalog` carries no total — `os.walk` cannot know one — so the
        bar stays indeterminate until the plan is resolved.
        """
        def report(found, relative_path):
            self.read_progress.emit(label, found, relative_path)
        return report

    @Slot(str)
    def import_clips(self, transfer):
        """Phase 2: resolve the destinations, then copy the clips."""
        try:
            plan = plan_import(
                candidates_from_catalog(self._catalog),
                self._schemes,
                self.library_root,
                transfer=transfer,
                existing=self._library_catalog,
                # The boundary a `move` may not climb past when it prunes the
                # folders it emptied. Stated rather than inferred: inferring it
                # from the candidates would put the deletion boundary in the same
                # place as the data.
                source_root=self.root,
            )
        except Exception as error:  # noqa: BLE001 - reported, never raised
            log_exception("the import could not be planned", error)
            self.failed.emit(f"{type(error).__name__}: {error}")
            self.finished.emit()
            return

        if not plan.clips:
            self.nothing_importable.emit(plan)
            self.finished.emit()
            return

        self.planned.emit(plan)
        try:
            result = execute_import(
                plan,
                on_progress=self.progressed.emit,
                should_cancel=self.cancel_event.is_set,
            )
        except Exception as error:  # noqa: BLE001 - reported, never raised
            log_exception("the import could not be run", error)
            self.failed.emit(f"{type(error).__name__}: {error}")
            self.finished.emit()
            return

        self.done.emit(result, plan)
        self.finished.emit()

    @Slot()
    def abort(self):
        """The user declined the transfer question: end the run, import nothing."""
        self.finished.emit()


#: What the user chose on the summary screen. `menu` closes the window that
#: started the import, and closing it is what hands the main menu back.
ACTION_MENU = 'menu'
ACTION_STAY = 'stay'


class ImportSummaryDialog(QDialog):
    """What the import did, and the end of the session.

    Shown once the import has run, over the window that started it. Built in
    code rather than loaded from a `.ui` file, for the reason
    `editor/editor.py:ExportSummaryDialog` gives its own: it is a transient
    modal with no layout worth designing, and nothing has to be added to the
    packaged `.ui` payload for it.

    **It is the end of the session, not a notice over it.** The window behind
    has nothing left to offer — its own **Import Now** would find nothing left
    to import — so the summary offers the main menu rather than an Ok that
    drops the user back into a screen they are finished with. The window is
    *closed* rather than hidden, because closing is what brings the menu back.
    """

    def __init__(self, body: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import finished")
        self._action = None

        layout = QVBoxLayout(self)
        layout.addWidget(self._body_box(body))
        layout.addLayout(self._button_row())

    def _body_box(self, body: str) -> QPlainTextEdit:
        """The summary, in a box that can be read and copied.

        A plain text edit rather than a label, because the summary is a list of
        lines and some of them are long: a label would either clip them or
        wrap them into something the line breaks no longer mean.
        """
        box = QPlainTextEdit(body)
        box.setReadOnly(True)
        box.setMinimumSize(420, 120)
        return box

    def _button_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addStretch(1)
        # **Staying is still an offer.** The window behind has its own buttons
        # — the clips still needing tags, the report of what was written — and
        # a user who wants to look at the folder first should not have to
        # leave the app to do it.
        stay = QPushButton("Stay in this window")
        stay.clicked.connect(lambda: self._choose(ACTION_STAY))
        row.addWidget(stay)
        back = QPushButton("Back to main menu")
        back.setDefault(True)
        back.clicked.connect(lambda: self._choose(ACTION_MENU))
        row.addWidget(back)
        return row

    def _choose(self, action):
        self._action = action
        self.accept()

    def chosen_action(self) -> str:
        """The button pressed, or staying if the dialog was dismissed.

        Escape and the window close button land here too, and staying is the
        safe reading of an unasked question: the import is done either way, and
        the window behind still has its own way out.
        """
        return self._action or ACTION_STAY


@dataclass(frozen=True)
class ImportOutcome:
    """What one import run did, in the shape the summary screen shows.

    `kind` is the only field the summary branches on; the rest carry what that
    kind has to say. "done" is the only one with a result, because it is the
    only one where something was written.
    """

    kind: str
    result: object = None
    plan: object = None
    message: str = ""


class ImportRun(QObject):
    """One import run: the worker, the thread it runs on, and the dialog above them.

    **The run owns the thread, not the window that started it.** The thread is
    local to the run and the run waits for it, so an import survives the window
    that started it being closed half way through — a half-written library is
    worse than a closed window. It is also why neither importer window has to
    grow a thread guard of its own.

    The dialog is application-modal and parentless. Modal, so the window behind
    it cannot start a second import over the same destinations; parentless, so
    that closing the window does not take the run down with it.

    The transfer question is parented to the *dialog* rather than to the window:
    a dialog that is a child of a modal window is not blocked by it, so the
    question is still usable while the progress dialog is up.

    It is a `QObject` so that the worker's signals reach it **queued**: a bound
    method of a plain Python object would be called in the thread that emitted
    the signal, and the handlers below touch the dialog.
    """

    def __init__(self, parent, root, library_root, settings_path):
        super().__init__()
        self._parent = parent
        self._root = root
        self._library_root = library_root
        self._cancel = threading.Event()
        self._worker = ImportWorker(root, library_root, settings_path,
                                     cancel_event=self._cancel)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._dialog = CancelOnCloseDialog(
            "Reading the import folder…", cancellable=True, modal=True,
            title="Import")
        self._dialog.set_range(0, 0)
        self._loop = QEventLoop()
        self._outcome = None
        self._total = 0
        self._wire()

    def _wire(self):
        # The first phase starts with the thread: `started` runs the slot inside
        # the thread's `exec()` loop, and a slot returning does not leave it.
        self._thread.started.connect(self._worker.read)
        self._worker.read_progress.connect(self._on_read_progress)
        self._worker.read_done.connect(self._on_read_done)
        self._worker.planned.connect(self._on_planned)
        self._worker.progressed.connect(self._on_progressed)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.nothing_to_import.connect(self._on_nothing_to_import)
        self._worker.nothing_importable.connect(self._on_nothing_importable)
        self._worker.cancelled.connect(self._on_cancelled)
        self._dialog.canceled.connect(self._on_cancel_requested)
        # The thread ends when the worker says so, and the teardown hangs off
        # `thread.finished` (invariant 14): the worker's own signal is emitted
        # from inside the still-running thread, so deleting the thread on it
        # would destroy a live one.
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._loop.quit)

    def go(self):
        """Run the import, and wait for it to finish.

        The nested event loop is what makes this a *run* rather than a freeze:
        the GUI thread stays free inside it, so the dialog repaints and the
        worker's queued signals land. It quits on `thread.finished` rather than
        on the worker's signal, so the thread has really stopped by the time
        this returns.
        """
        self._dialog.show()
        self._thread.start()
        self._loop.exec()
        # The loop quits on `thread.finished`, so the thread has already stopped
        # and this returns at once. The timeout is the same one the editor's
        # pre-scan and the scanner's use: it is a formality, not the mechanism.
        self._thread.wait(5000)
        self._thread.deleteLater()
        self._dialog.close()
        self._dialog.deleteLater()

    # -- the worker's word -------------------------------------------------

    def _on_read_progress(self, label, found, relative_path):
        """Progress arrives with no total — `os.walk` cannot know one."""
        self._dialog.set_message(f"Reading {label}…\n{relative_path}")

    def _on_read_done(self, catalog):
        """The folder has been read: ask the question, then let the worker import."""
        try:
            transfer = ask_transfer(self._dialog, self._root)
        except Exception as error:  # noqa: BLE001 - reported, never raised
            log_exception("the transfer question could not be asked", error)
            transfer = None
        if transfer is None:
            self._outcome = ImportOutcome("declined")
            self._abort()
            return
        self._dialog.set_message("Importing…")
        if not self._start_import(transfer):
            self._abort()

    def _on_planned(self, plan):
        """The plan is resolved, so the bar gets a real denominator."""
        self._total = len(plan.clips)
        self._dialog.set_range(0, max(self._total, 1))

    def _on_progressed(self, done, path):
        message = f"Imported {done} of {self._total} clip(s)"
        if path:
            message += f"\n{path}"
        self._dialog.set_message(message)

    def _on_done(self, result, plan):
        self._outcome = ImportOutcome("done", result=result, plan=plan)

    def _on_failed(self, message):
        self._outcome = ImportOutcome("failed", message=message)

    def _on_nothing_to_import(self):
        self._outcome = ImportOutcome("empty")

    def _on_nothing_importable(self, plan):
        self._outcome = ImportOutcome("nothing", plan=plan)

    def _on_cancelled(self):
        self._outcome = ImportOutcome("cancelled")

    def _on_cancel_requested(self):
        """The user asked for the run to stop."""
        self._cancel.set()
        self._dialog.set_message("Cancelling…")

    # -- across the thread boundary ----------------------------------------

    def _start_import(self, transfer):
        """Hand the transfer to the worker thread, and start the second phase.

        The call is **queued**, so it runs in the worker thread rather than in
        the GUI thread that made it. A direct call would run the whole import on
        the GUI thread again, which is the thing this change exists to stop.
        """
        started = QMetaObject.invokeMethod(
            self._worker, "import_clips", Qt.ConnectionType.QueuedConnection,
            Q_ARG(str, transfer))
        if not started:
            log("the import could not be started: the worker did not take the "
                "transfer")
        return started

    def _abort(self):
        """End the run without importing anything."""
        stopped = QMetaObject.invokeMethod(
            self._worker, "abort", Qt.ConnectionType.QueuedConnection)
        if not stopped:
            # The worker did not take it, so the thread is still waiting for
            # something. Stopping the thread directly is the only way out.
            self._thread.quit()

    # -- the report --------------------------------------------------------

    def report(self):
        """Say what the run did, and hand back its result."""
        outcome = self._outcome
        # The run can outlive the window that started it, so the summary's parent
        # has to be checked rather than assumed. `widget_is_alive` answers a
        # question; the widget or `None` is what the dialog wants.
        parent = self._parent
        if parent is not None and not widget_is_alive(parent):
            parent = None
        if outcome is None:
            log("the import run ended without saying what it did")
            QMessageBox.warning(
                parent, "The import could not be run",
                "The import stopped without saying what it did.\n\n"
                "Nothing further was written.")
            return None
        if outcome.kind == "done":
            self._show_finished(parent, outcome.result, outcome.plan)
            return outcome.result
        if outcome.kind == "failed":
            QMessageBox.warning(
                parent, "The import could not be run",
                f"{outcome.message}\n\nNothing further was written.")
            return None
        if outcome.kind == "empty":
            QMessageBox.information(
                parent, "Nothing to import",
                f"No clip in {self._root} has a readable record yet, so there is "
                f"nothing to import. A clip's tags have to be settled before it "
                f"can go into your library.")
            return None
        if outcome.kind == "nothing":
            body = ["Nothing could be imported:"]
            body.extend(f"  - {skip}" for skip in outcome.plan.skipped)
            QMessageBox.information(parent, "Nothing to import", "\n".join(body))
            return None
        if outcome.kind == "cancelled":
            QMessageBox.information(
                parent, "The import was cancelled",
                "The import was cancelled before anything was written.")
            return None
        # "declined" — the user cancelled the transfer question. There is
        # nothing to report: they said so themselves.
        return None

    def _show_finished(self, parent, result, plan) -> None:
        """The import is over: say what it did, and offer the way out.

        The summary is the **end of the session**. The window that started the
        import has nothing left to offer — its own **Import Now** would find
        nothing left to import — so the dialog offers the main menu, and
        choosing it closes that window, which is what brings the menu back.

        The dialog is released before the answer is acted on, so closing the
        window behind it does not destroy a dialog that is still running.
        """
        body = "\n".join(_summary_body(result, plan, self._library_root))
        dialog = ImportSummaryDialog(body, parent=parent)
        dialog.exec()
        action = dialog.chosen_action()
        dialog.deleteLater()
        if action == ACTION_MENU and parent is not None:
            parent.close()


def confirm_and_import(parent, root: str, library_root: str, settings_path: str):
    """Import everything in `root` that has a record, and report what happened.

    Returns the `ImportResult`, or None when there was nothing to import, the user
    cancelled the transfer question, or the run failed — all of which are answered
    with a message rather than a silent no-op, because "nothing happened" is
    indistinguishable from "it did not work" unless somebody says which.

    **The work happens off the GUI thread**, behind a progress dialog, because a
    run over eight hundred clips is hours of copying and a blocked GUI thread
    cannot even show the user how far along it is.

    The transfer question comes **after** the read, so it is not asked about a
    folder that turns out to hold nothing, and **before** the plan is built and
    therefore before the space preflight, so `move` can never be chosen into an
    out-of-space failure and `copy` can be refused before anything is written
    rather than half way through.

    A failure is caught and reported rather than raised. This is called from a button
    handler, and an exception escaping one of those reaches the event loop and takes
    down whichever window raised — `Shell.open_safely` exists so a window does not
    *build* that way, and a failure after it is up has to be caught here.
    """
    run = ImportRun(parent, root, library_root, settings_path)
    run.go()
    return run.report()

