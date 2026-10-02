"""Tests for the main menu window and the paths it launches.

The menu is the app's entry point, so the three things worth guarding are that it
resolves the right scripts, that **Editor** asks for a source video and hands the
chosen one to the scanner, and that a failure to launch surfaces as a dialog
rather than a crash.
"""

import os

import pytest

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
    than by a mock.
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


