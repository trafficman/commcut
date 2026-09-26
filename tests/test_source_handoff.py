"""The chosen source has to survive every hand-off between windows.

Each window is its own process, so the path travels as a command-line argument
through two hops: picker -> scanner, scanner -> editor. Drop it at either and
the failure is quiet and confusing -- the editor opens a different video than
the one that was scanned, or falls back to the legacy default.

The picker's side of the first hop is covered by tests/test_picker.py. What is
checked here is the shape of the plumbing the window constructors rely on: the
source arrives as a parameter instead of each window re-deriving it for itself.
Neither window constructor can be built without libmpv, so most of the guard is
on signatures, plus real subprocess runs of the entry points for the two bugs
that only appear when a script is launched as a script.
"""

import importlib
import inspect
import os
import subprocess
import sys

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


# ---------------------------------------------------------------------------
# The entry points as scripts
# ---------------------------------------------------------------------------
#
# Two bugs lived here and neither shows up in the tests above, because both
# need a script to be launched as a *script* rather than imported:
#
# 1. `python scanner/scanner.py` puts scanner/ on sys.path[0], and the
#    scanner.py in it outranks the scanner/ namespace package -- a regular
#    module anywhere on sys.path beats a namespace portion collected
#    elsewhere -- so `scanner.marker_timeline` raised "'scanner' is not a
#    package". That is exactly the command launch_command() builds from
#    source, which is how the picker starts the scanner.
# 2. `sys.exit(run())` dropped sys.argv[1:], so the chosen video was ignored
#    and the window fell back to import/test.mp4.
#
# These have to run in a fresh interpreter. Reproducing the shadowing inside
# the test process is pointless: by then `scanner` is already in sys.modules
# as the package, so the import succeeds however sys.path is arranged and the
# test passes against broken code.

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.parametrize("script", [
    os.path.join("scanner", "scanner.py"),
    os.path.join("editor", "editor.py"),
])
def test_a_source_argument_reaches_a_window_run_as_a_script(script, tmp_path):
    """Launched the way launch_command() launches it from source.

    A path outside the import folder fails in require_source_video, before any
    window exists, and the message names the path that was passed -- so this
    asserts that the module header imported (no ModuleNotFoundError) *and*
    that the argument survived the trip through __main__ to run().

    The timeout is what a regression looks like when the argument is dropped:
    the window falls back to import/test.mp4, finds its .cmct, and opens a
    window nobody asked for, so the run would otherwise never end.
    """
    outside = str(tmp_path / "elsewhere.mp4")
    with open(outside, "wb") as handle:
        handle.write(b"\0")

    result = subprocess.run(
        [sys.executable, os.path.join(PROJECT_ROOT, script), outside],
        capture_output=True, text=True, timeout=60,
    )

    assert "ModuleNotFoundError" not in result.stderr, result.stderr
    assert outside in result.stderr, result.stderr
    assert "import folder" in result.stderr, result.stderr
