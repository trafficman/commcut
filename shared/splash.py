"""Low-level splash screen building blocks: the banner pixmap and ``show_splash``.

Applies to: `shared/splash.py`, `shared/loading.py` (`LoadingSplash` re-exports
from here), `assets/commcut_banner.png`, `packaging/commcut.spec` (`UI_DATAS`).

The two windows that need a splash — the scanner and the editor — now build one
through `LoadingSplash` in `shared/loading.py`, which wraps the pixmap painter
and `processEvents` pump defined here. ``show_splash`` is retained for the
startup splash in `main.py` and for `tests/test_splash.py`, which measures the
painted pixels directly.

**The splash is decoration, and it is not allowed to become a dependency.** A
banner that cannot be read leaves a plain splash and a log line; the window still
opens. The alternative — a window that will not appear because an image was
missing from the payload — turns a cosmetic packaging mistake into a dead app.

**The background is white because the banner is.** `assets/commcut_banner.png` has
an opaque white background and dark artwork, so a dark splash would frame the logo
in a white rectangle and the black wordmark would sit on near-black. Filling with
the banner's own colour makes its edges disappear into the splash, which is the
whole trick: the logo looks like it is on the screen rather than pasted onto it,
and the message below it is dark for the same reason.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QSplashScreen

from shared.diagnostics import log
from shared.environment import resource_path

#: The banner, as (folder, name) under the project root — the form
#: `resource_path()` takes, and the layout the spec's `UI_DATAS` mirrors so one
#: expression resolves the same way frozen and unfrozen.
BANNER = ("assets", "commcut_banner.png")

#: The splash's size. Unchanged from the pixmap the call sites used to fill by
#: hand, so the window that opens afterwards is not a different size than before.
SPLASH_WIDTH = 480
SPLASH_HEIGHT = 270

#: Blank space around the banner, and the room left at the bottom for the message
#: `showMessage` paints into. The two together are what keep the logo above the
#: text: the banner is scaled into the space that is left over, so it can never
#: grow down into the band the caption is drawn in.
MARGIN = 20
MESSAGE_BAND = 44

BACKGROUND = QColor(255, 255, 255)
MESSAGE_COLOR = QColor(60, 60, 60)


def banner_box() -> QRect:
    """The area the banner may occupy: everything above the message band."""
    return QRect(
        MARGIN,
        MARGIN,
        SPLASH_WIDTH - 2 * MARGIN,
        SPLASH_HEIGHT - 2 * MARGIN - MESSAGE_BAND,
    )


def splash_pixmap() -> QPixmap:
    """The image the splash shows: the banner over a plain background.

    Scaled to fit `banner_box()` with its aspect ratio kept, and centred in
    whatever is left over. Stretching it to fill the box is the one thing this
    must not do: the wordmark is hand-drawn lettering, and a non-uniform scale
    makes it look broken rather than scaled.

    Centred on `box.x() + (box.width() - scaled.width()) // 2` rather than on
    `box.center()`, because `QRect::center()` is the integer midpoint of an
    *inclusive* span and lands an even-sized draw one pixel left and high of where
    it belongs. Sub-pixel, invisible, and worth not carrying into the one line
    that has to be right about placement.
    """
    pixmap = QPixmap(SPLASH_WIDTH, SPLASH_HEIGHT)
    pixmap.fill(BACKGROUND)

    image = _banner_image()
    if image is None:
        return pixmap

    box = banner_box()
    scaled = image.scaled(
        box.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    painter.drawImage(
        QPoint(box.x() + (box.width() - scaled.width()) // 2,
               box.y() + (box.height() - scaled.height()) // 2),
        scaled,
    )
    painter.end()
    return pixmap


def show_splash(app, message: str) -> QSplashScreen:
    """Put the splash on screen with `message` under the banner, and return it.

    **The caller closes it.** That is not tidiness: invariant 6 requires the
    splash to be closed before any mpv-backed widget is constructed, and both call
    sites close it immediately before building their window. A `with` block here
    would end at the end of the *scan* instead, which is the wrong place by a long
    way — hence a returned object rather than a context manager.

    `processEvents()` runs once, after the message is set. That is not optional:
    the ffprobe scan it covers is synchronous on the GUI thread, so without a pump
    the splash is never painted and the user stares at the previous window until
    the scan finishes — which is the exact thing it was added to hide.
    """
    splash = QSplashScreen(splash_pixmap())
    splash.show()
    splash.showMessage(
        message, Qt.AlignHCenter | Qt.AlignBottom, MESSAGE_COLOR)
    app.processEvents()
    return splash


def _banner_image() -> QImage | None:
    """The banner image, or None if it is missing or unreadable.

    Logged and dropped rather than raised. See the module docstring: this is
    decoration on a progress screen, and the one failure worth avoiding is a
    window that never opens because a PNG did not make it into the payload.
    """
    path = resource_path(*BANNER)
    image = QImage(path)
    if image.isNull():
        log(f"the splash banner could not be read, so the splash is plain: {path}")
        return None
    return image