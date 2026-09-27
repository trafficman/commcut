"""Tests for how the mpv player is built, without a player or a video.

`create_mpv_player` is the one place the app's Qt and mpv worlds meet: it turns
a widget into a native handle, hands that to mpv, and chooses the video output
driver. None of that needs libmpv to be exercised, so the `mpv` module is
replaced with a recorder and the frame with an object that reports whatever
handle the test wants.

This is the only coverage the embedding path has. Nothing else in the suite
constructs a real player -- `tests/editor_stub.py`'s `FakeBridge` stands in for
one by design -- so the two things that are easy to get wrong here, and that
fail silently when wrong, are asserted here: the libmpv load happening *before*
the import that depends on it, and a zero handle never being handed to mpv.
"""

import contextlib
import sys
from types import SimpleNamespace

import pytest

from shared.mpv import create_mpv_player


class FakeFrame:
    """The QFrame surface create_mpv_player uses, and nothing else."""

    def __init__(self, handle=0x2A2A):
        self.handle = handle
        self.attributes = []

    def setAttribute(self, attribute, value):
        self.attributes.append((attribute, value))

    def winId(self):
        return self.handle


@pytest.fixture
def events():
    """Appends to, so ordering between steps can be asserted."""
    return []


@pytest.fixture
def fake_mpv(monkeypatch, events):
    """Replace the `mpv` module with a recorder, and report the construction."""
    recorded = {}

    class FakePlayer:
        def __init__(self, **kwargs):
            recorded.update(kwargs)
            events.append(("construct", kwargs.get("wid")))

    monkeypatch.setitem(
        sys.modules, "mpv", SimpleNamespace(MPV=FakePlayer))
    return recorded


@pytest.fixture
def prepared(monkeypatch, events):
    """Stand in for the libmpv load, recording that it happened first."""
    @contextlib.contextmanager
    def import_context():
        events.append(("prepare", None))
        yield None

    monkeypatch.setattr("shared.mpv.mpv_import_context", import_context)


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

def test_libmpv_is_loaded_before_mpv_is_imported(prepared, fake_mpv, events):
    """The whole reason the import is deferred inside the function.

    python-mpv resolves libmpv for itself the moment it is imported, and its
    POSIX branch *raises* when that lookup comes back empty rather than falling
    back to a library that is already loaded -- so the load has to happen, and
    the lookup has to be answered, before the import rather than after.
    """
    create_mpv_player(FakeFrame())

    assert [name for name, _ in events] == ["prepare", "construct"]


def test_a_missing_libmpv_surfaces_before_anything_is_built(
    monkeypatch, fake_mpv, events
):
    """Named and actionable, rather than a bare ctypes OSError from inside
    python-mpv telling the user to read the find_library documentation."""
    @contextlib.contextmanager
    def refuse():
        raise FileNotFoundError("Could not find the libmpv library. Looked in:")
        yield  # pragma: no cover - the generator never runs

    monkeypatch.setattr("shared.mpv.mpv_import_context", refuse)

    with pytest.raises(FileNotFoundError, match="libmpv"):
        create_mpv_player(FakeFrame())

    assert "construct" not in [name for name, _ in events]


# ---------------------------------------------------------------------------
# The handle
# ---------------------------------------------------------------------------

def test_the_frame_native_handle_is_handed_to_mpv(prepared, fake_mpv):
    """On Windows a child HWND, on macOS an NSView*, on Linux a window: mpv
    embeds into whichever one the platform uses, so the integer form is the
    portable part."""
    create_mpv_player(FakeFrame(handle=0x1234))

    assert fake_mpv["wid"] == "4660"


def test_a_zero_handle_is_refused_rather_than_handed_to_mpv(
    prepared, fake_mpv, events
):
    """A frame that never got a native handle leaves winId() at 0, and the
    string "0" is a valid-looking handle that makes mpv render nothing at all.

    That failure mode is indistinguishable from a broken build, which is why it
    is refused here instead.
    """
    with pytest.raises(RuntimeError) as error:
        create_mpv_player(FakeFrame(handle=0))

    assert "native window handle" in str(error.value)
    assert "construct" not in [name for name, _ in events]


# ---------------------------------------------------------------------------
# The frame and the driver
# ---------------------------------------------------------------------------

def test_the_native_window_attribute_is_set(prepared, fake_mpv):
    """Required on every platform, not just the one whose driver it was
    written for: winId() is what produces the handle in the first place, and
    without the attribute it returns 0."""
    from PySide6.QtCore import Qt

    frame = FakeFrame()
    create_mpv_player(frame)

    assert frame.attributes == [(Qt.WA_NativeWindow, True)]


def test_the_platform_video_output_is_requested(prepared, fake_mpv):
    from shared.environment import video_output

    create_mpv_player(FakeFrame())

    assert fake_mpv["vo"] == video_output()


def test_the_player_options_are_the_documented_set(prepared, fake_mpv):
    """These are the app's playback behaviour, and they are set here rather
    than left to mpv's defaults, so they are worth pinning: the on-screen
    controller and mpv's own key bindings would otherwise both fight the
    editor for the keyboard."""
    create_mpv_player(FakeFrame())

    assert fake_mpv["osc"] is False
    assert fake_mpv["input_default_bindings"] is False
    assert fake_mpv["input_vo_keyboard"] is False
    assert fake_mpv["keep_open"] is True
    assert fake_mpv["hr_seek"] == "always"
