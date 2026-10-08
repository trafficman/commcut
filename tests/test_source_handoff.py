"""The chosen source has to survive every hand-off between windows.

The path travels as a constructor argument through two hops: the main menu's file
dialog -> scanner, scanner -> editor. Drop it at either and the failure is quiet
and confusing -- the editor opens a different video than the one that was
scanned, or falls back to the legacy default.

Each hop used to cross a process boundary, which made the shape of the plumbing
load-bearing in a way it no longer is: there was a `run()` whose signature had to
tolerate forwarded argv, and two bugs that only appeared when a window's script
was launched as a *script* rather than imported (the scanner's package name
shadowed by its own directory, and `sys.exit(run())` dropping `sys.argv[1:]`).
None of that exists now -- `main.py` is the only entry point, the menu asks with
a native dialog, and the shell passes the path straight to a builder -- so those
tests are gone rather than converted, and what is left is the part that would
still be a bug.

The menu's side of the first hop is covered by tests/test_main_window.py, and the
shell's own half -- that the path is what the window is opened *with* -- by
tests/test_session.py.
"""

import inspect
import os

import pytest


def test_the_editor_window_takes_its_source_as_an_argument():
    """MediaPlayer used to call source_video_path() itself. That is exactly how
    a handed-down path could be silently ignored and a different video opened."""
    from editor.editor import MediaPlayer

    parameters = list(inspect.signature(MediaPlayer.__init__).parameters)

    assert parameters[:2] == ["self", "media_path"]


def test_the_scanner_window_takes_its_source_as_an_argument():
    from scanner.scanner import ScannerWindow

    parameters = list(inspect.signature(ScannerWindow.__init__).parameters)

    assert parameters[:2] == ["self", "source_path"]
    # The preview clip and keyframe list are now built on a worker thread in
    # create() and passed to the constructor, rather than ScannerWindow building
    # its own preview via clip_to_temp.
    assert parameters[2:4] == ["clip_path", "keyframes"]


@pytest.mark.parametrize("module_name,builder_name", [
    # Both are reached from the shell with a path: the scanner from the menu's
    # file dialog, the editor from the scanner and from the already-scanned
    # redirect.
    ("scanner.scanner", "create"),
    ("editor.editor", "create"),
])
def test_the_builders_the_shell_calls_require_a_source(
        module_name, builder_name):
    """The two hops go through Shell.open(name, source=path).

    `source` has to be a *named, required* parameter on the builder, because that
    is the keyword the caller passes. A builder that took only **kwargs would
    swallow it and then open whatever default it had, which is the same quiet
    wrong video the process model used to allow -- and there is no fallback left
    to land in, so a missing argument has to be a loud failure at the builder.
    """
    import importlib

    builder = getattr(importlib.import_module(module_name), builder_name)
    parameters = inspect.signature(builder).parameters

    assert "source" in parameters, f"{module_name}.{builder_name}"
    assert parameters["source"].default is inspect.Parameter.empty


def test_the_settings_builder_takes_nothing_but_the_app():
    """It is opened with no arguments at all, so a second parameter would mean
    the menu's button could not open it — and a required one would mean a caller
    had to invent a value the window has no use for."""
    import importlib

    builder = importlib.import_module("settings.settings").create
    parameters = list(inspect.signature(builder).parameters)

    assert parameters == ["app"], f"settings.settings.create{tuple(parameters)}"


def test_a_scanned_video_is_not_rescanned(tmp_path):
    """The .cmct rule, which the picker used to advertise in its list and which
    is now invisible but unchanged: a video that was scanned before goes
    straight to the editor."""
    from scanner.scanner import _editor_to_launch

    source = str(tmp_path / "compilation.mp4")
    with open(source, "wb") as handle:
        handle.write(b"\0")

    assert _editor_to_launch(source) is False

    with open(os.path.splitext(source)[0] + ".cmct", "w") as handle:
        handle.write("{}")

    assert _editor_to_launch(source) is True
