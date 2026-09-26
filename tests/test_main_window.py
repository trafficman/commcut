"""Tests for the main menu window and the paths it launches.

The menu is the app's entry point, so the two things worth guarding are that
it resolves the right scripts and that a failure to launch surfaces as a
dialog rather than a crash. **Editor** opens the source picker, which is what
hands the chosen video to the scanner.
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
    os.path.join("picker", "picker.py"),
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
def window(qapp, monkeypatch):
    """A MainWindow with its launch recorded instead of spawning real GUIs."""
    import mainwindow

    warnings = []
    monkeypatch.setattr(
        mainwindow.QMessageBox, "warning",
        staticmethod(lambda parent, title, text: warnings.append((title, text))),
    )
    created = []
    monkeypatch.setattr(
        mainwindow.subprocess, "Popen",
        lambda command, *a, **k: created.append(command),
    )

    instance = mainwindow.MainWindow()
    instance.launches = created
    instance.warnings = warnings
    yield instance

    instance.close()
    instance.deleteLater()
    qapp.processEvents()


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def test_window_offers_both_destinations(window):
    assert window.ui.editorButton.text() == "Editor"
    assert window.ui.settingsButton.text() == "Settings"


def test_editor_button_explains_the_journey_it_starts(window):
    """The hint should name what actually happens: pick, scan, then tag."""
    hint = window.ui.labelEditorHint.text().lower()
    assert "scan" in hint
    assert window.ui.editorButton.toolTip()


def test_window_keeps_its_own_size(window):
    # Tracks the minimumSize declared in mainwindow.ui. The menu is
    # deliberately compact (two buttons and a hint), so this is the shipped
    # design rather than a floor the window is allowed to shrink past.
    assert window.ui.minimumSize().width() >= 400
    assert window.ui.minimumSize().height() >= 150


# ---------------------------------------------------------------------------
# Launching
# ---------------------------------------------------------------------------

def test_editor_button_launches_the_source_picker(window):
    """The menu picks the video; the scanner is launched by the picker."""
    window.ui.editorButton.click()

    assert len(window.launches) == 1
    assert window.launches[0][1] == os.path.join(
        PROJECT_ROOT, "picker", "picker.py")


def test_settings_button_launches_the_settings_window(window):
    window.ui.settingsButton.click()

    assert window.launches[0][1] == os.path.join(
        PROJECT_ROOT, "settings", "settings.py")


def test_launched_scripts_actually_exist(window):
    for command in (("scanner", "scanner.py"), ("settings", "settings.py"),
                     ("picker", "picker.py")):
        path = os.path.join(PROJECT_ROOT, *command)
        assert os.path.exists(path), f"{path} does not exist"


def test_launch_uses_the_current_interpreter(window):
    import sys

    window.ui.settingsButton.click()

    assert window.launches[0][0] == sys.executable


def test_window_survives_a_child_process_failure(window, monkeypatch):
    """A failed launch must warn, not take the menu down with it."""
    import mainwindow

    monkeypatch.setattr(
        mainwindow.subprocess, "Popen",
        lambda command, *a, **k: (_ for _ in ()).throw(OSError("no python")),
    )

    window.ui.settingsButton.click()

    assert window.warnings
    assert "no python" in window.warnings[0][1]


def test_missing_script_is_reported_not_raised(qapp, monkeypatch):
    """_launch refuses a window it does not know; the window must warn rather
    than let the exception escape into Qt's event loop."""
    import mainwindow

    warnings = []
    monkeypatch.setattr(
        mainwindow.QMessageBox, "warning",
        staticmethod(lambda parent, title, text: warnings.append((title, text))),
    )
    instance = mainwindow.MainWindow()
    try:
        instance._open("nope", "Settings")
        assert warnings
        assert "nope" in warnings[0][1]
    finally:
        instance.close()
        instance.deleteLater()
        qapp.processEvents()


def test_launch_refuses_an_unknown_window():
    """An unregistered window name is a programming error, and ValueError says so."""
    from shared.environment import launch_command

    with pytest.raises(ValueError) as error:
        launch_command("nope")

    # The message must name the valid options: this is the error a developer
    # hits first when adding a window.
    assert "scanner" in str(error.value)


def test_launch_refuses_a_missing_script(monkeypatch, tmp_path):
    """A registered window whose script is absent must raise rather than
    launch a process pointed at nothing."""
    import shared.environment as environment

    monkeypatch.setattr(environment, "install_root", lambda: str(tmp_path))

    with pytest.raises(FileNotFoundError) as error:
        environment.launch_command("scanner")

    assert "scanner" in str(error.value)


# ---------------------------------------------------------------------------
# Arguments to a child window
# ---------------------------------------------------------------------------

def test_launch_command_appends_arguments_from_source():
    """The picker hands the chosen video to the scanner as an argument."""
    from shared.environment import launch_command

    command = launch_command("scanner", r"C:\videos\compilation.mp4")

    assert command[-1] == r"C:\videos\compilation.mp4"


def test_launch_command_appends_arguments_when_frozen(monkeypatch):
    import shared.environment as environment

    monkeypatch.setattr(environment, "is_frozen", lambda: True)
    monkeypatch.setattr(environment.sys, "executable", r"C:\app\commcut.exe")

    command = environment.launch_command("editor", r"C:\videos\clip.mp4")

    assert command == [
        r"C:\app\commcut.exe", "--window", "editor", r"C:\videos\clip.mp4"]


def test_launch_command_without_arguments_is_unchanged():
    from shared.environment import launch_command

    command = launch_command("settings")

    assert command[-1].endswith(os.path.join("settings", "settings.py"))


def test_every_window_name_has_a_script():
    """WINDOW_NAMES drives the error message, so it must not list a window
    that has no script to launch."""
    from shared.environment import WINDOW_NAMES, launch_command

    for name in WINDOW_NAMES:
        assert launch_command(name)[-1].endswith(".py")


def test_dispatcher_forwards_arguments_to_the_window(monkeypatch):
    """main.py drops extra argv on the floor if it stops forwarding it, and the
    scanner would then silently fall back to the default source video."""
    import main

    seen = {}
    monkeypatch.setitem(
        main.__dict__, "_run_window", lambda name, args: seen.update(
            name=name, args=args) or 0)

    assert main.main(["commcut", "--window", "scanner", "a b.mp4"]) == 0
    assert seen == {"name": "scanner", "args": ["a b.mp4"]}
