"""The one screen both importer windows share: what to do with the files, and what
the summary says afterwards.

Applies to: `importer/importrun.py`, `shared/importing.py`.

`confirm_and_import` is the only place an import is started from, so it is where the
transfer question lives and where the summary's wording is decided. Both are
deliberate rather than incidental:

- The transfer is asked **every** run and defaults to `copy`. A remembered answer
  would quietly un-fix the backend's own rule that copy is the only transfer safe to
  assume.
- `move` is described by **what it does to the folder**, not by the word "move". It
  is the one option that removes the user's media, and a user who has not understood
  it cannot meaningfully consent to it.

The dialog is built and inspected rather than exec'd, for the reason
`tests/test_mesh_window.py` gives its own: a modal under `QT_QPA_PLATFORM=offscreen`
blocks on nobody and hangs the run.
"""

import pytest
from PySide6.QtWidgets import QLabel

import importer.importrun as importrun_module
from editor_stub import ensure_qapp
from shared.importing import (
    TRANSFER_COPY,
    TRANSFER_LINK,
    TRANSFER_MOVE,
    TRANSFERS,
)


@pytest.fixture(scope="module")
def qapp():
    return ensure_qapp()


@pytest.fixture
def choices(qapp, tmp_path):
    """The real widgets, so the wording under test is the wording on screen.

    `qapp` is a dependency and not a nicety: constructing a `QRadioButton` with no
    `QApplication` takes the interpreter down rather than raising, so the module
    fixture has to come first.
    """
    root = tmp_path / "import"
    root.mkdir()
    return importrun_module._transfer_choices(str(root))


def labels_in(widget) -> str:
    return " ".join(label.text() for label in widget.findChildren(QLabel))


# ---------------------------------------------------------------------------
# The question
# ---------------------------------------------------------------------------

def test_copy_is_the_only_thing_pre_selected(choices):
    """`shared/importing.py` fixes copy as the only transfer safe to assume — a
    cancelled run, a wrong tag or a wrong destination must not destroy the user's
    media — and the dialog has to agree with that on every single run rather than
    once and then remembering whatever was picked."""
    _, _, (copy, link, move) = choices

    assert (copy.isChecked(), link.isChecked(), move.isChecked()) == (
        True, False, False)


def test_copy_and_link_both_say_the_videos_stay_put(choices):
    """The two options a user is most likely to confuse, told apart by the only
    thing that differs: what they cost in disk."""
    _, _, (copy, link, move) = choices

    assert "leave them where they are" in copy.text()
    assert "leave them where they are" in link.text()
    assert "no extra room on disk" in link.toolTip(), (
        "so the difference between them is stated, rather than left to be inferred "
        "from two nearly identical labels")


def test_move_names_the_folder_it_empties_and_says_it_is_forever(choices):
    """Consent to a deletion has to be informed, and "move" on its own says neither
    from where nor whether it can be taken back."""
    _, group, (_, _, move) = choices

    assert "empty" in move.text()
    assert "no undo" in labels_in(group), (
        "the consequence is stated next to the choices, where it is read")
    assert "would not mind losing" in move.toolTip()


def test_move_names_the_actual_folder_rather_than_the_word_import(qapp, tmp_path):
    """A label reading "empty the import folder" is a claim about a folder the user
    may have redirected. The path is what makes it checkable."""
    root = tmp_path / "somewhere-else-entirely"
    root.mkdir()

    _, group, (_, _, move) = importrun_module._transfer_choices(str(root))

    assert str(root) in move.text()
    assert group is not None, "the group is what keeps these widgets alive"


def test_the_three_buttons_map_to_the_three_transfers_in_order(choices):
    """Positional pairing, and therefore order-sensitive.

    Not a detail: reordering the radio buttons without reordering the walk in
    `ask_transfer` would mean the button labelled "copy" silently moved the user's
    files.
    """
    _, _, buttons = choices

    assert len(buttons) == 3
    assert list(TRANSFERS) == [TRANSFER_COPY, TRANSFER_LINK, TRANSFER_MOVE]


def test_the_heading_names_the_folder_the_question_is_about(qapp, tmp_path):
    root = tmp_path / "import"
    root.mkdir()

    heading, _, _ = importrun_module._transfer_choices(str(root))

    assert str(root) in heading.text()
    assert "What should happen to the videos" in heading.text()


