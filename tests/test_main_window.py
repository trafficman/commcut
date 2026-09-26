"""Tests for the main menu window and the paths it launches.

The menu is the app's entry point, so the two things worth guarding are that
it resolves the right scripts and that a failure to launch surfaces as a
dialog rather than a crash.
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


def test_editor_button_explains_it_enters_through_the_scanner(window):
    """The scanner is the pre-process phase, so the hint should say so."""
    hint = window.ui.labelEditorHint.text()
    assert "scan" in hint.lower()
    assert window.ui.editorButton.toolTip()


def test_window_keeps_its_own_size(window):
    assert window.ui.minimumSize().width() >= 400
    assert window.ui.minimumSize().height() >= 240


# ---------------------------------------------------------------------------
# Launching
# ---------------------------------------------------------------------------

def test_editor_button_launches_the_scanner(window):
    window.ui.editorButton.click()

    assert len(window.launches) == 1
    assert window.launches[0][1] == os.path.join(
        PROJECT_ROOT, "scanner", "scanner.py")


def test_settings_button_launches_the_settings_window(window):
    window.ui.settingsButton.click()

    assert window.launches[0][1] == os.path.join(
        PROJECT_ROOT, "settings", "settings.py")


def test_launched_scripts_actually_exist(window):
    for command in (("scanner", "scanner.py"), ("settings", "settings.py")):
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
    """_launch refuses a path that is not there; the window must warn rather
    than let the exception escape into Qt's event loop."""
    import mainwindow

    warnings = []
    monkeypatch.setattr(
        mainwindow.QMessageBox, "warning",
        staticmethod(lambda parent, title, text: warnings.append((title, text))),
    )
    instance = mainwindow.MainWindow()
    try:
        instance._open(os.path.join("nope", "missing.py"), "Settings")
        assert warnings
        assert "missing.py" in warnings[0][1]
    finally:
        instance.close()
        instance.deleteLater()
        qapp.processEvents()


def test_launch_refuses_a_missing_script(qapp):
    import mainwindow

    with pytest.raises(FileNotFoundError):
        mainwindow._launch(os.path.join("nope", "missing.py"))
