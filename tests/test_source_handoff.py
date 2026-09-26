"""The chosen source has to survive every hand-off between windows.

Each window is its own process, so the path travels as a command-line argument
through two hops: picker -> scanner, scanner -> editor. Drop it at either and
the failure is quiet and confusing -- the editor opens a different video than
the one that was scanned, or falls back to the legacy default.

The picker's side of the first hop is covered by tests/test_picker.py. What is
checked here is the shape of the plumbing the window constructors rely on: the
source arrives as a parameter instead of each window re-deriving it for itself.
Neither window constructor can be built without libmpv, so the guard is on the
signature, which is the thing that regressed.
"""

import importlib
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


@pytest.mark.parametrize("module,run_name", [
    ("editor.editor", "run"),
    ("scanner.scanner", "run"),
])
def test_run_accepts_an_optional_source(module, run_name):
    """Running a window's script directly with no argument has to keep
    working, so the source is optional rather than required."""
    run = getattr(importlib.import_module(module), run_name)
    parameters = inspect.signature(run).parameters

    assert "source" in parameters
    assert parameters["source"].default is None


def test_every_window_run_tolerates_forwarded_arguments():
    """main.py forwards argv to every window uniformly. One whose run() did not
    accept an argument would fail only when someone hand-edited a command
    line."""
    for module in ("editor.editor", "scanner.scanner", "settings.settings",
                   "picker.picker"):
        run = getattr(importlib.import_module(module), "run")
        # Either it takes **args or a plain positional; neither is a fixed
        # no-argument signature.
        kinds = [p.kind for p in inspect.signature(run).parameters.values()]
        assert inspect.Parameter.VAR_POSITIONAL in kinds or any(
            kind == inspect.Parameter.POSITIONAL_OR_KEYWORD for kind in kinds), module


def test_no_window_re_derives_the_source_for_itself():
    """The failure this guards against: a window that resolves its own source
    instead of using the one it was given."""
    for module in ("editor.editor", "scanner.scanner"):
        source = inspect.getsource(importlib.import_module(module))
        assert "source_video_path(" not in source, module


def test_a_scanned_video_is_not_rescanned(tmp_path):
    """The .cmct rule the picker advertises in its list: a video that was
    scanned before goes straight to the editor."""
    from scanner.scanner import _editor_to_launch

    source = str(tmp_path / "compilation.mp4")
    with open(source, "wb") as handle:
        handle.write(b"\0")

    assert _editor_to_launch(source) is None

    with open(os.path.splitext(source)[0] + ".cmct", "w") as handle:
        handle.write("{}")

    assert _editor_to_launch(source) == "editor"