# ---------------------------------------------------------------------------
# The whole question, pressed
# ---------------------------------------------------------------------------
#
# The tests above build the real widgets but never press anything, and that is
# exactly the gap this bug lived in: the failure was in the wiring *around* the radio
# buttons, on a code path no test executed, because `exec()` blocks under
# `QT_QPA_PLATFORM=offscreen` and hangs the run. So `exec` is replaced by a function
# that actually clicks the button — which is what a user does, and which fires the
# same signal a real click fires.


def _press(monkeypatch, source_root, standard_button, pick=None):
    """Run `ask_transfer` with `exec` replaced by a real click of one button."""
    from PySide6.QtWidgets import QDialog, QDialogButtonBox, QRadioButton

    def fake_exec(dialog):
        if pick is not None:
            for button in dialog.findChildren(QRadioButton):
                if pick in button.text():
                    button.setChecked(True)
        box = dialog.findChild(QDialogButtonBox)
        box.button(standard_button).click()
        return (QDialog.DialogCode.Accepted
                if standard_button == QDialogButtonBox.StandardButton.Ok
                else QDialog.DialogCode.Rejected)

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    return importrun_module.ask_transfer(None, str(source_root))


@pytest.fixture
def import_root(tmp_path):
    root = tmp_path / "import"
    root.mkdir()
    return root


def test_pressing_import_returns_the_checked_transfer(monkeypatch, import_root):
    """The default answer.

    This one would **not** have caught the reported bug, and that is worth knowing:
    the exception happens inside a Qt signal handler, so PySide6 prints it to stderr
    and carries on — the dialog stays open, the default survives, and `copy` comes
    back as if nothing had gone wrong. Only the two tests below, which check a
    *non-default* choice, turn the silent failure into a visible one. All three stay
    for that reason.
    """
    from PySide6.QtWidgets import QDialogButtonBox

    answer = _press(monkeypatch, import_root, QDialogButtonBox.StandardButton.Ok)

    assert answer == TRANSFER_COPY, (
        "the pre-selected option is the answer when nothing was changed")


def test_pressing_import_returns_move_when_move_was_chosen(monkeypatch,
                                                          import_root):
    """The test that would have caught it.

    A wrong transfer here does not just file the wrong way round — `move` is the one
    option that deletes the user's media, so a broken wiring that silently answered
    with `copy` on a folder of eight hundred clips would leave the user believing
    their import folder had been emptied when it had not.
    """
    from PySide6.QtWidgets import QDialogButtonBox

    answer = _press(monkeypatch, import_root, QDialogButtonBox.StandardButton.Ok,
                    pick="Move them")

    assert answer == TRANSFER_MOVE


def test_pressing_import_returns_link_when_link_was_chosen(monkeypatch,
                                                          import_root):
    from PySide6.QtWidgets import QDialogButtonBox

    answer = _press(monkeypatch, import_root, QDialogButtonBox.StandardButton.Ok,
                     pick="Link them")

    assert answer == TRANSFER_LINK


def test_cancelling_the_question_returns_none(monkeypatch, import_root):
    """Cancelling is how a user backs out of an import they did not mean to start,
    so it has to be distinguishable from answering with copy."""
    from PySide6.QtWidgets import QDialogButtonBox

    assert _press(monkeypatch, import_root,
                  QDialogButtonBox.StandardButton.Cancel) is None


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------

class _Plan:
    def __init__(self, transfer):
        self.transfer = transfer


def test_the_summary_says_what_the_transfer_left_behind():
    """The half of a transfer the word "imported" says nothing about.

    A user who chose copy and then sees the folder unchanged may reasonably wonder
    whether anything happened; a user who chose move needs to know the videos are
    gone from *there*, not merely somewhere else now.
    """
    assert "still in the import folder" in (
        importrun_module._source_fate(_Plan(TRANSFER_COPY)))
    assert "links to them" in (
        importrun_module._source_fate(_Plan(TRANSFER_LINK)))


def test_the_move_summary_says_the_folders_went_too():
    """Because that is the visible part of the cleanup, and a user watching the
    folder empty needs to know it was meant to rather than half-finished."""
    fate = importrun_module._source_fate(_Plan(TRANSFER_MOVE))

    assert "moved out of the import folder" in fate
    assert "folder that emptied was removed" in fate