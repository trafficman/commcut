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

**The transfer question is asked every time, and defaults to copy.** Not remembered,
because the safe default and the remembered one are the same answer on every run
except the ones where the user remembered something else — and on those the user is
standing right there. `move` is a deletion of the user's media from a folder they may
not have a backup of, so it says what it will do to the folder rather than leaving
the user to work it out from the word "move".
"""

from __future__ import annotations

import os

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QLabel,
    QMessageBox,
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
from shared.loading import LoadingDialog

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
        body.append(f"{len(result.already_present)} were already there with these "
                    f"tags, so there was nothing to do.")
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


def confirm_and_import(parent, root: str, library_root: str, settings_path: str):
    """Import everything in `root` that has a record, and report what happened.

    Returns the `ImportResult`, or None when there was nothing to import, the user
    cancelled the transfer question, or the run failed — all of which are answered
    with a message rather than a silent no-op, because "nothing happened" is
    indistinguishable from "it did not work" unless somebody says which.

    The transfer question comes **before** the plan is built and therefore before the
    space preflight, so `move` can never be chosen into an out-of-space failure and
    `copy` can be refused before anything is written rather than half way through.

    A failure is caught and reported rather than raised. This is called from a button
    handler, and an exception escaping one of those reaches the event loop and takes
    down whichever window raised — `Shell.open_safely` exists so a window does not
    *build* that way, and a failure after it is up has to be caught here.
    """
    catalog = build_catalog(root)
    if not catalog.clips:
        QMessageBox.information(
            parent, "Nothing to import",
            f"No clip in {root} has a readable record yet, so there is nothing to "
            f"import. A clip's tags have to be settled before it can go into your "
            f"library.")
        return None
    if catalog.problems:
        log(f"{len(catalog.problems)} record(s) in {root} could not be read, so "
            f"those clips were not imported")

    transfer = ask_transfer(parent, root)
    if transfer is None:
        return None

    progress = LoadingDialog("Importing...", cancellable=False, modal=False,
                             title="Import")
    try:
        plan = plan_import(
            candidates_from_catalog(catalog),
            load_export_schemes(settings_path),
            library_root,
            transfer=transfer,
            existing=build_catalog(library_root),
            # The boundary a `move` may not climb past when it prunes the folders it
            # emptied. Stated rather than inferred: inferring it from the candidates
            # would put the deletion boundary in the same place as the data.
            source_root=root,
        )
        if not plan.clips:
            body = ["Nothing could be imported:"]
            body.extend(f"  - {skip}" for skip in plan.skipped)
            QMessageBox.information(parent, "Nothing to import", "\n".join(body))
            return None
        result = execute_import(
            plan,
            on_progress=lambda done, path: progress.set_message(
                f"Imported {done} clip(s)\n{path}"),
        )
    except Exception as error:  # noqa: BLE001 - reported, never raised
        log_exception("the import could not be run", error)
        QMessageBox.warning(
            parent, "The import could not be run",
            f"{type(error).__name__}: {error}\n\nNothing further was written.")
        return None
    finally:
        progress.deleteLater()

    QMessageBox.information(
        parent, "Import finished",
        "\n".join(_summary_body(result, plan, library_root)))
    return result
