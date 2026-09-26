# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for commcut.

Produces a single self-extracting commcut.exe that unpacks the Python and Qt
runtime to a temp folder and runs. The distributable is the *folder* around it,
not the exe alone -- see the layout in packaging/README.md:

    commcut.exe      this build: Python + PySide6 + the app + the .ui files
    bin/win/         ffmpeg, ffprobe, libmpv  (NOT inside the exe)
    import/          drop a compilation video in as test.mp4
    export/          named clips are written here

The one decision that matters in here
------------------------------------------------------------------------------

bin/win/ is deliberately NOT bundled. It is ~366 MB, and onefile re-extracts
its whole payload on *every* launch -- including every child window, because
each window is a separate process. Shipping the binaries inside the exe would
re-unpack a third of a gigabyte every time you opened a window.

Keeping bin/ beside the exe instead means shared/environment.py's
``install_root()`` -- which is ``dirname(sys.executable)`` when frozen -- finds
them immediately, with no extraction step at all. They are also visible and
replaceable, which is what you want from a folder you can put on a stick.

Everything else in the payload (Python, PySide6, the app, the .ui files) *is*
bundled, because that part is what makes the exe self-contained.

contents_directory
------------------------------------------------------------------------------

Only meaningful in onedir mode (``COMMCUT_ONEFILE=0``), where PyInstaller 6
would otherwise put the payload in dist/commcut/_internal/ while leaving the
exe at dist/commcut/. That would break the same bin/win lookup. "." keeps
everything flat. Onefile has no contents directory and ignores it.
"""

import os
import platform


# spec files are executed with the spec's folder as the anchor, but be
# explicit so a build from another directory still works.
PROJECT_ROOT = os.path.abspath(os.path.dirname(SPECPATH))

if platform.system().lower() != 'windows':
    raise SystemExit(
        "commcut only ships a Windows build today. bin/linux and bin/mac are "
        "placeholders with no binaries in them, so a build for another "
        "platform would produce an executable that cannot cut a clip."
    )

# Set COMMCUT_ONEFILE=0 for the onedir tree, which starts faster and is the
# one to debug against. The default is the shipping artifact.
ONEFILE = os.environ.get('COMMCUT_ONEFILE', '1') != '0'

# Read-only data bundled into the payload, as (path relative to the project
# root, destination inside the payload). The subfolders mirror the source tree
# so resource_path() resolves identically frozen and unfrozen.
#
# Forward slashes on both sides, deliberately: these are PyInstaller paths, not
# OS paths, and must not be run through os.path.join on Windows.
UI_DATAS = [
    ('mainwindow.ui', '.'),
    ('editor/editorwindow.ui', 'editor'),
    ('scanner/scannerwindow.ui', 'scanner'),
    ('settings/settingswindow.ui', 'settings'),
]

# PyInstaller resolves a relative datas source against the *spec's* folder, not
# the working directory, so the sources are made absolute here.
datas = [
    (os.path.join(PROJECT_ROOT, source), destination)
    for source, destination in UI_DATAS
]

# python-mpv is imported inside create_mpv_player rather than at module top.
# PyInstaller's modulegraph walks nested code and would find it anyway, but a
# deferred import of a compiled extension is exactly the kind of thing that
# breaks silently when an analysis filter is added later, and listing it costs
# nothing.
hiddenimports = [
    'mpv',
]

# Qt ships a tkinter binding and a large set of unused Qt modules. Excluding
# them cuts the payload meaningfully; none are imported by the app.
excludes = [
    'tkinter',
    'unittest',
    'pydoc_data',
]

a = Analysis(
    # Absolute: PyInstaller resolves a relative script path against the
    # *spec's* folder, not the working directory, so a bare 'main.py' here
    # would resolve to packaging/main.py.
    [os.path.join(PROJECT_ROOT, 'main.py')],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# Shared between both modes.
EXE_KWARGS = dict(
    name='commcut',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # The app is a GUI: no console window. Every ffmpeg/ffprobe call captures
    # its output via subprocess, so nothing is lost by not having a console --
    # and shared/diagnostics.py replaced the bare print() calls that used to be
    # the only diagnostic channel.
    console=False,
    # True, not False: the default makes the windowed bootloader pop its own
    # MODAL traceback dialog on an uncaught exception, which the process waits
    # on -- an error the user cannot dismiss looks exactly like a hang. We
    # handle reporting ourselves via shared/diagnostics.py (log file + a
    # QMessageBox, and a real exit code from main()).
    disable_windowed_traceback=True,
    # UPX corrupts some Qt DLLs and produces a great many antivirus false
    # positives. Not worth the size.
    upx=False,
)


if ONEFILE:
    # Binaries and data are packed into the exe rather than collected beside
    # it. There is deliberately no COLLECT step: that is the whole difference
    # between onefile and onedir, and adding one here would silently turn the
    # shipping artifact into a folder.
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        **EXE_KWARGS,
    )

else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        # See the contents_directory section of the docstring.
        contents_directory='.',
        **EXE_KWARGS,
    )

    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name='commcut',
    )
