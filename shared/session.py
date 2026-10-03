"""The one QApplication, and the one window the user is looking at.

Every window in this app runs in the same process, so something has to own the
``QApplication`` and decide what is on screen. That is this module: ``main.py``
constructs a :class:`Shell` once, and the windows ask it to open each other.

One visible window, not a stack
------------------------------------------------------------------------------

The shell shows exactly one of {menu, scanner, editor, settings, mesh, queue} at a time. A
window opens another by asking the shell, the shell hides or closes what is
there, and the new window takes the screen. When a non-menu window goes away,
the menu comes back.

This replaced a stack, where the main menu stayed open behind whatever else was
up. The stack was carrying two costs that were not obvious at the time. The menu
could be lost behind another window and put a second entry in the taskbar, so the
app did not present as one thing; and a window permanently behind another one is
a permanent source of edge cases about focus and activation, which is the sort
of thing that produces bugs nobody can attribute. With one window there are
still no guarantees, but there is one rule instead of a set of interactions.

What is *not* here: modal dialogs. The editor's export progress and summary
dialogs are ``QDialog``s owned by the editor, not shell-managed windows, so they
travel with whatever window opened them and the shell does not track them.

Why navigation is global but the source path is not
------------------------------------------------------------------------------

The video a window works on is always a constructor argument, never something
it reads back out of shared state. Navigation is the opposite case and has to be
reachable from anywhere: the scanner's Finished button, the editor's export
summary, and the main menu's **Editor** button all need to open a *different*
window from wherever they happen to be, and threading a reference to the shell
through every constructor would put the same coupling in a worse place. So there
is one shell per process and :func:`shell` hands it to whoever asks.

The source path starts at the main menu, which asks for it with a native file
dialog. That dialog is a modal dialog owned by the menu, and it is deliberately
not here: the shell tracks the one visible *window*, and the editor's own modal
dialogs are on the same footing.

The builders are imported lazily, inside :meth:`Shell.open`, and this module
imports nothing from the windows at the top. That is deliberate: the main menu
is the first thing that runs, and the two player-bearing windows pull in libmpv
via ``shared.mpv``. Importing them eagerly would load libmpv into the process
just to draw a two-button menu. See ``mpv_import_context`` in
``shared/environment.py`` for the other half of that arrangement.

Closed, not hidden -- except the menu
------------------------------------------------------------------------------

Opening a window *closes* the one it replaces, and closing is what runs the
window's ``closeEvent``. For the scanner and the editor that matters: their
``closeEvent`` shuts their mpv player down while the video frame still has its
native handle, and a hidden window would keep that player alive instead.

The menu is the exception because it is cheap to bring back and holds nothing
but two buttons. It is hidden and reused rather than rebuilt, so returning to it
is instant and ``mainwindow.ui`` is not re-parsed.

Closing is by identity, not by position
------------------------------------------------------------------------------

A window can close after its successor is already on screen: the scanner's
Finished button opens the editor and then closes itself. The window that died is
not the one that is current, so the shell removes the window it is told about by
identity and shows whatever is current afterwards.
"""

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QMessageBox

from shared.diagnostics import log, log_exception, log_path

#: name -> (module, builder) resolved on first use. Every builder takes the
#: process's QApplication as its first argument, so the shell can call them
#: without knowing which ones need it: the scanner and the editor show a splash
#: while they work and must pump the loop, and the settings window and the mesh
#: wizard never construct a player and ignore it. Uniformity beats introspection
#: in a registry this small — a builder that changed shape would fail loudly here,
#: not silently.
_BUILDERS = {
    "scanner": ("scanner.scanner", "create"),
    "editor": ("editor.editor", "create"),
    "settings": ("settings.settings", "create"),
    "mesh": ("importer.mesh", "create"),
    "queue": ("importer.queue", "create"),
}

#: The names a caller may open. Settings is listed so a bad name says what the
#: options are.
WINDOW_NAMES = tuple(sorted(_BUILDERS))

#: The one shell in this process, set by main.py. None until then, which is the
#: honest answer for a library that a test may import without an app.
_shell = None


def set_shell(shell):
    """Record the process's shell. Called once, by main.py."""
    global _shell
    _shell = shell
    return shell


