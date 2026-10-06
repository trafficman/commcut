"""Shared environment setup for the project's entry points.

Every module that needs the project root or the binaries imports this. They may
live at the project root (main.py, mainwindow.py) or in a subdirectory (editor/,
scanner/, settings/). Either way they need the same two things before
anything else:

  1. The project root on sys.path so `shared.*` imports resolve.
  2. The binaries folder discoverable, so mpv, ffprobe, and ffmpeg resolve
     predictably rather than to whatever happens to be on the system -- to the
     bundled copy on Windows, to the user's own install on macOS and Linux.
     See "Where the binaries come from" below.

The bin folder is selected per-OS via ``get_binary_path`` / ``_bin_dir``
(the cross-platform binary resolution formerly living in ``core.py``).

All the windows are in one process
------------------------------------------------------------------------------

This module used to own ``launch_command()``, which built the argv that started
another window as its own process, and the frozen build's ``--window <name>``
dispatch that fed it. Both are gone: ``main.py`` is the only entry point, and
windows open each other through ``shared/session.py``. What is left here is the
environment they share.

Note what that changed for libmpv. It used to be true that the main menu could
never load mpv, because the menu and every player lived in different processes.
Now the menu is still the first thing to run and still never loads mpv on its
own -- but the moment a player window is opened, libmpv is loaded into the same
process. ``mpv_import_context()`` below still keeps the main menu's own startup
off that path, and the second-player optimization is now more valuable than it
was, since more windows can share one process.

Two roots, and the difference matters once the app is frozen
------------------------------------------------------------------------------

Running from source there is only one root and the distinction is academic.
Frozen there are two, and conflating them is the single most common way a
PyInstaller build of this app breaks:

  ``resource_root()``  read-only data bundled inside the executable's payload
                       (the four .ui files). Resolves to ``sys._MEIPASS``.
  ``install_root()``   writable user data that must survive a restart
                       (settings.json, import/, the default export/, temp/)
                       plus the bundled bin/<os>/ folder. Resolves to the folder
                       holding the executable, never the extraction folder --
                       the latter is wiped when the process exits.

``install_root()`` is also the one owner of the three writable folder *names*
below it, which is why they are spelled here rather than in the modules that
read them: ``import_folder()`` next to ``install_root()`` rather than in
``shared/sources.py``, and the export root in ``shared/exporting.py``, which
resolves a setting with this folder as its default.

The PyInstaller spec pins ``contents_directory="."`` so that, in the shipped
onedir layout, ``resource_root() == install_root()`` and the two collapse into
a single path. They are still separate functions because that is a property of
the build, not of the code: a onefile build would put ``resource_root()`` in a
temporary directory and ``install_root()`` beside the .exe.

Where the binaries come from
------------------------------------------------------------------------------

Windows ships its own ffmpeg, ffprobe and libmpv in ``bin/win/`` and this build
owns them. macOS and Linux are *source* installs: nothing is bundled, and the
binaries come from whatever the user installed (Homebrew, Linuxbrew, a distro
package). That is a policy difference, not an implementation one, so it is data:

``_BUNDLED_BINARY_PLATFORMS``
    which platforms ship binaries in ``bin/<os>/``. On these, a missing bundled
    binary is an error -- falling back to a system copy would silently run a
    build nobody tested.
``_SYSTEM_BIN_DIRS``
    the absolute ``bin`` directories searched on platforms that do *not* bundle.
``_SYSTEM_LIB_DIRS``
    the absolute library directories searched there. A sibling of the ``bin``
    directory, **not** a child: appending ``lib`` to ``/opt/homebrew/bin`` gives
    ``/opt/homebrew/bin/lib``, which exists nowhere, while Homebrew's libmpv is
    in ``/opt/homebrew/lib``.
``system_lib_dirs()``
    ``_SYSTEM_LIB_DIRS`` for this platform, plus the multiarch directory on
    Linux -- where ``apt install libmpv2`` puts libmpv, which no entry in
    that table contains. The Linux row also names ``/usr/lib64``, where the
    Red Hat family puts it.

The directories are an explicit list rather than ``shutil.which`` on purpose: a
bare name resolves through a mutated ``PATH`` and picks up whatever happens to
be installed, which is a very confusing bug to chase. On a bundling platform
that is exactly wrong. On a sourcing platform "whatever the user installed" is
the intent -- but naming the prefixes keeps the resolution reproducible, and
keeps the difference between "bundled" and "system" visible at every call site.

macOS needs no architecture handling anywhere. Homebrew installs to
``/opt/homebrew`` on Apple Silicon and ``/usr/local`` on Intel, and both are in
the list.

No console windows
------------------------------------------------------------------------------

The shipped build has no console of its own -- ``commcut.spec`` sets
``console=False``, so the executable is a GUI-subsystem binary -- and that
changes what Windows does with a child process. A console program started by a
process that has no console is given a *new, visible* console window. ffmpeg
and ffprobe are console programs, so every one of their calls would flash a
console over the app: the scanner's preview clip, the editor's duration and
keyframe probes, one per exported clip, and so on. It is the kind of surprise
that makes a GUI look like it is doing something to the machine.

``no_console_kwargs`` is the single owner of the fix. It returns the
``CREATE_NO_WINDOW`` flag on Windows and nothing elsewhere, for the call to
splat into ``subprocess.run``/``Popen``::

    subprocess.run(command, capture_output=True, text=True, **no_console_kwargs())

The flag gives the child a console with no window, so pipes and the temporary
stderr file keep working exactly as they do now. It is a no-op on a child that
is not a console application, which is why the window launches can carry it
without a second code path.

The alternative -- only adding the flag when the current process happens to
have no console -- was not taken. Every call site already captures its child's
output, so there is nothing to lose by never joining the parent console, and
"does this process have a console" is a question with a ctypes answer that has
to be kept correct rather than one the call sites can get right by accident.

Loading libmpv
------------------------------------------------------------------------------

libmpv is the one dependency that has to be *loaded* rather than merely
executed, and how that happens is not the same on every platform.

On Windows, ``ctypes.util.find_library`` -- which python-mpv calls at *import*
time -- scans ``%PATH%`` and returns the absolute path of the first hit.
Prepending ``bin/<os>`` to ``%PATH%`` is therefore load-bearing, and it has to
happen before ``import mpv`` runs. ``os.add_dll_directory`` is registered too;
the returned handle is stored in a module global, because
``add_dll_directory`` unregisters the directory as soon as its handle is garbage
collected, and a dropped handle shows up as a bare ``OSError`` much later.

On macOS ``find_library`` ignores ``PATH`` entirely: it searches a fixed list of
system directories and returns a path only if that exact file exists. None of
that is where a source install's libmpv is, so the ``PATH`` prepend buys nothing
there and the failure surfaces as a bare ctypes ``OSError`` from inside
python-mpv, naming none of the folders this module actually searched.

Mapping the library first is **not** sufficient on its own, which is the
non-obvious part. python-mpv 1.0.8's POSIX branch asks
``ctypes.util.find_library('mpv')`` and raises outright if that returns ``None``,
without ever checking whether libmpv is already mapped. ``DYLD_LIBRARY_PATH``
does not help either, because ``find_library`` looks for a *file* at a fixed
list of paths rather than asking the dynamic loader. So ``mpv_import_context``
also *answers* the lookup, from the path already resolved, for exactly the names
python-mpv asks for; the override is removed again on the way out.

``DYLD_LIBRARY_PATH`` is still set, for libmpv's transitive dylib
dependencies. That is a secondary measure, and it works because a PyInstaller
app is not SIP-protected (SIP strips ``DYLD_*`` only for Apple-signed system
binaries). It is skipped on Linux, where ``LD_LIBRARY_PATH`` semantics for
runtime ``dlopen`` are murkier.

``mpv_import_context`` is deliberately *not* entered from
``setup_environment``: the main menu never loads mpv, and doing it there would
put libmpv in every process including the one that has no player. Wrap the
``import mpv`` instead.
"""

