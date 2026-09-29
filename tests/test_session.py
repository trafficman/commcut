"""The window stack: shared/session.py.

The process-per-window model is gone, so the shell is now the only thing
deciding which window is on top. Two of its rules are load-bearing enough to
have their own tests here, because both fail quietly if they regress:

  - Windows are removed from the stack **by identity**, not by popping the top.
    The picker's Open button opens the scanner and *then* closes itself, so the
    window that died is not the one on top. A stack that popped the top would
    throw the scanner away and put the menu back over it, and the user would be
    looking at the main menu after choosing a video.
  - A window that fails to open is **reported, not raised**. Every window opens
    the next one from a button handler, and the separate-process arrangement
    used to be what kept an exception in the scanner from taking the menu down
    with it. There is no process boundary now, so open_safely is what stands in
    for it.

The windows are plain QMainWindows registered under test-only names. What is
under test is the stack, not any particular window's UI, and building a real
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


@pytest.fixture
def stack(qapp, monkeypatch):
    """A shell over a plain root window, with test-only builders registered.

    The builders are resolved through the real registry and the real
    importlib lookup — they are attributes of this module under the test-only
    names — so the path a window name actually takes at runtime is the one
    under test. Only the window classes are substituted.

    Yields the shell; ``stack.calls`` is the list every builder appended to,
    so a test can assert what a window was constructed with.
    """
    calls = []
    failures = {}

    def register(name, error=None):
        def builder(app, **kwargs):
            calls.append((name, kwargs))
            if error is not None:
                raise error
            window = QMainWindow()
            window.setWindowTitle(name)
            return window
        setattr(sys.modules[__name__], name, builder)
        return builder

    for name in ("alpha", "beta", "gamma"):
        register(name)
    register("broken", FileNotFoundError("no source video found"))
    register("redirecting", OpenInstead("alpha", source=r"C:\videos\old.mp4"))
    register("redirecting_missing", OpenInstead("not-a-window"))

    monkeypatch.setattr(
        session_module, "_BUILDERS",
        {name: (__name__, name) for name in
         ("alpha", "beta", "gamma", "broken", "redirecting",
          "redirecting_missing")})

    root = QMainWindow()
    root.setWindowTitle("menu")
    shell = Shell(qapp, root)
    shell.calls = calls
    set_shell(shell)
    try:
        yield shell
    finally:
        set_shell(None)


def close(window, qapp):
    """Close a window and let Qt deliver the destroyed signal.

    WA_DeleteOnClose destroys the C++ object through a deferred delete, which
    processEvents() does not reliably deliver — a test that only pumps the loop
    sometimes asserts against a stack that has not been updated yet, and then
    the same code passes on a second run. Delivering DeferredDelete explicitly
    is what makes the removal deterministic.
    """
    from PySide6.QtCore import QEvent

    window.close()
    qapp.processEvents()
    qapp.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def test_the_menu_is_the_root_and_survives_its_children(stack, qapp):
    menu = stack.windows[0]

    child = stack.open("alpha")
    assert stack.windows == (menu, child)

    close(child, qapp)
    assert stack.windows == (menu,), "the menu must still be there to come back to"


def test_a_window_closes_back_to_the_one_below_it(stack, qapp):
    menu = stack.windows[0]
    beta = stack.open("beta")
    gamma = stack.open("gamma")

    close(gamma, qapp)

    assert stack.windows == (menu, beta)


def test_a_window_that_closed_itself_is_removed_by_identity(stack, qapp):
    """The case the picker actually produces: open the next window, then close
    yourself. The window that dies is not the one on top, so a stack that
    popped the top would discard the scanner and put the menu over it."""
    picker = stack.open("alpha")
    scanner = stack.open("beta")
    assert stack.windows[-1] is scanner

    close(picker, qapp)

    assert stack.windows[-1] is scanner, "the window just opened must stay on top"
    assert picker not in stack.windows


def test_closing_the_oldest_child_leaves_the_newest_in_front(stack, qapp):
    menu = stack.windows[0]
    alpha = stack.open("alpha")
    beta = stack.open("beta")
    gamma = stack.open("gamma")

    close(alpha, qapp)

    assert stack.windows == (menu, beta, gamma)
    assert stack.windows[-1] is gamma


def test_a_source_path_travels_as_a_constructor_argument(stack):
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
    menu = stack.windows[0]

    window = stack.open("redirecting")

    assert type(window).__name__ == "QMainWindow"
    assert window is not menu
    assert stack.windows == (menu, window)
    assert stack.calls == [
        ("redirecting", {}),
        ("alpha", {"source": r"C:\videos\old.mp4"}),
    ]


def test_a_redirect_is_not_reported_as_a_failure(stack, monkeypatch):
    """It is routing, not an error, so it must not reach the log-and-warn path
    that a real failure uses."""
    from PySide6.QtWidgets import QMessageBox

    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *args: warnings.append(args)))

    stack.open_safely("redirecting")

    assert not warnings


def test_a_redirect_to_a_window_that_does_not_exist_is_an_error(stack, monkeypatch):
    """A redirect naming a window the registry does not have is a programming
    error in the builder, and it surfaces the same way any other failure would."""
    from PySide6.QtWidgets import QMessageBox

    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *args: warnings.append(args)))

    assert stack.open_safely("redirecting_missing") is None
    assert warnings
    assert "not-a-window" in warnings[0][2]
    assert len(stack.windows) == 1, "a failed redirect must not leave a window"


def test_a_window_that_cannot_be_opened_is_reported_not_raised(stack, monkeypatch):
    """An exception out of a button handler reaches the event loop, and with one
    process that takes down the window the button belonged to."""
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *args: warnings.append(args)))

    assert stack.open_safely("broken") is None

    assert warnings, "a failed launch must say so rather than raise"
    assert "no source video" in warnings[0][2]


def test_a_failed_open_leaves_the_stack_alone(stack, monkeypatch):
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a: None))
    before = stack.windows

    assert stack.open_safely("broken") is None

    assert stack.windows == before


def test_the_shell_is_reachable_from_a_window(stack):
    assert session_module.shell() is stack


def test_a_window_cannot_be_opened_before_main_installs_a_shell():
    set_shell(None)

    with pytest.raises(RuntimeError) as error:
        session_module.shell()

    assert "main.py" in str(error.value)