def shell():
    """The process's shell.

    Raises RuntimeError if main.py has not built one, which is a programming
    error rather than a user-facing condition: every window reaches the shell
    only through the buttons main.py wired up.
    """
    if _shell is None:
        raise RuntimeError(
            "No shell has been installed. shared/session.py:set_shell() is "
            "called by main.py once it has a QApplication, and a window "
            "cannot be opened before that."
        )
    return _shell


class OpenInstead(Exception):
    """A builder declined to open its own window and named one to open instead.

    The scanner is the case: a video that already has a `.cmct` sidecar must
    never be re-scanned, so opening the scanner for it would overwrite the
    existing model. The rule belongs to the scanner, but the *routing* is not
    the scanner's to perform — it has no window to show.

    An explicit exception says "I am not opening, open this instead" without
    overloading a builder's return value. It used to be a `None` return, and
    that failed the way a sentinel does: the shell treated the `None` as a
    window and reported the already-scanned video as unopenable. A builder's
    contract is now either "return a window" or "raise", and this is the one
    exception to the second that is not an error.
    """

    def __init__(self, name, **kwargs):
        super().__init__(f"open {name!r} instead")
        self.name = name
        self.kwargs = kwargs


class WindowBusy(Exception):
    """A window that had to go away first would not close.

    Not a build failure — the incoming window was built fine — so reporting it
    through the "could not be opened" path in :meth:`Shell.open_safely` would
    blame the wrong window. It exists because a window may refuse to close while
    it still owns something that must not be destroyed yet, and the old
    behaviour discarded ``close()``'s return value: the refusal was silent, the
    outgoing window stayed on screen holding a live thread, and the incoming one
    was presented on top of it. Two live windows, and the thread that caused the
    refusal is destroyed by Qt on the way out.
    """

    def __init__(self, name):
        super().__init__(
            f"The window you were in is still busy, so the {name} window was "
            f"not opened. Nothing was changed. Try again in a moment."
        )
        self.name = name


