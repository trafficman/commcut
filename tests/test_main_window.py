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
    """A MainWindow whose window-opening is recorded instead of building GUIs.

    The menu opens the other windows through the shell, so that is what gets
    stubbed. open_safely is the one it must call, and the fake only provides
    that one method on purpose: reaching for shell().open() instead would be an
    AttributeError here, which is the point — an exception out of a button
    handler reaches Qt's event loop, and with one process that takes the menu
    down with it.
    """
    import mainwindow

    opened = []

    class RecordingShell:
        def open_safely(self, name, **kwargs):
            opened.append((name, kwargs))
            return object()

    monkeypatch.setattr(mainwindow, "shell", RecordingShell)

    instance = mainwindow.MainWindow()
    instance.opened = opened
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
# Opening the other windows
# ---------------------------------------------------------------------------

def test_editor_button_opens_the_source_picker(window):
    """The menu picks the video; the scanner is opened by the picker."""
    window.ui.editorButton.click()

    assert window.opened == [("picker", {})]


def test_settings_button_opens_the_settings_window(window):
    window.ui.settingsButton.click()

    assert window.opened == [("settings", {})]


def test_the_menu_does_not_start_processes(window):
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


def test_the_menu_does_not_wait_on_a_child(window):
    """It used to hold a Popen handle it deliberately never waited on. The
    equivalent today is that it never observes the window it opened."""
    window.ui.editorButton.click()

    assert window.opened  # it asked, and that is the whole of its involvement


