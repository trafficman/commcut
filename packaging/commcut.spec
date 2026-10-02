# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for commcut.

Produces a single self-extracting commcut.exe that unpacks the Python and Qt
runtime to a temp folder and runs. The distributable is the *folder* around it,
not the exe alone -- see the layout in packaging/README.md:

    commcut.exe      this build: Python + PySide6 + the app + the .ui files
    bin/win/         ffmpeg, ffprobe, libmpv  (NOT inside the exe)
    import/          finished clips to import go here; a source video is picked
                      from anywhere with a file dialog
    export/          named clips are written here

The one decision that matters in here
------------------------------------------------------------------------------

bin/win/ is deliberately NOT bundled. It is ~366 MB, and onefile re-extracts
its whole payload on *every* launch. Shipping the binaries inside the exe would
re-unpack a third of a gigabyte every time you started commcut.

(This used to be paid once per *window*: the app ran each of its windows as a
separate process, so one editing session extracted the payload four times. There
is one process now, so it is paid once. The layout is unchanged and the argument
is if anything stronger -- see docs/packaging.md.)

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

hiddenimports
------------------------------------------------------------------------------

The four windows are named there, and reading the comment on the assignment is
the way to find out why. In short: they used to be Analysis() entry points, so
they were bundled by construction, and the lazy import that replaced them is the
one shape of import modulegraph cannot see. A build missing them starts and
shows a menu whose two buttons both fail.
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

# Two kinds of module have to be named here rather than reached by the analysis.
#
# mpv is imported inside create_mpv_player rather than at module top. That
# deferral is load-bearing in a second way: shared/session.py imports each
# window's builder lazily inside Shell.open, so the main menu reaching the
# screen never pulls in libmpv. PyInstaller's modulegraph walks nested code and
# would find this one anyway, but a deferred import of a compiled extension is
# exactly the kind of thing that breaks silently when an analysis filter is
# added later, and listing it costs nothing.
#
# The windows are the harder case, and the one that bites. session._BUILDERS
# maps a name to (module, builder) and Shell._resolve loads it with
# importlib.import_module(module_name) -- module_name is a variable. modulegraph
# cannot follow that: its _Visitor implements visit_Import and visit_ImportFrom
# and aliases every other expression node, visit_Call included, to a no-op. All
# four windows are therefore invisible to the graph, and this list is the only
# place they can be named.
#
# Before the app became one process they were Analysis() entry points, so they
# were bundled by construction and nothing imported them. Deleting the per-window
# entry points is what made that true no longer, and it is invisible from source
# and from the suite, which both import these modules directly, where
# importlib.import_module simply works. build.py's post-build checks are about
# the .ui files and bin/, and CI does not run the built exe. A build missing
# these starts normally, shows the main menu, and then cannot open either of
# its two buttons.
#
# tests/test_frozen_mode.py holds this list and session._BUILDERS to each other
# so a new window cannot be added to one and forgotten in the other.
hiddenimports = [
    'mpv',
    'editor.editor',
    'scanner.scanner',
    'settings.settings',
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