import contextlib
import ctypes
import ctypes.util
import fnmatch
import os
import platform
import subprocess
import sys
import sysconfig


# Presence of this file inside a `shared/` folder is what identifies a folder
# as the project root, so a script that already lives there (main.py) is not
# treated as if it were one level below the root.
_SHARED_MARKER = os.path.join('shared', 'environment.py')

# Per-OS bundled-binary subfolder, under install_root(). Mirrors the layout of
# the source tree, so bin/ is the same folder unfrozen and frozen.
_OS_BIN_FOLDERS = {
    'windows': 'win',
    'linux': 'linux',
    'darwin': 'mac',  # macOS
}

# Platforms that ship their own ffmpeg/ffprobe in bin/<os>/, as opposed to
# using the system's. This is the policy switch behind the whole resolution
# order: on a bundling platform a missing binary is an error, because silently
# falling back to whatever the machine has installed is how a build ends up
# running an ffmpeg nobody tested. The macOS and Linux folders are placeholders
# that ship empty; those platforms are source installs (see docs/source-install.md)
# and get their binaries from the prefixes below.
_BUNDLED_BINARY_PLATFORMS = frozenset({'windows'})

# Absolute *directories* searched on platforms that do not bundle, in order, for
# an executable like ffmpeg or ffprobe. Deliberately explicit rather than
# shutil.which: see the module docstring.
#
# These are directories that already end in `bin`. They are NOT prefixes, and
# nothing may derive a library directory from them by appending `lib` -- that
# produces `/opt/homebrew/bin/lib`, which has never existed on any machine, while
# the library Homebrew actually installs sits in the *sibling* directory
# `/opt/homebrew/lib`. `_SYSTEM_LIB_DIRS` below says so out loud.
#
# macOS needs no architecture handling: Homebrew uses /opt/homebrew on Apple
# Silicon and /usr/local on Intel, and both are listed.
_SYSTEM_BIN_DIRS = {
    'darwin': ('/opt/homebrew/bin', '/usr/local/bin', '/usr/bin'),
    'linux': ('/home/linuxbrew/.linuxbrew/bin', '/usr/local/bin', '/usr/bin'),
}

