"""The application icon: which file wins, and what happens when there is none.

`shared/icons.py` picks between two files — a multi-frame `.ico` and a PNG — and
has to answer for a third case the tests here exist to pin: neither of them is in
the tree, and that is not a failure. These drive the module directly, against
files the test writes, for the reason `tests/test_splash.py` gives: what this code
does is *choose and fall back*, and a re-implementation of the choice would agree
with it right up until the platform changed.

Two of the images here are built rather than shipped, so the assertions are about
the rule and not about anybody's artwork. The last test is the one that looks at
the real files, and skips while they are absent.
"""

import os
import struct

import pytest
from PySide6.QtGui import QColor, QIcon, QImage, QImageReader

from editor_stub import ensure_qapp
from shared import icons as icons_module
from shared.environment import resource_path


@pytest.fixture(scope="module")
def qapp():
    return ensure_qapp()


@pytest.fixture
def art(tmp_path, monkeypatch, qapp):
    """Point both halves of the icon at files the test controls.

    Absolute paths, which `resource_path()` passes through untouched because the
    second half of an `os.path.join` wins — the same trick
    `tests/test_splash.py` uses for the banner, and the reason the module's
    constants are tuples of parts rather than one joined string.

    Depends on `qapp` rather than leaving it to each test: `QIcon` builds a
    `QPixmap`, which aborts the process outright when no `QGuiApplication`
    exists, so a test here that forgets the fixture does not fail — it takes the
    whole run down with it.
    """
    png = tmp_path / "commcut_icon.png"
    ico = tmp_path / "commcut_icon.ico"
    monkeypatch.setattr(icons_module, "ICON_PNG", (str(png),))
    monkeypatch.setattr(icons_module, "ICON_ICO", (str(ico),))
    return png, ico


def _png(path, size=64):
    """A PNG with a real alpha channel, so "did it load" is never in question.

    Semi-transparent rather than fully transparent, because a wholly clear image
    is a legitimate file and an unhelpful test fixture: some image handlers treat
    "no visible pixel" as nothing to decode, and the question being asked here is
    about *loading*, never about appearance.
    """
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(QColor(10, 20, 30, 128))
    assert image.save(str(path)), path
    return path


def _ico(path, size=32):
    """A one-frame .ico whose frame is a PNG, which is what Vista-era tools write.

    Assembled here rather than shipped, because Pillow is not a dependency
    (`requirements.txt` says so) and because the point is only that the frame is
    *decodable* and *distinguishable by size* — the fallback logic keys off which
    file produced the icon, and two different sizes is how a test can tell.
    """
    data = _png(path.parent / (path.stem + ".frame.png"), size).read_bytes()
    directory = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(data), 22)
    path.write_bytes(directory + entry + data)
    return path


def _sizes(icon):
    """An icon's frame sizes as a set of `(width, height)` tuples.

    Set rather than a list so the assertions read as "the 32px frame and no
    other", which is the claim being made: an icon built from the PNG at a
    different size is a *different* answer, not a smaller version of the same
    one.
    """
    return {(size.width(), size.height()) for size in icon.availableSizes()}


# ---------------------------------------------------------------------------
# Which file wins
# ---------------------------------------------------------------------------

def test_app_icon_prefers_the_ico_where_qt_can_read_one(art):
    """Windows gets the multi-frame `.ico`, and the sizes say so.

    Skipped where this build of Qt cannot decode ICO at all. That is not the
    normal case — the macOS and Linux legs both read the shipped `.ico`, which
    is what corrected an earlier claim here that the format was Windows-only —
    but a Qt built without the handler must still land on the PNG rather than on
    an empty icon, and that is what the skip exists to allow.
    """
    png, ico = art
    _ico(ico, size=32)
    _png(png, size=64)

    if QIcon(str(ico)).isNull():
        pytest.skip("this Qt build has no ICO reader; the PNG is the fallback")

    assert _sizes(icons_module.app_icon()) == {(32, 32)}


def test_app_icon_falls_back_to_the_png_when_there_is_no_ico(art):
    """The half-delivered pair, and the ordinary macOS and Linux case.

    Both are the same shape: nothing readable at the `.ico` path. Asserting on
    the size rather than on a non-null icon is what makes it the fallback rather
    than merely a load.
    """
    png, _ico_path = art
    _png(png, size=64)

    assert _sizes(icons_module.app_icon()) == {(64, 64)}


