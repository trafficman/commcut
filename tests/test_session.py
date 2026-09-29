"""The one visible window: shared/session.py.

The shell shows exactly one of {menu, picker, scanner, editor, settings} at a
time, so its whole job is deciding what replaces what. Three of its rules carry
enough weight to have their own tests here, because each one fails quietly:

  - A window the shell opens **replaces** the one on screen: closed if it is not
    the menu, hidden if it is. Hiding a window that owns an mpv player would
    leave that player alive against a window the user cannot see, and closing is
    the only thing that runs the closeEvent which shuts the player down.
  - The new window is built **before** the old one is taken down. A build that
    fails then leaves the user exactly where they were, and a window opened from
    a button handler is not destroyed from inside its own signal.
  - A window that goes away brings the **menu** back, and the menu going away
    quits the app. X-click, "Back to main menu", a redirect, and a failed build
    are all the same trigger: the current window ended.

The windows are plain QMainWindows registered under test-only names. What is
under test is the shell, not any particular window's UI, and building a real
editor here would need libmpv and a video.
"""

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox

import shared.session as session_module
from shared.session import OpenInstead, Shell, set_shell


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def close(window, qapp):
    """Close a window and let Qt deliver the deferred delete.

    WA_DeleteOnClose destroys the C++ object through a DeferredDelete event,
    which processEvents() does not reliably deliver. A test that closes a window
    and only pumps the loop will sometimes assert against state that has not
    been updated yet, and then pass on the next run.
    """
    from PySide6.QtCore import QEvent

    window.close()
    qapp.processEvents()
    qapp.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


@pytest.fixture
def stack(qapp, monkeypatch):
    """A shell over a plain root window, with test-only builders registered.

    The builders are resolved through the real registry and the real importlib
    lookup — attributes of this module under the test-only names — so the path a
    window name actually takes at runtime is the one under test. Only the window
    classes are substituted.

    Yields the shell; ``stack.calls`` is the list every builder appended to, so
    a test can assert what a window was constructed with.
    """
    calls = []

    def register(name, error=None, redirect=None):
        def builder(app, **kwargs):
            calls.append((name, kwargs))
            if error is not None:
                raise error
            if redirect is not None:
                raise OpenInstead(*redirect[0], **redirect[1])
            window = QMainWindow()
            window.setWindowTitle(name)
            return window
        setattr(sys.modules[__name__], name, builder)
        return builder

    for name in ("alpha", "beta", "gamma"):
        register(name)
    register("broken", error=FileNotFoundError("no source video found"))
    register("redirecting", redirect=(("alpha",), {"source": r"C:\videos\old.mp4"}))
    register("redirecting_missing", redirect=(("not-a-window",), {}))

    monkeypatch.setattr(
        session_module, "_BUILDERS",
        {name: (__name__, name) for name in
         ("alpha", "beta", "gamma", "broken", "redirecting",
          "redirecting_missing")})

    menu = QMainWindow()
    menu.setWindowTitle("menu")
    shell = Shell(qapp, menu)
    shell.calls = calls
    shell.menu_widget = menu
    set_shell(shell)
    try:
        yield shell
    finally:
        set_shell(None)


def test_the_menu_is_the_starting_point(stack):
    assert stack.current is None
    assert stack.menu is stack.menu_widget
    assert stack.windows == (stack.menu,)


def test_opening_a_window_hides_the_menu(stack):
    """The menu is the one window that is hidden rather than closed: it is
    reused, and rebuilding it would re-parse mainwindow.ui for two buttons."""
    stack.menu_widget.show()
    assert stack.menu_widget.isVisible()

    window = stack.open("alpha")

    assert stack.current is window
    assert not stack.menu_widget.isVisible()
    assert window.isVisible()


def test_the_menu_is_the_same_instance_when_it_comes_back(stack):
    """Returning to the menu must not build a second one. A second menu would
    put two entries in the taskbar, which is the thing this design exists to
    avoid."""
    window = stack.open("alpha")
    close(window, _qapp())

    assert stack.current is None
    assert stack.menu is stack.menu_widget
    assert stack.menu_widget.isVisible()