# Absolute *directories* searched for a shared library, in order. Written out
# rather than computed from `_SYSTEM_BIN_DIRS`, because a package manager's bin
# and lib directories are siblings under a prefix, not parent and child:
#
#     /opt/homebrew/bin/ffmpeg      <- _SYSTEM_BIN_DIRS
#     /opt/homebrew/lib/libmpv.dylib   <- _SYSTEM_LIB_DIRS
#
# Deriving the second from the first is a one-character mistake that produces a
# directory no machine has, and the search then reports success-shaped output
# while having looked nowhere libmpv has ever been installed.
_SYSTEM_LIB_DIRS = {
    'darwin': ('/opt/homebrew/lib', '/usr/local/lib', '/usr/lib'),
    'linux': (
        '/home/linuxbrew/.linuxbrew/lib', '/usr/local/lib', '/usr/lib',
        '/usr/lib64', '/lib64',
    ),
}

# Where a Linux distro package keeps its shared libraries, which is under a
# multiarch triplet rather than directly in /usr/lib: `apt install libmpv2`
# installs /usr/lib/x86_64-linux-gnu/libmpv.so.2, and no entry in
# _SYSTEM_LIB_DIRS contains that directory. The triplet itself is read from
# sysconfig in system_lib_dirs() so an aarch64 host names its own.
_MULTIARCH_LIB_ROOT = '/usr/lib'

# The libmpv client library, by platform. This is not the mpv player binary:
# python-mpv needs the library, which is a separate thing to install and the
# usual reason a source install on macOS cannot start.
_MPV_LIBRARY_NAMES = {
    'windows': ('libmpv-2.dll',),
    'darwin': ('libmpv.2.dylib', 'libmpv.dylib'),
    'linux': ('libmpv.so.2',),
}

# The same, as a pattern, for the fallback pass below.
_MPV_LIBRARY_PATTERNS = {
    'windows': 'libmpv-*.dll',
    'darwin': 'libmpv*.dylib',
    'linux': 'libmpv*.so*',
}

#: Overrides the libmpv search outright. Set it to the absolute path of a
#: libmpv binary or framework when the automatic search cannot find one, which
#: is the documented escape hatch for a source install on an unusual machine.
#: A value that does not exist is an error rather than a reason to keep
#: searching -- an override that silently does nothing is worse than none.
MPV_LIBRARY_ENV_VAR = 'COMMCUT_MPV_LIB'

