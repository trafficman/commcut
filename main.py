"""Entry point: create the QApplication, the main menu, and the window shell.

This is the only way into the app. Every window runs in this process, on this
one event loop, and the shell (``shared/session.py``) is what puts one on top of
another. The main menu is the root of that stack and outlives its children, so
closing a window brings the menu back to the foreground without relaunching
anything or making a second one.

There is no ``--window`` flag and no per-window script. That used to be how a
window was opened — the app re-executed its own binary with a window name and a
source path as argv — and it existed because constructing an mpv player was
believed to deadlock when another top-level window was foreground. That hazard
was tested directly and did not reproduce; see
``experiments/mpv_foreground/``. ``main.py`` now imports a window's builder on
demand and the shell shows it, instead of dispatching to a new process.
"""

import os
import sys

# main.py sits at the project root, which is what the shared package needs on
# sys.path before it can be imported.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shared import diagnostics
from shared.environment import ensure_app_folders, setup_environment

SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from mainwindow import MainWindow
from shared.session import Shell, set_shell


def run_main_menu(argv):
    """Show the main menu and run the event loop. Returns the exit code."""
    # High-DPI scaling on modern Windows displays. Set before the QApplication
    # exists, which is why it is here and not in a window.
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(argv)
    diagnostics.install_excepthook(app)

    # The menu is the root of the stack and is never destroyed: closing a child
    # brings this same window back to the foreground, which is what the
    # separate-process arrangement used to do by leaving the menu running
    # behind everything else.
    menu = MainWindow()
    set_shell(Shell(app, menu))
    menu.show()
    return app.exec()


def main(argv=None):
    """Start the app. Returns the process exit code.

    `argv` goes to the QApplication, which is the only part of it that is still
    read. It used to be more than that: ``--window <name> [args]`` routed it to
    a child window's ``run()``, which is how the picker passed a source video to
    the scanner and the scanner to the editor. A window is now opened by the
    shell and handed the path as a constructor argument, so there is nothing
    left to dispatch.
    """
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

    return run_main_menu(argv)


if __name__ == "__main__":
    diagnostics.log(f"commcut starting ({sys.executable})")
    sys.exit(main())