def test_opening_a_window_closes_the_one_it_replaces(stack, qapp):
    """Not hides it. A hidden scanner or editor would keep its mpv player alive
    against a window the user cannot see, and closing is the only thing that
    runs the closeEvent which shuts that player down."""
    first = stack.open("alpha")
    first.show()

    second = stack.open("beta")

    assert not first.isVisible()
    assert stack.current is second
    assert second.isVisible()


def test_a_failed_open_leaves_what_was_showing_alone(stack, monkeypatch):
    """The new window is built before the old one is taken down precisely so
    this holds: a window that will not open must not cost the user the screen
    they were on."""
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a: None))
    stack.open("alpha")
    before = stack.current

    assert stack.open_safely("broken") is None

    assert stack.current is before
    assert before.isVisible()
    assert not stack.menu_widget.isVisible()


def test_a_failed_open_reports_rather_than_raises(stack, monkeypatch):
    """An exception out of a button handler reaches the event loop, and with one
    process that takes down the window the button belonged to."""
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *args: warnings.append(args)))

    assert stack.open_safely("broken") is None

    assert warnings, "a failed launch must say so rather than raise"
    assert "no source video" in warnings[0][2]


def test_a_source_path_travels_as_an_constructor_argument(stack):
    """Not re-derived by the window and not stashed in shared state: it is what
    the shell was asked to open the window with."""
    stack.open("beta", source=r"C:\videos\compilation.mp4")

    assert stack.calls == [("beta", {"source": r"C:\videos\compilation.mp4"})]


def test_an_unknown_window_is_refused_by_name(stack):
    with pytest.raises(ValueError) as error:
        stack.open("not-a-window")

    for name in ("alpha", "beta", "gamma"):
        assert name in str(error.value)


def test_a_window_that_declines_is_replaced_by_the_one_it_named(stack):
    """The scanner is the case: a video that already has a .cmct must not be
    re-scanned, so opening the scanner for it opens the editor instead.

    This was a `None` return at first, and the shell treated the None as a
    window -- which reported the already-scanned video as unopenable rather
    than opening the editor. The assertion that the caller gets a real window
    is the regression guard.
    """
    window = stack.open("redirecting")

    assert type(window).__name__ == "QMainWindow"
    assert stack.current is window
    assert stack.calls == [
        ("redirecting", {}),
        ("alpha", {"source": r"C:\videos\old.mp4"}),
    ]


def test_a_redirect_is_not_reported_as_a_failure(stack, monkeypatch):
    """It is routing, not an error, so it must not reach the log-and-warn path
    that a real failure uses."""
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *args: warnings.append(args)))

    stack.open_safely("redirecting")

    assert not warnings


def test_a_redirect_to_a_window_that_does_not_exist_is_an_error(stack, monkeypatch):
    """A redirect naming a window the registry does not have is a programming
    error in the builder, and it surfaces the way any other failure would."""
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *args: warnings.append(args)))

    assert stack.open_safely("redirecting_missing") is None
    assert warnings
    assert "not-a-window" in warnings[0][2]
    assert stack.current is None


def test_closing_the_menu_quits_the_app(stack, qapp):
    """Nothing left to run. This is the case that used to be a process exit."""
    quit_calls = []
    stack.app.quit = lambda: quit_calls.append(1)
    stack.menu_widget.show()

    close(stack.menu_widget, qapp)

    assert quit_calls == [1]
    assert stack.current is None


def test_a_window_closed_out_of_order_does_not_disturb_the_new_one(stack, qapp):
    """The picker's Open button opened the scanner and then closed itself, so
    the window that died was not the one on screen. The shell removes the
    window it is told about by identity for exactly this."""
    picker = stack.open("alpha")
    scanner = stack.open("beta")
    assert stack.current is scanner

    close(picker, qapp)

    assert stack.current is scanner, "the window just opened must stay current"
    assert scanner.isVisible()


def test_the_shell_is_reachable_from_a_window(stack):
    assert session_module.shell() is stack


def test_a_window_cannot_be_opened_before_main_installs_a_shell():
    set_shell(None)

    with pytest.raises(RuntimeError) as error:
        session_module.shell()

    assert "main.py" in str(error.value)


def _qapp():
    return QApplication.instance()