#: The names python-mpv hands to ``ctypes.util.find_library`` when it is
#: imported, which is what ``mpv_import_context`` has to answer. Taken from
#: python-mpv 1.0.8's own import block: the Windows branch loops over three DLL
#: names in that order, and the POSIX branch asks for the bare string 'mpv' and
#: relies on ``find_library`` to supply the platform filename. The darwin/linux
#: rows are the spellings ``find_library`` itself would try, so a name it might
#: have produced is intercepted too.
_MPV_LOOKUP_NAMES = {
    'windows': ('mpv', 'mpv-2.dll', 'libmpv-2.dll', 'mpv-1.dll'),
    'darwin': ('mpv', 'libmpv.2.dylib', 'mpv.2.dylib',
               'libmpv.dylib', 'mpv.dylib'),
    'linux': ('mpv', 'libmpv.so.2', 'mpv.so.2', 'libmpv.so', 'mpv.so'),
}

# mpv's video output driver. 'direct3d' is the Windows embedded-rendering path;
# the other platforms use mpv's generic GPU output. Kept here rather than
# hardcoded at the call site so the next port does not have to rediscover it.
# The macOS and Linux entries are unverified: mpv is embedded through an
# NSView* on macOS and an X11/Wayland window on Linux, and neither path has
# been exercised on this machine. See docs/source-install.md.
MPV_VIDEO_OUTPUT = {
    'windows': 'direct3d',
    'linux': 'gpu',
    'darwin': 'gpu',
}

#: The mpv video encoder this app hardcodes, checked at first use so a source
#: install on a machine whose ffmpeg lacks it reports that rather than failing
#: every export with a raw encoder error.
REQUIRED_VIDEO_ENCODER = 'libx264'

# Handles returned by os.add_dll_directory(). Module-level so they outlive the
# call that created them -- see the module docstring.
_dll_directory_handles = []

# The loaded libmpv, so a second player in one process reuses it.
_mpv_library_handle = None

def is_frozen():
    """True when running from a PyInstaller-built executable."""
    return bool(getattr(sys, 'frozen', False))


def _os_key():
    return platform.system().lower()


def _source_root():
    """The project root as it exists in the source tree.

    Walks up from this file to the folder containing shared/environment.py, so
    it is correct no matter which entry point imported us. Not meaningful when
    frozen, where __file__ points into the extraction directory.
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_root():
    """Read-only root for data bundled inside the executable payload.

    This is where the four .ui files live once frozen. Resolve read-only
    resources through resource_path(), never through install_root(): the two
    point at the same folder in the shipped onedir build but not in general.
    """
    if is_frozen():
        return os.path.abspath(getattr(sys, '_MEIPASS', _source_root()))
    return _source_root()


def install_root():
    """Writable root for user data and for the bundled bin/<os>/ folder.

    Holds settings.json, import/, the default export/, and temp/. When frozen
    this is the folder containing the executable -- explicitly *not* the
    extraction directory, which is deleted when the process exits, and not
    necessarily writable (a build dropped in Program Files would fail every
    write).
    """
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return _source_root()


#: The user-editable JSON file every setting lives in. Beside the executable,
#: so two installed copies do not share it -- see docs/packaging.md.
SETTINGS_FILENAME = "settings.json"

#: Folder under the install root that the Library Importer reads from, and moves
#: and deletes within. Fixed rather than configurable on purpose: the importer
#: removes the clips it has taken, so the folder it is allowed to destroy has to
#: be inside the program root rather than somewhere a single mis-click reaches.
IMPORT_FOLDER_NAME = "import"


def settings_path() -> str:
    """Absolute path of ``settings.json``.

    The one owner of that path. It was spelled out in four places before this
    existed, and the fifth -- the configurable export root, which has to read
    the same file -- would have been the one a change was missed in. Four
    spellings of one path is four places to update and no test to say so.

    Resolved through ``install_root()``, so it is beside the executable in a
    packaged build and in the source tree when running from source.
    """
    return os.path.join(install_root(), SETTINGS_FILENAME)


def import_folder() -> str:
    """Absolute path of the folder the Library Importer reads from.

    Not a source-video folder any more -- a compilation is picked with a file
    dialog from anywhere, which is what freed this one for finished clips. Fixed
    under the install root and not configurable: the importer moves and deletes
    here, which is the reason. See :data:`IMPORT_FOLDER_NAME`.

    Here rather than in ``shared/sources.py`` (which used to carry a dead second
    copy) because ``shared/exporting.py`` must compare the export root against
    it and ``shared/importing.py`` imports *from* ``shared/exporting.py``, so
    either of those would be a cycle.
    """
    return os.path.join(install_root(), IMPORT_FOLDER_NAME)



def resource_path(*parts):
    """Absolute path to a bundled read-only resource.

    Pass the resource's path *relative to the project root*, subfolders
    included: resource_path("settings", "settingswindow.ui"). The PyInstaller
    spec mirrors the source tree's subfolder layout into the payload so that
    one expression is correct both unfrozen and frozen -- flattening the four
    .ui files into the payload root would make resource_path() right only in a
    packaged build and wrong when running from source.
    """
    return os.path.join(resource_root(), *parts)


def _bin_dir():
    """Return the absolute path to the bundled binaries folder for this OS.

    bin/win on Windows, bin/linux on Linux, bin/mac on macOS. When frozen this
    is resolved against install_root() rather than against __file__, so the
    binaries sit beside the executable in the shipped onedir layout instead of
    inside a temporary extraction directory.
    """
    folder = _OS_BIN_FOLDERS.get(_os_key())
    if folder is None:
        raise EnvironmentError(
            f"Unsupported operating system: {platform.system()}")

    return os.path.join(install_root(), 'bin', folder)


def bin_dir():
    """Public accessor for the bundled binaries folder, whether or not it exists."""
    return _bin_dir()


def _binary_filename(binary_name):
    """This platform's filename for a bundled binary: ffmpeg, ffmpeg.exe, ..."""
    return f"{binary_name}{'.exe' if _os_key() == 'windows' else ''}"