class Shell(QObject):
    """Owns the one visible window. One per process, built by main.py."""

    def __init__(self, app, menu):
        super().__init__()
        self.app = app
        self._menu = menu
        self._current = None
        self._adopt(menu, destroy_on_close=False)
        menu.installEventFilter(self)

    def eventFilter(self, watched, event):
        """Closing the main menu ends the app.

        The menu is the one window the shell never destroys, so ``destroyed`` is
        not the signal that it went away — ``close()`` only hides it. Without
        this, closing the menu left the process running with nothing on screen,
        which is the one case the user cannot get out of. A child window does not
        need this: it really is destroyed, and ``_forget`` brings the menu back.
        """
        if watched is self._menu and event.type() == QEvent.Close:
            self._current = None
            self.app.quit()
        return False

    def open(self, name, **kwargs):
        """Build a window by name and put it on screen, alone.

        `kwargs` go to the window's constructor; the source video is one of
        them, and is how a window learns what to work on.

        The new window is built *before* the old one is taken down, which is
        both safer and less work than the other order:

        - If the build fails, whatever was on screen never moved, so there is
          nothing to put back. Losing the screen to an error is worse than the
          error, and the caller can keep working.
        - The outgoing window is closed *after* the build rather than before it.
          A build pumps the event loop to drive its splash, and closing first
          would mean the outgoing window was destroyed from inside the button
          handler that opened the new one. Closed afterwards, ``
          WA_DeleteOnClose`` only posts a deferred delete, which cannot run
          until that handler has returned.

        Closing it can still fail, because a window may refuse while it owns a
        worker thread. That is reported as :class:`WindowBusy` rather than
        ignored, and nothing is put on screen.
        """
        try:
            module_name, builder_name = _BUILDERS[name]
        except KeyError:
            # Read the registry rather than the WINDOW_NAMES constant, so the
            # message can only ever name the windows this shell actually has.
            raise ValueError(
                f"Unknown window {name!r}; expected one of "
                f"{', '.join(sorted(_BUILDERS))}"
            ) from None

        window = self._build(name, kwargs)
        self._adopt(window, destroy_on_close=True)

        busy = self._stand_down()
        if busy is not None:
            # The outgoing window is still up, so putting the new one on screen
            # would break the one-visible-window rule and leave both alive. Give
            # the new one back instead: its own `closeEvent` is what knows
            # whether it is holding something that must outlive it.
            window.close()
            raise WindowBusy(name)

        self._current = window
        self._present(window)
        return window

    def open_safely(self, name, **kwargs):
        """Open a window, or tell the user it could not be opened. Never raises.

        Every window opens the next one from inside a button handler, and an
        exception escaping one of those reaches the event loop and takes down
        whichever window raised — the menu, if the scanner raised. The old
        process arrangement gave each window its own process precisely so that
        did not matter; now that they share one, the rule that a failed launch
        is reported rather than raised has one owner, and this is it.

        Returns the window, or None if it could not be opened.
        """
        try:
            return self.open(name, **kwargs)
        except WindowBusy as busy:
            log(str(busy))
            QMessageBox.warning(None, "The previous window is still busy",
                                str(busy))
            return None
        except Exception as error:
            log_exception(f"the {name} window could not be opened", error)
            QMessageBox.warning(
                None,
                f"The {name} window could not start",
                f"{type(error).__name__}: {error}\n\n"
                f"Details were written to:\n{log_path()}"
            )
            return None

    @property
    def menu(self):
        """The main menu. Hidden while another window is up."""
        return self._menu

    @property
    def current(self):
        """The window the user is looking at, or None if it is the menu."""
        return self._current

    @property
    def windows(self):
        """The menu and the current window, for tests and diagnostics.

        Kept for compatibility with code that wants to know what exists; there
        is no longer a stack, and nothing navigates through this.
        """
        return (self._menu,) if self._current is None else (self._menu, self._current)

    def _build(self, name, kwargs):
        """Call a window's builder, following a redirect if it issues one.

        A redirect is resolved here rather than in the builder so that the
        builder never has to open anything itself, and it is not an error, so
        it does not go through the log-and-warn path in open_safely.
        """
        module_name, builder_name = _BUILDERS[name]
        try:
            return self._resolve(module_name, builder_name)(self.app, **kwargs)
        except OpenInstead as redirect:
            return self._build(redirect.name, redirect.kwargs)

    def _stand_down(self):
        """Take the current window off the screen, or hand it back.

        The menu is hidden, because it is reused and holds nothing. Anything
        else is closed, because closing is what runs its closeEvent — and for
        the two windows with an mpv player that is the only place the player
        gets shut down while its video frame still has a native handle. Hiding
        one of those would keep a live player attached to a window the user
        cannot see, and would move the next window on top of a window that is
        still alive underneath it.

        A window can refuse: `closeEvent` ignores the close while it still owns
        a worker thread. That refusal is returned rather than swallowed, and
        `_current` is put back, because the alternative is the one-visible-window
        rule being enforced only when it happens to be convenient — which is how
        two windows ended up alive at once, each holding a thread.
        """
        window = self._current
        if window is None:
            self._menu.hide()
            return None
        self._current = None
        if not window.close():
            self._current = window
            return window
        return None

    def _resolve(self, module_name, builder_name):
        import importlib
        return getattr(importlib.import_module(module_name), builder_name)

    def _adopt(self, window, destroy_on_close):
        """Put a window under the shell's control and watch it for closing.

        A window closes in two ways: the shell's own code calls ``close()``, or
        the user clicks the window manager's X. Both end up here. ``
        WA_DeleteOnClose`` is what makes the C++ object actually go away, and
        ``destroyed`` is therefore the one signal both paths fire.
        """
        window.setAttribute(Qt.WA_DeleteOnClose, destroy_on_close)
        window.destroyed.connect(
            lambda *_: self._forget(window))
        return window

    def _forget(self, window):
        """A window went away. The menu comes back, unless the menu is what went."""
        if window is not self._current:
            return
        self._current = None
        if not _alive(self._menu):
            # Shutting down. Windows are destroyed in an arbitrary order and the
            # menu can be one of the first, so there is nothing left to bring
            # back to.
            return
        self._present(self._menu)

    def _present(self, window):
        """Show a window and put it in front, without stealing a modal's focus.

        A raised window is not necessarily an activated one, and activating over
        a modal dialog would let the dialog lose keyboard focus.
        """
        window.show()
        window.raise_()
        if not any(
            child.isModal() for child in self.app.topLevelWidgets()
            if child.isVisible()
        ):
            window.activateWindow()


def _alive(widget):
    """True while a widget's C++ object still exists.

    A closed window is still a Python object, and at shutdown Qt destroys them
    in an arbitrary order, so "I have a reference to it" says nothing about
    whether it can still be used.
    """
    try:
        import shiboken6
    except ImportError:
        return True
    return shiboken6.isValid(widget)
