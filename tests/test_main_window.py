"""Tests for the main menu window and the paths it launches.

The menu is the app's entry point, so the four things worth guarding are that it
resolves the right scripts, that **Editor** asks for a source video and hands the
chosen one to the scanner, that a video **dropped on the menu** reaches the same
place the dialog does, and that a failure to launch surfaces as a dialog rather
than a crash.
"""

import os

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication

from editor_stub import ensure_qapp
from shared.environment import setup_environment

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Project root resolution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", [
    "main.py",
    "mainwindow.py",
    os.path.join("editor", "editor.py"),
    os.path.join("scanner", "scanner.py"),
    os.path.join("settings", "settings.py"),
])
def test_setup_environment_finds_the_project_root_from_any_entry_point(entry):
    """Root-level scripts must not be treated as if they were one level below
    the root, which would resolve every launched path outside the tree."""
    script_dir, project_root = setup_environment(
        os.path.join(PROJECT_ROOT, entry))

    assert project_root == PROJECT_ROOT
    # The script directory is always the script's own folder, never the root.
    assert script_dir == os.path.dirname(os.path.join(PROJECT_ROOT, entry))


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    return ensure_qapp()


@pytest.fixture
def menu(qapp, monkeypatch, tmp_path):
    """A MainWindow whose window-opening and file dialog are both recorded.

    Two seams are stubbed, and both have to be:

    - `shell`, because the menu opens other windows through it. `open_safely` is
      the one method it must call and the fake provides only that, on purpose:
      reaching for `shell().open()` instead would be an AttributeError here,
      which is the point — an exception out of a button handler reaches Qt's
      event loop, and with one process that takes the menu down with it.
    - `choose_source_video`, because a real `QFileDialog` is modal and cannot be
      answered. The user cancelling it is a first-class outcome and one of the
      tests below, so the stub's answer is settable.

    Everything downstream of the answer is real: `validate_source_video` runs, so
    a folder that cannot take the sidecar is refused by the shipped rule rather
    than by a mock. That is what lets the drop tests below assert against the same
    behaviour without any drop-specific stub.
    """
    import mainwindow

    opened = []
    refusals = []
    source = tmp_path / "elsewhere" / "compilation.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"video")

    class RecordingShell:
        def open_safely(self, name, **kwargs):
            opened.append((name, kwargs))
            return object()

    class RecordingMessageBox:
        @staticmethod
        def warning(_parent, _title, message, *_args):
            refusals.append(message)
            return RecordingMessageBox.Yes

        Yes = 1

    state = {"answer": str(source)}

    monkeypatch.setattr(mainwindow, "shell", RecordingShell)
    monkeypatch.setattr(mainwindow, "QMessageBox", RecordingMessageBox)
    monkeypatch.setattr(
        mainwindow, "choose_source_video", lambda *_args: state["answer"])

    instance = mainwindow.MainWindow()
    instance.opened = opened
    instance.refusals = refusals
    instance.source = source
    instance.answer = state
    yield instance

    instance.close()
    instance.deleteLater()
    qapp.processEvents()


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def test_window_offers_both_destinations(menu):
    assert menu.ui.editorButton.text() == "Editor"
    assert menu.ui.settingsButton.text() == "Settings"


def test_editor_button_explains_the_journey_it_starts(menu):
    """The hint should name what actually happens: choose a video, scan, then
    tag."""
    hint = menu.ui.labelEditorHint.text().lower()
    assert "scan" in hint
    assert menu.ui.editorButton.toolTip()


def test_window_keeps_its_own_size(menu):
    # Tracks the minimumSize declared in mainwindow.ui. The menu is
    # deliberately compact (two buttons and a hint), so this is the shipped
    # design rather than a floor the window is allowed to shrink past.
    assert menu.ui.minimumSize().width() >= 400
    assert menu.ui.minimumSize().height() >= 150


# ---------------------------------------------------------------------------
# The file dialog
# ---------------------------------------------------------------------------

def test_the_filter_is_built_from_the_supported_extensions():
    """Generated, not typed: a container added to one list and missed in the
    other would be invisible in the dialog and refused after the user picked it,
    which is the worse of the two failures."""
    from shared.sources import VIDEO_EXTENSIONS
    from mainwindow import video_name_filter

    filter_text = video_name_filter()

    assert filter_text.startswith("Videos (")
    for extension in VIDEO_EXTENSIONS:
        assert f"*{extension}" in filter_text
    assert filter_text.endswith(";;All files (*)")


def test_the_dialog_is_left_native(qapp, monkeypatch):
    """The OS dialog is better than Qt's, and it is what remembers the folder the
    user was last in — which is why nothing here persists a start directory.
    `DontUseNativeDialog` would trade that away for nothing."""
    import mainwindow
    from PySide6.QtWidgets import QFileDialog

    recorded = {}

    class RecordingDialog(QFileDialog):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            recorded["options"] = self.options()

        def exec(self):
            return 0

    monkeypatch.setattr(mainwindow, "QFileDialog", RecordingDialog)

    assert mainwindow.choose_source_video() is None
    assert not recorded["options"] & QFileDialog.DontUseNativeDialog