def _system_bin_candidates(binary_name):
    """Absolute paths searched for a binary on a platform that does not bundle."""
    filename = _binary_filename(binary_name)
    for directory in _SYSTEM_BIN_DIRS.get(_os_key(), ()):
        yield os.path.join(directory, filename)


def system_lib_dirs():
    """Absolute library directories searched on a platform that does not bundle.

    ``_SYSTEM_LIB_DIRS``, written out rather than derived from
    ``_SYSTEM_BIN_DIRS``: a package manager's bin and lib directories are
    siblings under a prefix, not parent and child, so appending ``lib`` to
    ``/opt/homebrew/bin`` yields ``/opt/homebrew/bin/lib`` -- a directory no
    machine has -- while Homebrew's libmpv is in the sibling ``/opt/homebrew/lib``.
    That mistake is why this is a table of its own.

    **Plus, on Linux, the multiarch directory.** A distro package puts libmpv in
    ``/usr/lib/<multiarch>`` -- ``x86_64-linux-gnu``, ``aarch64-linux-gnu``,
    and so on -- and that is *not* ``/usr/lib``, so the table alone misses
    exactly the case the error message tells the user to create: ``apt install
    libmpv2`` installs ``/usr/lib/x86_64-linux-gnu/libmpv.so.2``. The triplet is
    read from ``sysconfig`` rather than written out, so an aarch64 host resolves
    its own name instead of a hardcoded one.

    **And on Linux, the Red Hat family's ``lib64``.** Fedora and RHEL put
    their libraries in ``/usr/lib64`` with no multiarch triplet at all, so
    ``dnf install mpv`` installs ``/usr/lib64/libmpv.so.2``, which none of
    the prefix directories contain. ``/lib64`` is the same directory on a
    usr-merged system and the real one on an older Red Hat, so both are
    listed.

    Nothing is filtered on existence here. The caller reports every directory it
    looked in, and a list that silently dropped the ones not present would be a
    worse error message than a longer one.
    """
    directories = list(_SYSTEM_LIB_DIRS.get(_os_key(), ()))

    multiarch = sysconfig.get_config_var('MULTIARCH')
    if _os_key() == 'linux' and multiarch:
        directories.append(os.path.join(_MULTIARCH_LIB_ROOT, multiarch))

    return tuple(directories)


