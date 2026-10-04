"""Running the import, and saying what it did.

Applies to: `importer/importrun.py`, `importer/queue.py`, `importer/values.py`,
`shared/importing.py`, `shared/catalog.py`.

The Library Mesh Tag Editor and the Tagged Library Mesh both end at the same place:
every clip in `import/` has a record, and the user can import them. One of them
running the import and the other not would mean two progress dialogs and two result
summaries that drift, so the screen lives here and both windows call it.

What is here is only the **presentation** — the progress dialog, the summary, and the
planning and execution calls around it. Every rule about what an import does belongs
to `shared/importing.py`, which holds no Qt type; this module adds none of its own.
"""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QProgressDialog

from shared.catalog import build_catalog
from shared.diagnostics import log, log_exception
from shared.exporting import load_export_schemes, missing_required_tags
from shared.importing import (
    REASON_ALREADY_PRESENT,
    ImportResult,
    candidates_from_catalog,
    execute_import,
    plan_import,
)


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


def _summary_body(result: ImportResult, plan, library_root: str) -> list[str]:
    """Everything the run did not do, which is the part a user cannot work out.

    Three separate lists, because they mean different things and lumping them is how
    a refusal reads as a failure: a clip **already in the library** with the same
    tags is a no-op and the run worked; a clip **refused** because that destination
    holds something different is a decision the user may want to revisit; a clip that
    **failed** is a problem. All of them are named.
    """
    refusals = [skip for skip in plan.skipped
                if skip.reason != REASON_ALREADY_PRESENT]
    body = [f"Imported {len(result.written)} clip(s) into {library_root}."]
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


def confirm_and_import(parent, root: str, library_root: str, settings_path: str):
    """Import everything in `root` that has a record, and report what happened.

    Returns the `ImportResult`, or None when there was nothing to import — answered
    with a message rather than a silent no-op, because "nothing happened" is
    indistinguishable from "it did not work" unless somebody says which.

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

    progress = QProgressDialog("Importing...", None, 0, 0, parent)
    progress.setWindowTitle("Import")
    progress.setParent(None)
    progress.setModal(False)
    progress.setMinimumDuration(0)
    progress.setAutoClose(False)
    progress.setAutoReset(False)
    try:
        plan = plan_import(
            candidates_from_catalog(catalog),
            load_export_schemes(settings_path),
            library_root,
            existing=build_catalog(library_root),
        )
        if not plan.clips:
            body = ["Nothing could be imported:"]
            body.extend(f"  - {skip}" for skip in plan.skipped)
            QMessageBox.information(parent, "Nothing to import", "\n".join(body))
            return None
        result = execute_import(
            plan,
            on_progress=lambda done, path: progress.setLabelText(
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