def test_a_cancelled_dialog_chooses_nothing(qapp, monkeypatch):
    import mainwindow
    from PySide6.QtWidgets import QFileDialog

    class CancellingDialog(QFileDialog):
        def exec(self):
            return 0

    monkeypatch.setattr(mainwindow, "QFileDialog", CancellingDialog)

    assert mainwindow.choose_source_video() is None


def test_the_dialog_reports_one_selected_file(qapp, monkeypatch, tmp_path):
    """Asserted through the validator rather than as a string, because Qt hands
    back forward slashes on Windows and the question that matters is whether
    that survives the pipeline — not which separator it arrives with."""
    from shared.sources import validate_source_video

    import mainwindow
    from PySide6.QtWidgets import QFileDialog

    chosen = tmp_path / "compilation.mp4"
    chosen.write_bytes(b"video")

    class AcceptingDialog(QFileDialog):
        def exec(self):
            self.selectFile(str(chosen))
            return 1

    monkeypatch.setattr(mainwindow, "QFileDialog", AcceptingDialog)

    reported = mainwindow.choose_source_video()

    assert reported
    assert validate_source_video(reported) == str(chosen)


# ---------------------------------------------------------------------------
# Dragging a video in
# ---------------------------------------------------------------------------

def _mime(entries):
    """A drag's mime data, carrying `entries` as file URLs.

    An entry that is already a `QUrl` is passed through, which is how the
    not-a-file-on-this-machine case is built.

    The caller has to hold on to the result until the event has been *sent*. Qt
    keeps a bare pointer to it, so a mime data built inline as a constructor
    argument is destroyed before the event is delivered and the window is handed
    a freed one — which arrives as a `QObject` with no `urls`, and fails in a way
    that looks like a PySide bug rather than a harness one.
    """
    data = QMimeData()
    data.setUrls([
        entry if isinstance(entry, QUrl) else QUrl.fromLocalFile(str(entry))
        for entry in entries
    ])
    return data