def get_binary_path(binary_name):
    """Absolute path to this machine's ffmpeg, ffprobe, or any bundled binary.

    ``bin/<os>/`` is searched first, always. On a platform that bundles its
    binaries that is the only place searched, and a missing one is an error:
    falling through to a system copy would silently run a build nobody tested,
    which is a much worse failure than a missing file. On a platform that does
    not bundle -- the macOS and Linux source installs -- the absolute prefixes
    in ``_SYSTEM_BIN_DIRS`` are searched next, and a candidate has to be an
    executable file to count.

    Raises ``FileNotFoundError`` naming every location it looked in, because a
    packaged build that cannot find ffmpeg otherwise reports nothing useful.

    Every ffmpeg/ffprobe call site should use this rather than a bare binary
    name. A bare name resolves through the mutated ``PATH`` and picks up
    whatever happens to be installed, which is a very confusing bug to chase.
    """
    bundled = os.path.join(_bin_dir(), _binary_filename(binary_name))
    if os.path.exists(bundled):
        return bundled

    if _os_key() in _BUNDLED_BINARY_PLATFORMS:
        raise FileNotFoundError(
            f"Could not find {_binary_filename(binary_name)} in the bundled "
            f"binaries folder:\n\n{bundled}\n\n"
            f"This build bundles its own ffmpeg and will not fall back to a "
            f"system copy, so restore bin/"
            f"{_OS_BIN_FOLDERS[_os_key()]}/ beside the executable and try "
            f"again."
        )

    candidates = list(_system_bin_candidates(binary_name))
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    searched = "\n".join(f"  {path}" for path in [bundled, *candidates])
    raise FileNotFoundError(
        f"Could not find {_binary_filename(binary_name)}. Looked in:\n\n"
        f"{searched}\n\n"
        f"commcut uses the system's ffmpeg on this platform. Install it and "
        f"make sure it is on PATH:\n\n"
        f"  brew install ffmpeg        # macOS, or Linux with Linuxbrew\n"
        f"  apt install ffmpeg         # Debian, Ubuntu\n"
        f"  dnf install ffmpeg         # Fedora, RHEL"
    )


def resolve_mpv_library():
    """Absolute path to the libmpv client library on this machine.

    Searched in order: the ``MPV_LIBRARY_ENV_VAR`` override, then ``bin/<os>/``,
    then ``system_lib_dirs()`` -- which is each system prefix's ``lib/``
    subdirectory plus, on Linux, the multiarch directory a distro package
    actually uses. Within those, the platform's known libmpv filenames first and
    a pattern match second, so a soname nobody promised to keep still resolves.
    Returns the first hit and raises ``FileNotFoundError`` -- naming every
    location -- when there is none.

    The override must be an absolute path that exists. One that does not is an
    error rather than a reason to keep searching, because an override that
    silently does nothing is worse than no override at all. Point it at a
    framework binary (``…/mpv.framework/mpv``) if that is what your build
    ships; ``ctypes.CDLL`` loads those too.

    This resolves only, and loads nothing, so the search order is testable on
    any platform. ``mpv_import_context`` is what actually loads it and hands the
    result to python-mpv.
    """
    override = os.environ.get(MPV_LIBRARY_ENV_VAR)
    if override:
        if not os.path.isabs(override):
            raise FileNotFoundError(
                f"{MPV_LIBRARY_ENV_VAR} must be an absolute path, not "
                f"{override!r}."
            )
        if not os.path.isfile(override):
            raise FileNotFoundError(
                f"{MPV_LIBRARY_ENV_VAR} points at {override}, which does not "
                f"exist."
            )
        return os.path.abspath(override)

    names = _MPV_LIBRARY_NAMES.get(_os_key())
    if not names:
        raise EnvironmentError(
            f"Unsupported operating system: {platform.system()}")

    directories = [_bin_dir(), *system_lib_dirs()]

    searched = []
    for directory in directories:
        for name in names:
            candidate = os.path.join(directory, name)
            searched.append(candidate)
            if os.path.isfile(candidate):
                return candidate

    # Second pass, on a pattern rather than a name.
    #
    # The list above is a guess about a soname nobody promised to keep. libmpv's
    # has already changed once -- mpv 0.35 shipped `libmpv.1`, 0.37 shipped
    # `libmpv.2` -- and every distribution is free to pick its own, so a hardcoded
    # list turns a routine upstream bump into "commcut cannot find libmpv" on
    # every machine that has not been patched yet. That is exactly the shape of
    # failure this search is supposed to absorb.
    #
    # Only reached when none of the named files exist, so it cannot change which
    # library is picked where the list is right, and an unfamiliar name still
    # loads: `ctypes.CDLL` does not care about a soname. Every candidate it
    # considered is added to the failure listing below, so a wrong guess is
    # visible rather than mysterious.
    pattern = _MPV_LIBRARY_PATTERNS.get(_os_key())
    if pattern:
        for directory in directories:
            try:
                entries = sorted(os.listdir(directory))
            except OSError:
                # A directory we cannot list is one we cannot search, and it is
                # already named in the failure listing below.
                continue
            for entry in entries:
                if not fnmatch.fnmatch(entry, pattern):
                    continue
                candidate = os.path.join(directory, entry)
                searched.append(candidate)
                if os.path.isfile(candidate):
                    return candidate

    listing = "\n".join(f"  {path}" for path in searched)
    raise FileNotFoundError(
        f"Could not find the libmpv library. Looked in:\n\n{listing}\n\n"
        f"commcut needs the libmpv client library, which is a different thing "
        f"from the mpv player:\n\n"
        f"  brew install mpv                # macOS, or Linux with Linuxbrew\n"
        f"  apt install libmpv2             # Debian, Ubuntu\n"
        f"  dnf install mpv                 # Fedora, RHEL\n\n"
        f"If your mpv ships one somewhere else, point "
        f"{MPV_LIBRARY_ENV_VAR} at it:\n\n"
        f"  {MPV_LIBRARY_ENV_VAR}=/full/path/to/libmpv.2.dylib"
    )


