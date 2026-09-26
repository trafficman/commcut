"""Diagnostics for the application: a log file, and a last-resort error dialog.

The packaged build is compiled with ``console=False``, which is right for a GUI
app but leaves nowhere for a traceback to go. The bare ``print()`` calls this
module replaces were writing ffmpeg and mpv failures to a console that does not
exist in a packaged build, so those failures were invisible.

Everything lands in one append-only log next to the executable, and an
uncaught exception additionally raises a dialog naming that log. The rule this
module follows: nothing is swallowed, and nothing is reported without a path
the user can actually look at.
"""

import datetime
import os
import sys
import traceback

from shared.environment import install_root


#: Name of the log file, in the install root next to the executable.
LOG_FILENAME = 'commcut.log'

#: Environment variable that overrides the log file's location. Set it to
#: redirect diagnostics somewhere writable, which is also how the test suite
#: keeps its logging out of the source tree.
LOG_ENV_VAR = 'COMMCUT_LOG'

# Keep the log from growing without bound across many edit/extract sessions.
_MAX_LOG_BYTES = 1 * 1024 * 1024


def log_path():
    """Absolute path of the log file.

    $COMMCUT_LOG wins if set, so a build dropped somewhere non-writable can
    still be diagnosed.
    """
    override = os.environ.get(LOG_ENV_VAR)
    if override:
        return os.path.abspath(override)
    return os.path.join(install_root(), LOG_FILENAME)


def _timestamp():
    return datetime.datetime.now().isoformat(timespec='seconds')


def _rotate_if_large(path):
    """Truncate rather than delete: one full log of history is plenty."""
    try:
        if os.path.exists(path) and os.path.getsize(path) > _MAX_LOG_BYTES:
            os.remove(path)
    except OSError:
        # Losing the ability to rotate is not worth failing a log call over.
        pass


def log(message):
    """Append one line to the log, and echo it to stderr when there is one.

    Never raises. Diagnostics that can themselves crash are worse than
    diagnostics that are occasionally lost, so every failure path here is
    swallowed on purpose -- the caller has no useful recovery from "the log
    file could not be written".
    """
    line = f"[{_timestamp()}] {message}"

    try:
        path = log_path()
        _rotate_if_large(path)
        with open(path, 'a', encoding='utf-8', errors='replace') as handle:
            handle.write(line + '\n')
    except OSError:
        pass

    # A windowed PyInstaller build has no stderr, so this is normally a no-op.
    try:
        if sys.stderr is not None:
            print(line, file=sys.stderr)
    except (OSError, ValueError):
        pass


def log_exception(message, exc):
    """Log a handled exception with its traceback at ERROR level."""
    log(f"ERROR {message}: {type(exc).__name__}: {exc}")
    for line in traceback.format_exception(type(exc), exc, exc.__traceback__):
        log("  " + line.rstrip("\n"))


def fatal(title, text):
    """Report a startup failure the user can act on, then return.

    Used for anything that goes wrong *before* a window is up -- a missing
    source video, an unwritable install folder, a bad --window argument. In a
    windowed build there is no console, so an uncaught exception here would
    otherwise be reported by the PyInstaller bootloader's own traceback dialog,
    which is modal: the process sits there waiting for a click and looks like a
    hang. This shows one message and lets the caller return a real exit code.

    Creates a QApplication if there is not one, because the dialog is the only
    way this reaches the user.
    """
    log(f"FATAL {title}: {text}")

    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication(sys.argv[:1])
        QMessageBox(QMessageBox.Critical, title, text).exec()
        del app
    except Exception:
        # If even the dialog cannot be shown, the log is all we have left.
        print(f"commcut: {title}: {text}", file=sys.stderr)


def install_excepthook(app=None):
    """Route uncaught exceptions to the log and, when possible, a dialog.

    Call once after the QApplication exists. `app` is only used so the dialog
    has a parent to centre on; without a QApplication the dialog cannot be
    shown at all, in which case the log is the only record and the message says
    so rather than failing silently.
    """
    previous = sys.excepthook

    def handle(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            previous(exc_type, exc_value, exc_tb)
            return

        log("ERROR unhandled exception (top level)")
        for line in traceback.format_exception(exc_type, exc_value, exc_tb):
            log("  " + line.rstrip("\n"))

        text = (
            f"{exc_type.__name__}: {exc_value}\n\n"
            f"A full traceback was written to:\n{log_path()}"
        )
        try:
            from PySide6.QtCore import Qt
            from PySide6.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is None:
                text += (
                    "\n\n(no window could be opened because the Qt application "
                    "is not running)"
                )
                return
            box = QMessageBox(QMessageBox.Critical, "commcut stopped", text)
            if app is not None:
                box.setWindowModality(Qt.ApplicationModal)
            box.exec()
        except Exception:
            # Showing the dialog must not mask the original failure.
            pass

    sys.excepthook = handle
