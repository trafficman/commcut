"""The application's icon: one image, set once, inherited by every window.

Applies to: `shared/icons.py`, `assets/commcut_icon.png`,
`assets/commcut_icon.ico`, `main.py`, `packaging/commcut.spec` (`UI_DATAS`,
`EXE_KWARGS`), `packaging/build.py` (`OPTIONAL_PAYLOAD_FILES`,
`check_icon`), `tests/test_icons.py`.

**Why it is set on the `QApplication` and not on each window.** There is one
`QApplication` (`main.py`) and every window is built in-process by
`shared/session.py`, so a window icon set on the application is the default for
every widget constructed afterwards: all seven windows, and every modal dialog
including `Shell.open_safely`'s `QMessageBox`. One call in one place is the
whole feature. A per-window `setWindowIcon` would be seven copies of one value,
and the dialogs would be the seven-and-eighth copy nobody remembered.

**Why the icon has two files.** `assets/commcut_icon.png` is the cross-platform
image; `assets/commcut_icon.ico` carries the per-size frames Windows wants in a
16px title bar. They are not redundant, and neither is optional in its own
right:

- Qt's ICO image plugin is **Windows-only**, so on macOS and Linux an icon built
  from the `.ico` alone is empty. The PNG is what those platforms show.
- PyInstaller's ``icon=`` on Windows accepts only ``.ico``/``.exe``, and
  `requirements.txt` has no Pillow to convert anything else. So the `.ico` is
  what Explorer's icon for ``commcut.exe`` is made of.

`app_icon()` therefore asks for the `.ico`, takes it when this platform can
decode it, and falls back to the PNG. On Windows the `.ico` wins, because its
frames are sharper than a 512px PNG downscaled to a title bar.

**A missing icon is a null icon and one log line, never an error.** The art does
not exist in the repository yet, and the rule is the one
``shared/splash.py`` already states for the banner: decoration must never become
a dependency. The alternative is a commcut that will not start because a PNG is
not in the payload. The same shape is why the icon files are listed as *optional*
payload files rather than required ones -- see `packaging/build.py`.

**The artwork's contract**, since it is a file a person prepares by hand:

- square, because ``QIcon.pixmap(size)`` scales to a square rect without keeping
  the aspect ratio -- a 300x200 source is distorted, not letterboxed;
- at least 256px, and 512 is worth having for the macOS Dock tile;
- a real alpha channel, or it renders as a hard rectangle on a title bar.

**Requires a `QApplication`.** `QIcon` builds a `QPixmap`, so `app_icon()`
cannot be called before the application exists. `main.py` calls it after, and
the tests call `editor_stub.ensure_qapp()` first.
"""

from __future__ import annotations

from PySide6.QtGui import QIcon

from shared.diagnostics import log
from shared.environment import resource_path


#: The cross-platform icon, as (folder, name) under the project root -- the form
#: `resource_path()` takes, and the layout the spec's `UI_DATAS` mirrors so one
#: expression resolves the same way frozen and unfrozen. Read by every platform.
ICON_PNG = ("assets", "commcut_icon.png")

#: The Windows icon, same form. Multi-frame, and read only on Windows: see the
#: module docstring for why the PNG is not redundant beside it.
ICON_ICO = ("assets", "commcut_icon.ico")


def app_icon() -> QIcon:
    """The application's icon, or a null `QIcon` if there is no art to load.

    Prefers `ICON_ICO` where Qt can decode it and falls back to `ICON_PNG`, so
    the one expression is correct on all three platforms. A `QIcon` built from a
    path that does not exist is null rather than an error, which is what makes
    the whole absent-art case work: this returns a null `QIcon` and logs the two
    paths it looked in, and every window falls back to whatever Qt uses by
    default.
    """
    ico = _read(ICON_ICO)
    if ico is not None:
        return ico

    png = _read(ICON_PNG)
    if png is not None:
        return png

    log("no app icon, so the windows use Qt's default: neither "
        f"{resource_path(*ICON_PNG)} nor {resource_path(*ICON_ICO)} could be "
        "read. See docs/packaging.md for what these two files are.")
    return QIcon()


def install_app_icon(app) -> QIcon:
    """Give `app` the icon, and return it.

    Separate from `app_icon()` so a test can drive the install against a real
    `QApplication` without entering `app.exec()`. Returns the icon rather than
    nothing because a caller may want to ask whether there was one --
    `QIcon.isNull()` is that answer.
    """
    icon = app_icon()
    app.setWindowIcon(icon)
    return icon


def _read(parts):
    """The `QIcon` at `parts`, or None if the file is missing or unreadable.

    None rather than a null `QIcon`, because "this platform cannot read this
    format" and "this file is not here" are both a fallback and only one of them
    is worth a log line -- and neither is worth an exception. Checked with
    `QIcon.isNull()` rather than `os.path.exists()` because a file that exists
    and cannot be decoded (a truncated copy, a JPEG renamed to `.png`) has to
    fall back too, or the window icon would be silently blank.
    """
    icon = QIcon(resource_path(*parts))
    return None if icon.isNull() else icon