def load_mpv_library(path=None):
    """Map the resolved libmpv, once per process, and return the handle.

    ``RTLD_GLOBAL`` so it is visible to everything already loaded. Memoized: a
    second player in one process reuses the mapped image rather than loading it
    again. Pass ``path`` to avoid resolving twice when the caller already has
    the answer.
    """
    global _mpv_library_handle
    if _mpv_library_handle is not None:
        return _mpv_library_handle

    if path is None:
        path = resolve_mpv_library()

    if _os_key() == 'darwin':
        # Secondary measure, for libmpv's own transitive dylibs. Effective
        # because a PyInstaller app is not SIP-protected. Skipped on Linux,
        # where LD_LIBRARY_PATH semantics for runtime dlopen are murkier.
        library_dir = os.path.dirname(path)
        current = os.environ.get('DYLD_LIBRARY_PATH', '')
        entries = current.split(os.pathsep) if current else []
        if library_dir not in entries:
            os.environ['DYLD_LIBRARY_PATH'] = (
                library_dir + os.pathsep + current if current else library_dir)

    _mpv_library_handle = ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
    return _mpv_library_handle


@contextlib.contextmanager
def mpv_import_context():
    """Load libmpv and make python-mpv's own lookup agree with ours.

    Wrap the ``import mpv`` in this, and nothing else. Mapping the library
    first is **not** sufficient on its own, which is the non-obvious part:
    python-mpv 1.0.8's POSIX branch asks ``ctypes.util.find_library('mpv')`` and
    raises outright if that returns ``None``, without ever checking whether
    libmpv is already mapped. And ``DYLD_LIBRARY_PATH`` does not help either,
    because ``find_library`` looks for a *file* at a fixed list of paths rather
    than asking the dynamic loader. A Homebrew libmpv in ``/opt/homebrew/lib``
    is therefore invisible to it, and the failure is a message that tells the
    user to read the ``ctypes.util.find_library`` documentation.

    So the lookup is answered, from the path already resolved, for exactly the
    names python-mpv asks for and nothing else. Every other name still goes to
    the real function, and the override is removed on the way out. On Windows
    this is redundant with the ``PATH`` prepend underneath, and it is applied
    anyway so the library that gets bound is the one this module validated.
    """
    path = resolve_mpv_library()
    load_mpv_library(path)

    wanted = _MPV_LOOKUP_NAMES.get(_os_key(), ('mpv',))
    original = ctypes.util.find_library

    def find_library(name, *args, **kwargs):
        if name in wanted:
            return path
        return original(name, *args, **kwargs)

    ctypes.util.find_library = find_library
    try:
        yield path
    finally:
        ctypes.util.find_library = original


