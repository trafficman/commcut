"""The loading splash: the banner is drawn, and it stays out of the text.

The two windows used to build this by hand and identically, which is the whole
reason it is a module now — so these tests drive `shared/splash.py` directly, and
they measure the painted pixels rather than asserting on the calls, because the
two ways a splash gets visibly wrong are both pixel facts: a logo that is loaded
and then not drawn, and a logo drawn over the caption.
"""

import os

import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QSplashScreen

from editor_stub import ensure_qapp
from shared import splash as splash_module


@pytest.fixture(scope="module")
def qapp():
    return ensure_qapp()


@pytest.fixture
def splash_env(monkeypatch, tmp_path):
    """Point the banner at a file the test controls, and return its path."""
    banner = tmp_path / "banner.png"
    monkeypatch.setattr(splash_module, "BANNER", (str(banner),))
    return banner


def solid_image(width, height, colour=(0, 0, 0)):
    """A wholly opaque image, so the pixels that were painted are countable.

    Deliberately not the real banner: what the splash does is scale and place an
    image, and measuring that should not depend on a hand-drawn letterform.
    """
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(*colour))
    return image


def painted_bounds(pixmap):
    """`(left, top, right, bottom)` of everything that is not the background."""
    image = pixmap.toImage()
    points = [
        (x, y)
        for y in range(pixmap.height())
        for x in range(pixmap.width())
        if image.pixelColor(x, y) != splash_module.BACKGROUND
    ]
    if not points:
        return None
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return min(xs), min(ys), max(xs), max(ys)


def message_band():
    """The strip at the bottom that `showMessage` paints its caption into."""
    return QRect(
        0,
        splash_module.SPLASH_HEIGHT - splash_module.MESSAGE_BAND,
        splash_module.SPLASH_WIDTH,
        splash_module.MESSAGE_BAND,
    )


@pytest.mark.parametrize("source,expected_size", [
    # Width binds: a 3:1 banner in a box wider than 3:1 keeps its ratio and is
    # centred in what is left over, rather than stretched to fill.
    ((300, 100), (440, 146)),
    # Height binds, which is the case a fill-to-width implementation gets wrong.
    ((100, 300), (62, 186)),
])
def test_the_banner_is_scaled_to_fit_and_centred(splash_env, monkeypatch, qapp,
                                                 source, expected_size):
    """The logo appears, keeps its proportions, and lands above the caption.

    A splash that stretches the banner to fill looks fine in a unit test and
    distorted on screen, and one that is allowed to fill the whole pixmap covers
    the text — so the drawn extent is asserted exactly, in both directions of the
    aspect ratio.
    """
    _stub_banner(monkeypatch, solid_image(*source))

    pixmap = splash_module.splash_pixmap()

    box = splash_module.banner_box()
    width, height = expected_size
    left = box.x() + (box.width() - width) // 2
    top = box.y() + (box.height() - height) // 2
    assert painted_bounds(pixmap) == (
        left, top, left + width - 1, top + height - 1
    ), "scaled to fit the box and centred in it, not stretched to fill"
    assert box.contains(left, top) and box.contains(left + width - 1,
                                                   top + height - 1)


def test_nothing_is_painted_where_the_caption_goes(splash_env, monkeypatch, qapp):
    """The two claims above together mean the message band stays clean, which is
    the property a user would actually notice."""
    _stub_banner(monkeypatch, solid_image(300, 100))

    pixmap = splash_module.splash_pixmap()

    image = pixmap.toImage()
    band = message_band()
    for y in range(band.top(), band.bottom() + 1):
        for x in range(band.left(), band.right() + 1):
            assert image.pixelColor(x, y) == splash_module.BACKGROUND, (
                f"something was painted at ({x}, {y}), in the band "
                f"showMessage draws its caption into")


def test_a_banner_that_cannot_be_read_leaves_a_plain_splash(
        splash_env, monkeypatch, qapp):
    """Decoration must never become a dependency.

    A PNG missing from the payload is a packaging mistake, and the worst possible
    response to one is a window that will not open. So the splash falls back to
    its background and the window opens as before — which is only true because
    nothing between here and the constructor of the mpv-backed widget looks at the
    return value.
    """
    # No stub: the real _banner_image reads a path that does not exist.
    logged = []
    monkeypatch.setattr(splash_module, "log", logged.append)

    pixmap = splash_module.splash_pixmap()

    assert (pixmap.width(), pixmap.height()) == (
        splash_module.SPLASH_WIDTH, splash_module.SPLASH_HEIGHT)
    assert painted_bounds(pixmap) is None, "nothing should have been painted"
    assert logged and splash_env.name in logged[0], (
        "and the reason belongs in the log, since the fallback is invisible")


def test_the_splash_shows_the_message_and_pumps_the_loop(splash_env, monkeypatch,
                                                          qapp):
    """`show_splash` has to paint before the synchronous scan it covers runs.

    Asserted through a counting stand-in rather than by eye, because
    `processEvents` returning is the only thing standing between this splash and a
    user who watches a frozen window for the length of an ffprobe run.
    """
    _stub_banner(monkeypatch, solid_image(*(720, 303)))

    class CountingApp:
        """Enough of a QApplication for `show_splash`, which uses one method."""

        def __init__(self, app):
            self.app = app
            self.pumps = 0

        def processEvents(self):
            self.pumps += 1
            self.app.processEvents()

    app = CountingApp(qapp)

    splash = splash_module.show_splash(app, "Loading something…")

    try:
        assert isinstance(splash, QSplashScreen)
        assert splash.message() == "Loading something…"
        assert app.pumps == 1
        assert splash.pixmap().size() == splash_module.splash_pixmap().size()
    finally:
        splash.close()
        splash.deleteLater()


def test_the_banner_lives_where_the_spec_bundles_it():
    """The one failure a unit test cannot see: a file that resolves from source and
    is not in the payload is a packaged build with no banner at all.

    `test_frozen_mode.test_source_and_payload_layouts_agree` asserts this for
    every entry in `PAYLOAD_FILES`, which now includes the banner — this is only
    here so the pairing in `splash.py` is pinned from its own side too, since
    `BANNER` is what the code asks for and `BANNER_FILES` is what the spec must
    carry.
    """
    from test_frozen_mode import BANNER_FILES

    assert splash_module.BANNER in BANNER_FILES


def _stub_banner(monkeypatch, image):
    monkeypatch.setattr(splash_module, "_banner_image", lambda: image)