def test_app_icon_falls_back_to_the_png_when_the_ico_cannot_be_read(art):
    """A file that exists and is not an icon is the case a `isfile` check misses.

    A truncated copy, a JPEG someone renamed, an `.ico` that was never
    converted. Checking existence would pass it through to a `QIcon` that has
    nothing in it, and the window would show Qt's default with nothing logged —
    which is why `shared/icons.py` asks `QIcon.isNull()` instead.
    """
    png, ico = art
    _png(png, size=64)
    ico.write_bytes(b"not an icon")

    assert _sizes(icons_module.app_icon()) == {(64, 64)}


def test_app_icon_is_null_and_says_where_it_looked_when_there_is_no_art(
        art, monkeypatch):
    """Neither file: a null `QIcon` and one log line, and nothing else.

    The log is the whole point. A window with no icon is not a bug report anyone
    can act on, and "the icon did not load" with no path in it leaves the reader
    guessing which of the two files was wanted. Both absolute paths are named,
    because on a packaged build neither of them is where a person would look.
    """
    logged = []
    monkeypatch.setattr(icons_module, "log", logged.append)

    assert icons_module.app_icon().isNull()
    assert len(logged) == 1
    for part in icons_module.ICON_PNG + icons_module.ICON_ICO:
        assert part in logged[0]


# ---------------------------------------------------------------------------
# Where it is installed
# ---------------------------------------------------------------------------

def test_install_app_icon_gives_the_application_a_usable_icon(qapp, art):
    png, _ico_path = art
    _png(png, size=64)

    installed = icons_module.install_app_icon(qapp)

    assert not installed.isNull()
    assert not qapp.windowIcon().isNull()


def test_installing_a_missing_icon_leaves_the_application_usable(qapp, art,
                                                                monkeypatch):
    """The absent-art case has to be survivable, not merely quiet.

    A `QApplication` with a null icon is a perfectly good `QApplication`: the
    windows open, Qt draws its own default, and nothing about the install
    raises. That is the property `shared/icons.py` trades the log line for, and
    it is worth asserting rather than reasoning about.
    """
    monkeypatch.setattr(icons_module, "log", lambda *_: None)

    assert icons_module.install_app_icon(qapp).isNull()


def test_a_window_constructed_afterwards_inherits_it(qapp, art):
    """The claim that makes one call in `main.py` enough for all seven windows.

    Qt takes the application icon as the default for every widget constructed
    after it, which is what makes this a one-line feature instead of seven. A
    bare `QWidget` is the honest subject: it has no `.ui`, no `adopt_title`, and
    no builder, so what is being observed is the propagation and nothing else.
    """
    from PySide6.QtWidgets import QWidget

    png, _ico_path = art
    _png(png, size=64)
    icons_module.install_app_icon(qapp)

    window = QWidget()
    assert not window.windowIcon().isNull()


def test_the_menu_window_carries_it(qapp, art, tmp_path):
    """And once more through the app's own first window.

    `MainWindow` is what the user actually sees first, and it is built the way
    every other window is: from a `.ui` through `adopt_title`, with no icon
    call of its own. If this passed while a real window did not, the feature
    would be one `.ui` away from not existing.
    """
    from mainwindow import MainWindow

    png, _ico_path = art
    _png(png, size=64)
    icons_module.install_app_icon(qapp)

    window = MainWindow()
    try:
        assert not window.windowIcon().isNull()
    finally:
        window.deleteLater()


def test_main_installs_the_icon_before_it_builds_the_menu():
    """The one call in `main.py`, and the order it has to happen in.

    Nothing else in this file can catch its absence. Every other test drives
    `shared.icons` directly, so a `main.py` with the line removed would leave
    the module correct, every window inheriting nothing, and the suite green —
    because a window without an icon is not an error, it is just a window. The
    order matters as much as the call: an icon installed after `MainWindow()` is
    built reaches the menu too late to be its default, and the menu is the one
    window that is never rebuilt.

    Read from the source rather than by running `run_main_menu`, because that
    constructs a `QApplication` and a second one in a process that already has
    one is refused by Qt outright — the widget tests here need a live
    application, and there is no ordering of two tests that gives both.
    """
    import inspect

    import main

    source = inspect.getsource(main.run_main_menu)

    assert "install_app_icon(app)" in source
    assert source.index("install_app_icon(app)") < source.index("MainWindow()")


# ---------------------------------------------------------------------------
# The artwork, once there is some
# ---------------------------------------------------------------------------

