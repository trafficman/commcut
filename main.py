"""Application entry point.

Opens the main menu. Every other window is launched from there as its own
process, so this script's only job is to show the menu and run the event loop.

It is also the single packaged binary. From source the other windows are
separate .py scripts, but a PyInstaller build has no .py files on disk, so
``commcut.exe --window <name>`` re-executes this same binary and the block
below routes it to that window's entry point instead. Both paths end up in the
same per-window ``run()``, so there is exactly one implementation of each
window and the packaged build cannot drift from the source build.
"""

import os
import sys

# main.py sits at the project root, which is what the shared package needs on
# sys.path before it can be imported.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shared import diagnostics
from shared.environment import (
    WINDOW_NAMES, ensure_app_folders, setup_environment,
)

SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from mainwindow import MainWindow


WINDOW_FLAG = '--window'


def run_main_menu(argv):
    """Show the main menu and run the event loop. Returns the exit code."""
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(argv)
    diagnostics.install_excepthook(app)

    window = MainWindow()
    window.show()
    return app.exec()


def _run_window(window_name, argv):
    """Dispatch to a child window's entry point. Returns the exit code."""
    # Imported lazily: from source these are the same modules the standalone
    # scripts are, and importing them all up front would pull in mpv and every
    # window's dependencies just to show the main menu.
    if window_name == 'scanner':
        from scanner.scanner import run as scanner_run
        return scanner_run()
    if window_name == 'editor':
        from editor.editor import run as editor_run
        return editor_run()
    if window_name == 'settings':
        from settings.settings import run as settings_run
        return settings_run()
    raise ValueError(f"Unknown window: {window_name!r}")


def main(argv=None):
    """Route argv to the requested window and run it. Returns the exit code."""
    argv = list(sys.argv if argv is None else argv)

    try:
        # The folders are cheap to ensure and every window assumes they exist.
        # A non-writable install root is reported once here rather than as an
        # unrelated failure much later in a save or an export.
        ensure_app_folders()
    except OSError as error:
        diagnostics.fatal(
            "commcut could not start",
            f"The install folder is not writable:\n\n{PROJECT_ROOT}\n\n"
            f"{error}")
        return 1

    if len(argv) > 1 and argv[1] == WINDOW_FLAG:
        if len(argv) < 3:
            diagnostics.fatal(
                f"{WINDOW_FLAG} needs a window name",
                f"Expected one of:\n{', '.join(WINDOW_NAMES)}")
            return 2
        try:
            return _run_window(argv[2], argv[3:])
        except Exception as error:
            # Everything a child window can fail at before it opens a window
            # lands here. Letting it escape would reach the PyInstaller
            # bootloader's traceback dialog, which is modal and reads as a
            # hang, so it is reported and turned into an exit code instead.
            diagnostics.log_exception(
                f"the {argv[2]} window could not start", error)
            diagnostics.fatal(
                "commcut could not start",
                f"The {argv[2]} window could not start.\n\n"
                f"{type(error).__name__}: {error}\n\n"
                f"Details were written to:\n{diagnostics.log_path()}")
            return 1

    return run_main_menu(argv)


if __name__ == "__main__":
    diagnostics.log(f"commcut starting ({sys.executable})")
    sys.exit(main())