def video_output():
    """The mpv video output driver to use on this platform."""
    key = _os_key()
    if key not in MPV_VIDEO_OUTPUT:
        raise EnvironmentError(
            f"Unsupported operating system: {platform.system()}")
    return MPV_VIDEO_OUTPUT[key]


def no_console_kwargs():
    """Extra ``subprocess`` kwargs that keep a child off the screen on Windows.

    Splat into every ``subprocess.run`` / ``Popen`` that starts a console
    program -- ffmpeg, ffprobe, and a child window for good measure. See "No
    console windows" in the module docstring for why a windowed build needs
    this and why the flag is unconditional rather than only when the parent
    happens to have no console.

    Returns a fresh dict every call, so a caller that merges it into other
    keyword arguments cannot corrupt anyone else's.
    """
    if _os_key() == 'windows':
        return {'creationflags': subprocess.CREATE_NO_WINDOW}
    return {}


#: Writable folders the app expects to exist next to the executable.
#:
#: export/ and temp/ are created lazily by shared.ffmpeg when something is
#: actually written, so they only need to exist for discoverability -- and
#: export/ is here because it is the *default* export root, not because every
#: install uses it. import/ is never created by anything else, and an installed
#: build has no source tree to inherit it from -- the user drops a source video
#: in by hand.
APP_FOLDERS = ('import', 'export', 'temp')


def ensure_app_folders():
    """Create the app's writable folders under install_root().

    Returns the list of folders that now exist. Raises OSError if the install
    root is not writable, which is a legitimate thing to hit (a build dropped
    into Program Files) and is worth reporting rather than swallowing: every
    later save, scan, and export would fail the same way with a much less
    obvious message.
    """
    root = install_root()
    for name in APP_FOLDERS:
        os.makedirs(os.path.join(root, name), exist_ok=True)
    return [os.path.join(root, name) for name in APP_FOLDERS]


def _register_bin_dir():
    """Make the bundled binaries discoverable. Returns the folder, or None.

    Two mechanisms, both needed for different reasons. The %PATH% prepend is
    what ctypes.util.find_library -- and therefore python-mpv's import-time DLL
    lookup -- actually searches, so it is load-bearing. os.add_dll_directory
    registers the folder the modern way and keeps working regardless of the
    loader flags python-mpv passes to CDLL.
    """
    bin_dir = _bin_dir()
    if not os.path.isdir(bin_dir):
        return None

    if _os_key() == 'windows' and hasattr(os, 'add_dll_directory'):
        try:
            handle = os.add_dll_directory(bin_dir)
        except OSError:
            handle = None
        if handle is not None:
            # Held at module scope on purpose. Losing the handle unregisters
            # the directory, and the failure surfaces much later as a bare
            # OSError from ctypes when the player is constructed.
            _dll_directory_handles.append(handle)

    # ctypes.util.find_library subscripts os.environ['PATH'] directly, so PATH
    # has to exist, not merely be preferred.
    current = os.environ.get('PATH', '')
    entries = current.split(os.pathsep) if current else []
    if bin_dir not in entries:
        os.environ['PATH'] = (
            bin_dir + os.pathsep + current if current else bin_dir)

    return bin_dir


def setup_environment(script_path):
    """Prepare sys.path and the binaries folder for an entry-point script.

    Call at the very top of the script (before any imports of `shared` or
    mpv):

        from shared.environment import setup_environment
        SCRIPT_DIR, PROJECT_ROOT = setup_environment(__file__)

    Returns (script_dir, project_root) so the caller can build paths relative
    to the script or to the install root. `project_root` is install_root():
    the source tree when unfrozen, the executable's folder when frozen.

    `script_dir` is the script's own folder, which is only meaningful when
    running from source -- when frozen it points into the payload directory.
    Use resource_path() instead of it for anything that needs to exist in a
    packaged build; that distinction is why this function is no longer the way
    .ui files are located.
    """
    script_dir = os.path.dirname(os.path.abspath(script_path))
    project_root = install_root()

    # 1. Make the project root importable so 'shared' resolves. A frozen build
    #    already has an importer covering every bundled module, and its
    #    install root holds no .py files, so this is a no-op there.
    if not is_frozen() and project_root not in sys.path:
        sys.path.insert(0, project_root)

    # 2. Make the bundled binaries discoverable by mpv, ffprobe, and ffmpeg.
    _register_bin_dir()

    return script_dir, project_root