#: The frame sizes Windows asks an executable icon for, and so the frames an
#: `.ico` has to carry. A subset rather than the whole set: the shipped file also
#: carries 96x96, and demanding an exact list would fail on a file that is merely
#: *different* from the one these were written against rather than on a wrong
#: one. What this catches is the realistic mistake — an `.ico` exported as a
#: single frame, which has every size but the first.
REQUIRED_ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def _ico_frames(path):
    """Every frame in an `.ico` as `(width, height, has_alpha)`.

    **`QImage(path)` is not this.** It loads the first frame and discards the
    rest, and an `.ico`'s first frame is always its smallest — so measuring one
    with `QImage` reports 16x16 for a file that carries a 256px frame, and fails
    an icon that has every size in it. That is not a hypothetical: it is what
    this file asserted when the artwork first landed, on all three platforms.
    `QImageReader.jumpToNextImage()` is the walk.
    """
    reader = QImageReader(str(path))
    frames = []
    while True:
        image = QImage(reader.read())
        if image.isNull():
            break
        frames.append((image.width(), image.height(), image.hasAlphaChannel()))
        if not reader.jumpToNextImage():
            break
    return frames


def _artwork_path(constant):
    """`resource_path()` for one of the icon constants, skipping if it is absent.

    Skipped from inside the test rather than through a module-level `skipif`, so
    the answer is read at run time: dropping a file into `assets/` makes this
    start asserting without anything being edited.
    """
    parts = getattr(icons_module, constant)
    path = resource_path(*parts)
    if not os.path.isfile(path):
        pytest.skip(f"{'/'.join(parts)} is not in the tree yet")
    return parts, path


def test_the_shipped_png_is_square_large_and_carries_an_alpha_channel(qapp):
    """The cross-platform image, measured as a single image.

    All three properties are load-bearing. Non-square art is *distorted* rather
    than letterboxed, because `QIcon.pixmap(size)` scales to a square rect; no
    alpha means a hard rectangle on a title bar and on the Dock tile; and below
    256 there is nothing to scale *down* from on a high-DPI display.
    """
    parts, path = _artwork_path("ICON_PNG")

    image = QImage(path)

    assert not image.isNull(), f"{'/'.join(parts)} could not be read"
    assert image.width() == image.height(), (
        f"{'/'.join(parts)} is {image.width()}x{image.height()}; QIcon scales to "
        f"a square rect without keeping the aspect ratio, so this would be "
        f"distorted rather than letterboxed")
    assert max(image.width(), image.height()) >= 256, (
        f"{'/'.join(parts)} is {image.width()}px; there is nothing to scale "
        f"down from for a high-DPI display")
    assert image.hasAlphaChannel(), (
        f"{'/'.join(parts)} has no alpha channel, so it renders as a hard "
        f"rectangle against a title bar")


def test_the_shipped_ico_carries_every_frame_size_windows_asks_for(qapp):
    """The executable icon, measured frame by frame rather than as one image.

    Two claims, and the second is the one that matters operationally. Every frame
    is square and has alpha, because a single non-conforming frame is a size at
    which the window draws a rectangle. And every required size is *offered* by
    the `QIcon` the app installs: `availableSizes()` is what Qt picks from when
    something asks for a particular size, so a file whose frames the icon cannot
    see is an `.ico` that silently loses its sharp sizes.
    """
    parts, path = _artwork_path("ICON_ICO")

    frames = _ico_frames(path)

    assert frames, f"{'/'.join(parts)} has no readable frames"

    for width, height, alpha in frames:
        assert width == height, (
            f"{'/'.join(parts)} has a {width}x{height} frame; a non-square frame "
            f"is distorted rather than letterboxed")
        assert alpha, (
            f"{'/'.join(parts)} has a {width}x{width} frame with no alpha "
            f"channel, which renders as a hard rectangle against a title bar")

    present = {width for width, _, _ in frames}
    missing = [size for size in REQUIRED_ICO_SIZES if size not in present]
    assert not missing, (
        f"{'/'.join(parts)} is {sorted(present)}; it has no "
        f"{', '.join(str(size) for size in missing)} frame. A single-frame "
        f"export carries every size but the first and looks fine in a file "
        f"browser. Rebuild it with every size in it:\n\n"
        f"  magick assets/commcut_icon.png "
        f"-define icon:auto-resize={','.join(str(s) for s in REQUIRED_ICO_SIZES)} "
        f"assets/commcut_icon.ico")

    offered = {size.width() for size in QIcon(str(path)).availableSizes()}
    missing = [size for size in REQUIRED_ICO_SIZES if size not in offered]
    assert not missing, (
        f"the QIcon built from {'/'.join(parts)} offers {sorted(offered)}; "
        f"{missing} are in the file but not in the icon")