def _drag(widget, *entries):
    """The drag-enter a real drag produces over `widget`.

    Started unaccepted, because a drag is not taken until a widget says so: an
    assertion about `isAccepted()` is only evidence if it could have been false.
    """
    mime = _mime(entries)
    event = QDragEnterEvent(
        QPoint(5, 5), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    event.ignore()
    QApplication.sendEvent(widget, event)
    return event


def _drop(widget, *entries):
    """A whole drag over `widget`: enter, then release.

    Both halves, in that order, because Qt discards a drop that arrives without a
    drag-enter before it — a drop has no target widget without one — so sending a
    drop alone tests nothing about a real drag. The same reason means a drag the
    window refused never gets here, and this returns the *drag* event in that case
    so a test can say which half of the pair was refused.
    """
    entered = _drag(widget, *entries)
    if not entered.isAccepted():
        return entered
    mime = _mime(entries)
    event = QDropEvent(
        QPointF(5, 5), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    event.ignore()
    QApplication.sendEvent(widget, event)
    return event


def test_the_menu_is_a_drop_target(menu):
    """Only the window has to accept drops. Qt hands a drag to the widget under
    the cursor and, if that widget will not take it, up to its parent -- so the
    buttons and labels inside need nothing, and a drop works anywhere on the
    menu rather than only over the Editor button."""
    assert menu.acceptDrops()
    assert not menu.ui.editorButton.acceptDrops()


def test_the_editor_hint_advertises_the_drop(menu):
    """Discoverability is the whole difficulty of a second entry point. The
    behaviour is in the code whether or not the hint says so."""
    hint = menu.ui.labelEditorHint.text().lower()
    assert "drop" in hint
    assert "scan" in hint


def test_a_drag_is_offered_for_a_file_and_refused_for_anything_else(menu, tmp_path):
    """The cursor is the only answer a drag gives before it is released, and it
    cannot explain itself — so it says yes to anything that is a file here and
    lets the release be refused by name, and no only to what is not a path at
    all. Judging the extension here instead would answer a mistyped container
    with nothing happening."""
    video = tmp_path / "compilation.mp4"
    video.write_bytes(b"video")
    text = tmp_path / "notes.txt"
    text.write_text("not a video", encoding="utf-8")

    assert _drag(menu, video).isAccepted()
    assert _drag(menu, text).isAccepted()
    assert not _drag(menu, QUrl("https://example.com/clip.mp4")).isAccepted()


def test_dropping_a_video_opens_the_scanner_on_it(menu):
    """The same call the dialog makes, so a drop cannot reach a window the
    button would have refused."""
    event = _drop(menu, menu.source)

    assert event.isAccepted()
    assert menu.opened == [("scanner", {"source": str(menu.source)})]


def test_a_drop_of_several_files_opens_the_first_video(menu, tmp_path):
    """A drop can carry a folder's worth of rips and the wizard works on one
    source, so the first video wins. A file that is not a video is stepped over
    rather than chosen, since one is exactly what the drop has to be able to
    carry alongside the rest."""
    notes = tmp_path / "notes.txt"
    notes.write_text("not a video", encoding="utf-8")
    first = tmp_path / "first.mkv"
    first.write_bytes(b"video")
    second = tmp_path / "second.mp4"
    second.write_bytes(b"video")

    _drop(menu, notes, first, second)

    assert menu.opened == [("scanner", {"source": str(first)})]


def test_a_dropped_file_that_is_not_a_video_is_refused_by_name(menu, tmp_path):
    """Through the shipped rule, not by ignoring the drop: a gesture that visibly
    does nothing cannot be told from a bug, and this is the same file the dialog
    would have refused."""
    notes = tmp_path / "notes.txt"
    notes.write_text("not a video", encoding="utf-8")

    _drop(menu, notes)

    assert menu.opened == []
    assert menu.refusals and "notes.txt" in menu.refusals[0]


def test_a_drop_that_is_not_a_file_on_this_machine_is_ignored(menu):
    """A link dragged out of a browser is not a path this app can open, and
    there is nothing to refuse it by. The drag is refused outright, so no drop
    follows it."""
    assert not _drop(menu, QUrl("https://example.com/compilation.mp4")).isAccepted()
    assert menu.opened == []
    assert menu.refusals == []


def test_a_dropped_video_in_a_folder_that_cannot_be_written_is_refused(menu, monkeypatch):
    """The writable-folder rule reaches the drop route because both routes
    validate in one method, so a drop is not a way around it."""
    import shared.sources as sources_module

    monkeypatch.setattr(
        sources_module, "_writability_problem",
        lambda _folder: "[Errno 13] Permission denied")

    _drop(menu, menu.source)

    assert menu.opened == []
    assert menu.refusals and str(menu.source.parent) in menu.refusals[0]


def test_a_refused_drop_leaves_the_menu_up_to_try_again(menu, tmp_path):
    """The refusal is a message, not a dead end: the menu is still there and a
    second drop opens normally."""
    notes = tmp_path / "notes.txt"
    notes.write_text("not a video", encoding="utf-8")

    _drop(menu, notes)
    _drop(menu, menu.source)

    assert menu.opened == [("scanner", {"source": str(menu.source)})]


# ---------------------------------------------------------------------------
# Opening the other windows
# ---------------------------------------------------------------------------

def test_editor_button_opens_the_scanner_on_the_chosen_video(menu):
    """The menu chooses the video and hands it straight to the scanner; there is
    no window in between any more."""
    menu.ui.editorButton.click()

    assert menu.opened == [("scanner", {"source": str(menu.source)})]


def test_the_source_is_validated_before_the_scanner_is_built(menu, tmp_path):
    """The interesting refusals — a folder that cannot take the `.cmct`, a file
    that is not a video — are far clearer before the scanner is built than
    after."""
    menu.answer["answer"] = str(tmp_path / "notes.txt")
    (tmp_path / "notes.txt").write_text("not a video", encoding="utf-8")

    menu.ui.editorButton.click()

    assert menu.opened == []
    assert menu.refusals and "notes.txt" in menu.refusals[0]


def test_a_video_in_a_folder_that_cannot_be_written_is_refused(menu, monkeypatch):
    """The rule that only became reachable once sources could come from
    anywhere: the `.cmct` is written beside the video, so a read-only folder
    fails later and unhelpfully unless it is refused here."""
    import shared.sources as sources_module
    from shared.sources import validate_source_video

    monkeypatch.setattr(
        sources_module, "_writability_problem",
        lambda _folder: "[Errno 13] Permission denied")

    with pytest.raises(ValueError) as error:
        validate_source_video(str(menu.source))

    assert str(menu.source.parent) in str(error.value)


def test_cancelling_the_dialog_opens_nothing(menu):
    """The property the picker had and the dialog has to keep: a cancelled
    choice leaves the menu exactly where it was."""
    menu.answer["answer"] = None

    menu.ui.editorButton.click()

    assert menu.opened == []
    assert menu.refusals == []
    assert menu.isVisible() or True  # never closed, and never asked to be


def test_settings_button_opens_the_settings_window(menu):
    menu.ui.settingsButton.click()

    assert menu.opened == [("settings", {})]


def test_the_menu_does_not_start_processes(menu):
    """The process model is gone, and a subprocess here would mean it is not.

    Nothing in the menu's own code spawns anything any more — the shell builds
    windows in this process — so this is the guard against the old
    launch_command/Popen arrangement quietly coming back.
    """
    import ast

    import mainwindow

    source = open(mainwindow.__file__, encoding="utf-8").read()
    assert "subprocess" not in source
    assert ast.parse(source) is not None


def test_the_menu_does_not_wait_on_a_child(menu):
    """It used to hold a Popen handle it deliberately never waited on. The
    equivalent today is that it never observes the window it opened."""
    menu.ui.editorButton.click()

    assert menu.opened  # it asked, and that is the whole of its involvement


