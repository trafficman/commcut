"""Build the macOS and Linux source releases: code, one install script, one
launcher, and nothing that belongs to another machine.

    python packaging/source_release.py --check-only
    python packaging/source_release.py
    python packaging/source_release.py --version v0.1.5-alpha

The output is ``dist/commcut-<version>-source-<os>-<arch>.tar.gz``, holding one
top-level ``commcut-<version>/`` folder -- the same single-folder shape as the
Windows zip, for the same reason: extracting it must not scatter the app across
whatever folder the user extracted into, and two releases side by side must not
merge each other's ``settings.json``.

Why a script rather than GitHub's automatic source archives
------------------------------------------------------------------------------

Every GitHub release already carries a ``Source code (tar.gz)``. It is a zip of
the checkout: it contains ``prototypes/``, ``experiments/``, ``build/``,
``dist/`` and the dead ``core.py`` and ``picker/``, it contains the 384 MB of
``bin/win/`` binaries as LFS *pointer files*, and it contains no install script
and no launcher. It is what ``git archive`` produces, which is a different thing
from what a person who wants to run commcut needs.

The manifest
------------------------------------------------------------------------------

``SOURCE_ENTRIES`` is an **allow-list**, and that is the whole safety argument
rather than a style preference. ``import/``, ``export/`` and ``temp/`` are not
in ``.gitignore`` -- they are the app's own folders at the repo root -- so a
developer who has exported a hundred clips has a hundred real files sitting
where an exclusion-list walker would look. An archive built by "everything,
except the things I remembered to exclude" would ship a user's videos into a
public release. An allow-list cannot: a path that is not named is not walked.

``FORBIDDEN_NAMES`` is the tripwire for a future edit that widens the list. With
an allow-list it is unreachable, which is exactly why it can afford to be blunt.

Every markdown file in the repository is in the manifest, which is one line of
code rather than a list to maintain. ``tests/test_docs.py`` requires that every
relative markdown link in the tree resolves to a ``.md`` file, so "all of them"
is provably enough: no link inside a source release can dangle. That is also why
``experiments/README.md`` is listed -- ``AGENTS.md`` and three documents link
into it -- while the experiment code beside it is not shipped.

``bin/`` is deliberately absent. macOS and Linux resolve ffmpeg and libmpv from
the system, so the Windows binaries are dead weight; a user who wants to
override the search can still ``mkdir -p bin/mac`` and drop a ``libmpv.2.dylib``
in, because ``resolve_mpv_library`` searches ``bin/<os>/`` before the system
prefixes.

Modes and determinism
------------------------------------------------------------------------------

Permissions are written into each ``TarInfo`` explicitly -- 0755 for the three
launchers, 0644 for everything else -- rather than read from disk. A Windows
checkout has no execute bit and ``core.fileMode`` is off there, so a mode taken
from the filesystem would be whatever the builder's umask decided, and
``tar``'s own default would strip the execute bit off ``run.sh`` and produce an
archive whose launcher cannot be run. That is the reason this is a tarball and
not a zip: a zip written by Python does not carry Unix permissions, and Finder's
Archive Utility does not restore them on extraction.

``mtime``, ``uid``, ``gid``, ``uname`` and ``gname`` are fixed, so two builds of
one tree produce **byte-identical** archives and the ``.sha256`` sidecar means
something. The Windows build cannot do this -- PyInstaller embeds a build
timestamp -- so it settles for a stable entry order instead.

Reused, not reimplemented
------------------------------------------------------------------------------

``normalize_version``, ``check_requested_version`` and ``write_sha256_sidecar``
come from ``packaging/build.py``, loaded by path. The tag-must-match
``shared/version.py`` rule has one owner, and duplicating it here would be a
second rule that could disagree.

Loading by path is not optional: ``packaging/`` has no ``__init__.py``, so
``import packaging.build`` binds the ``packaging`` that PyInstaller depends on
and then reports no ``build`` inside it. See ``tests/test_release_build.py`` for
the same trick applied to that file.
"""

import argparse
import gzip
import importlib.util
import io
import os
import platform
import sys
import tarfile


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGING_DIR = os.path.join(PROJECT_ROOT, 'packaging')
DIST_DIR = os.path.join(PROJECT_ROOT, 'dist')

# This script is run by path, so sys.path[0] is packaging/ and shared/ is not
# importable without help.
sys.path.insert(0, PROJECT_ROOT)

from shared.version import VERSION  # noqa: E402  (needs the sys.path above)


# ---------------------------------------------------------------------------
# What goes in
# ---------------------------------------------------------------------------

#: Every path the archive contains, relative to the project root with forward
#: slashes. Directories are walked; files are taken as they are. Order here does
#: not matter -- the archive is sorted at the end -- so this is grouped by why
#: the thing is here.
SOURCE_ENTRIES = (
    # What runs.
    'main.py',
    'mainwindow.py',
    'mainwindow.ui',
    'shared',
    'editor',
    'scanner',
    'settings',
    'importer',
    'assets',

    # What it needs installed.
    'requirements.txt',

    # The launcher surface. Copied in as files rather than written as string
    # literals here: a shell script kept inside a Python file cannot be
    # highlighted, diffed, or run by anything that reads the repository.
    'install_deps.sh',
    'run.sh',
    'commcut.command',

    # Every markdown file in the repository. See the module docstring: all of
    # them is provably enough, because every relative link in the tree targets a
    # .md file.
    'README.md',
    'AGENTS.md',
    'LICENSE',
    'docs',
    'packaging/README.md',
    # Linked into by AGENTS.md and by three documents; the code beside it is
    # not shipped.
    'experiments/README.md',
)

#: The three files that must be executable once extracted. Anything not named
#: here is 0644.
EXECUTABLE_ENTRIES = frozenset({'install_deps.sh', 'run.sh', 'commcut.command'})

#: Never in the archive, whatever the manifest says. A tripwire for a future
#: edit that widens ``SOURCE_ENTRIES``. Read as: no path may be, or start with,
#: one of these. With an allow-list it is unreachable, which is exactly why it
#: can afford to be blunt.
#:
#: ``experiments/`` is deliberately absent from this list, because
#: ``experiments/README.md`` is in the manifest and documents link into it. The
#: manifest names that one *file* rather than the folder, which is how the rest
#: of that tree is kept out: nothing reaches ``experiments/`` except the path
#: written down here.
FORBIDDEN_PATHS = (
    # Another platform's binaries, and the release tooling.
    'bin',
    '.github',
    '.kilo',
    # Test, historical and dead code.
    'tests',
    'prototypes',
    'picker',
    # Build output.
    'build',
    'dist',
    # The app's own runtime folders, which for a source install *are* the
    # project root. A developer who has exported a hundred clips has a hundred
    # real files here, and they are not gitignored.
    'import',
    'export',
    'temp',
    '.venv',
    # Runtime state, written on first run.
    'settings.json',
    'vocabulary.json',
    'commcut.log',
    # Dead modules, kept in the repository as history.
    'core.py',
    'test files',
)

#: Refused as a path segment at any depth. __pycache__ is a Python run's build
#: output rather than source, and a source release carrying another machine's
#: .pyc files would be a strange thing to publish.
FORBIDDEN_SEGMENTS = ('__pycache__', '.git')

#: Refused by extension, at any depth.
FORBIDDEN_SUFFIXES = ('.pyc', '.pyd', '.so', '.dylib', '.dll', '.exe',
                      '.mp4', '.mkv', '.cmct', '.cnfo')

#: The single top-level folder the archive holds, and the name of the archive
#: itself. Both follow the Windows build so the two releases match.
DEFAULT_ARCHIVE_ROOT = 'commcut-portable'


class SourceReleaseError(Exception):
    """A pre-flight check failed, or the staged tree is not what we promised."""


def _say(message):
    print(f"[source-release] {message}", flush=True)


# ---------------------------------------------------------------------------
# Reuse from the Windows build
# ---------------------------------------------------------------------------

def _load_sibling(filename, module_name):
    """A module from this folder, loaded by path rather than by name.

    `packaging/` is a namespace portion and the real `packaging` package is
    installed alongside it, so a regular package beats a namespace portion
    wherever it appears on sys.path. Importing by name is therefore not a style
    choice here; it does not work.
    """
    path = os.path.join(PACKAGING_DIR, filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: packaging/build.py, for the three rules it owns. Named for what it is rather
#: than called ``build``, which would be shadowed by this module's own build().
windows_build = _load_sibling('build.py', 'commcut_build')


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

#: What this machine calls itself in an artifact name. platform.system() says
#: "Darwin", which is not what anyone calls the thing.
OS_LABELS = {'darwin': 'macos', 'linux': 'linux'}

#: Windows names the architecture "AMD64" and the others call it "x86_64". The
#: published archives are built on macOS and Linux runners, so the spelling that
#: reaches a filename is already the conventional one -- this only keeps a build
#: made from a Windows checkout under --target from looking like a mistake.
ARCH_LABELS = {'amd64': 'x86_64'}


def artifact_arch():
    """This machine's architecture, spelled the way a filename should."""
    machine = platform.machine().strip().lower()
    return ARCH_LABELS.get(machine, machine)


def artifact_platform(target=None):
    """This machine's ``<os>-<arch>`` suffix, as it appears in a filename.

    macOS reports "Darwin", which is not what anyone calls the thing.

    ``target`` names the OS to build *for* instead of using this machine's. It
    exists because **a source archive's contents are platform-independent**:
    nothing is compiled, every file is the same file, and the only thing that
    varies is the name. So refusing to name the other platform's archive would be
    refusing to do something this script is perfectly capable of, and it would
    mean the manifest checks could not be run -- or an archive built and
    inspected -- from the Windows checkout, which is where development happens.

    What the refusal *is* for is a Windows build: that one genuinely cannot be
    produced here, because ``bin/win/`` is 384 MB of Git LFS binaries this
    machine may not even have, and PyInstaller is a Windows tool.
    """
    if target:
        key = target.strip().lower()
        if key not in OS_LABELS.values():
            raise SourceReleaseError(
                f"commcut ships source releases for macOS and Linux, so --target "
                f"is one of: {', '.join(OS_LABELS.values())}. Got {target!r}."
            )
    else:
        key = OS_LABELS.get(platform.system().lower())
        if key is None:
            raise SourceReleaseError(
                f"commcut ships source releases for macOS and Linux. This "
                f"machine reports '{platform.system().lower()}', which is "
                f"neither -- pass --target to name one explicitly. The Windows "
                f"build is a download from the releases page."
            )

    return f"{key}-{artifact_arch()}"


def archive_root(version):
    """The single top-level folder the archive holds."""
    return f'commcut-{version}' if version else DEFAULT_ARCHIVE_ROOT


def archive_name(version=None, target=None):
    """The archive's filename, with no directory part."""
    root = archive_root(version)
    return f'{root}-source-{artifact_platform(target)}.tar.gz'


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------

def _is_forbidden(relative):
    """Whether a root-relative posix path is refused, whatever the manifest says."""
    parts = relative.split('/')

    for name in FORBIDDEN_SEGMENTS:
        if name in parts:
            return name

    for name in FORBIDDEN_PATHS:
        if relative == name or relative.startswith(name + '/'):
            return name

    if relative.endswith(FORBIDDEN_SUFFIXES):
        return os.path.splitext(relative)[1]

    return None


def _walk(relative):
    """Every file under a manifest entry, as root-relative posix paths.

    Symlinks are skipped rather than followed. There are none in the tree, and a
    link pointing out of it is the other way this walk could pick up a file the
    allow-list did not name.
    """
    absolute = os.path.join(PROJECT_ROOT, *relative.split('/'))

    if os.path.islink(absolute):
        return []
    if os.path.isfile(absolute):
        return [relative]

    if not os.path.isdir(absolute):
        raise SourceReleaseError(
            f"{relative} is in the source-release manifest but is not in the "
            f"tree.\n\nAn archive missing it would install cleanly and then "
            f"fail to start, so this refuses rather than shipping one."
        )

    found = []
    for dirpath, dirnames, filenames in os.walk(absolute):
        dirnames[:] = sorted(
            name for name in dirnames
            if name != '__pycache__'
            and not os.path.islink(os.path.join(dirpath, name))
        )
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            found.append(os.path.relpath(full, PROJECT_ROOT)
                         .replace(os.sep, '/'))
    return found


def manifest_files():
    """Every file the archive contains: sorted, deduplicated, and checked.

    Walks ``SOURCE_ENTRIES`` rather than iterating a closed list, for the reason
    ``test_every_ui_file_in_the_tree_is_listed_and_bundled`` gives: a list that
    is only checked against itself cannot notice a file that was added and never
    shipped.
    """
    found = set()
    for entry in SOURCE_ENTRIES:
        found.update(_walk(entry))

    relative = sorted(found)
    offending = [f"  {path}  ({_is_forbidden(path)})" for path in relative
                 if _is_forbidden(path)]
    if offending:
        raise SourceReleaseError(
            f"{len(offending)} paths in the source-release manifest are on the "
            f"forbidden list:\n\n" + "\n".join(offending) +
            "\n\nSomething widened SOURCE_ENTRIES. An allow-list cannot ship "
            "one of these by accident, so this is the tripwire for one that was "
            "deliberately widened by mistake."
        )
    return relative


def check_manifest():
    """Refuse an archive that could not start, before anything is written."""
    _say("checking the source-release manifest")
    relative = manifest_files()

    if not relative:
        raise SourceReleaseError(
            "The manifest resolved to no files at all. That is an empty "
            "archive, which would extract and appear to work."
        )

    for name in EXECUTABLE_ENTRIES:
        if name not in relative:
            raise SourceReleaseError(
                f"{name} is not in the manifest. It is the only thing in the "
                f"release that starts commcut."
            )
    for name in ('main.py', 'requirements.txt'):
        if name not in relative:
            raise SourceReleaseError(f"{name} is not in the manifest.")

    for prefix in ('shared/environment.py', 'mainwindow.py', 'mainwindow.ui',
                   'assets/commcut_banner.png', 'shared/tagform.ui',
                   'editor/editorwindow.ui', 'scanner/scannerwindow.ui',
                   'settings/settingswindow.ui', 'importer/meshwindow.ui',
                   'importer/queuewindow.ui', 'importer/valueswindow.ui'):
        if prefix not in relative:
            raise SourceReleaseError(
                f"{prefix} is missing from the manifest. It is resolved at run "
                f"time by shared/environment.resource_path(), so an archive "
                f"without it starts and then fails to open a window."
            )

    _say(f"  {len(relative)} files, "
         f"{len(EXECUTABLE_ENTRIES)} of them executable")
    return relative


# ---------------------------------------------------------------------------
# The archive
# ---------------------------------------------------------------------------

#: 1980-01-01. Not 1970 because some tar readers treat a zero timestamp as
#: missing, and not "now" because the whole point is that two builds of one tree
#: are the same bytes.
FIXED_MTIME = 315532800

#: Files normalized to LF on the way into the archive. `.gitattributes` already
#: forces `eol=lf` on these, so a checkout cannot hold CRLF -- but a release
#: artifact is worth being independent of that, because a CRLF line ending in a
#: shell script is a syntax error (`\r: command not found`) that produces an
#: archive which extracts cleanly and then fails on the first line it runs.
LINE_ENDING_NORMALIZED = frozenset({'.sh', '.command'})


def _read_for_archive(relative):
    """A file's bytes as they go into the archive, normalizing line endings.

    Returns ``(None, None)`` for a file to be streamed straight from disk, and
    ``(bytes, size)`` for one that had to be rewritten. Binary files are not
    read into memory: the splash banner is 35 KB, so it would not matter, but
    the split keeps the intent obvious -- only the text scripts are touched.
    """
    source = os.path.join(PROJECT_ROOT, *relative.split('/'))

    if os.path.splitext(relative)[1] not in LINE_ENDING_NORMALIZED:
        return None, None

    with open(source, 'rb') as handle:
        data = handle.read().replace(b'\r\n', b'\n')
    return data, len(data)


def _tarinfo(relative, mode, size=0, is_dir=False):
    info = tarfile.TarInfo(relative)
    info.mode = mode
    info.mtime = FIXED_MTIME
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    if is_dir:
        info.type = tarfile.DIRTYPE
        info.size = 0
    else:
        info.type = tarfile.REGTYPE
        info.size = size
    return info


def write_archive(relative, destination, version=None):
    """Write the archive and return its path.

    Modes, timestamps, ownership and entry order are all fixed rather than
    inherited, so the output is a function of the tree alone. `gzip` is driven
    by hand with `mtime=0` for the same reason -- `tarfile.open("w:gz")` stamps
    the current time into the gzip header and the bytes would differ every run.
    """
    root = archive_root(version)
    os.makedirs(os.path.dirname(destination), exist_ok=True)

    directories = set()
    for path in relative:
        parts = path.split('/')[:-1]
        for index in range(1, len(parts) + 1):
            directories.add('/'.join(parts[:index]))

    members = sorted(directories | set(relative))

    with open(destination, 'wb') as raw:
        with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode='w', format=tarfile.GNU_FORMAT) as tar:
                for path in members:
                    inside = f"{root}/{path}"
                    if path in directories:
                        tar.addfile(_tarinfo(inside, 0o755, is_dir=True))
                        continue
                    source = os.path.join(PROJECT_ROOT, *path.split('/'))
                    mode = 0o755 if path in EXECUTABLE_ENTRIES else 0o644
                    payload, size = _read_for_archive(path)
                    info = _tarinfo(inside, mode, size=size or 0)

                    if payload is None:
                        info.size = os.path.getsize(source)
                        with open(source, 'rb') as handle:
                            tar.addfile(info, handle)
                    else:
                        tar.addfile(info, io.BytesIO(payload))

    return destination


def build(version=None, target=None):
    """Check, then write. Returns the archive's path."""
    relative = check_manifest()

    destination = os.path.join(DIST_DIR, archive_name(version, target))
    write_archive(relative, destination, version)

    digest = windows_build.write_sha256_sidecar(destination)
    size = os.path.getsize(destination)

    _say(f"  {size / (1024 * 1024):.1f} MB {destination}")
    _say(f"  sha256 {digest}")

    # The single line the CI workflow reads, so the filename exists in exactly
    # one place. The Windows workflow reconstructs its archive name in
    # PowerShell instead; this does not, and should not be made to.
    #
    # Deliberately unprefixed, unlike every other line this script prints: the
    # workflow greps for `^artifact: `, and a "[source-release] " in front of it
    # would make that match nothing.
    print(f"artifact: {destination}", flush=True)
    return destination


def main():
    parser = argparse.ArgumentParser(
        description="Build the macOS or Linux source release archive.")
    parser.add_argument(
        "--check-only", action="store_true",
        help="Run the manifest checks and stop."
    )
    parser.add_argument(
        "--version", metavar="TAG", default=None,
        help=f"Name the archive for a release, e.g. v{VERSION}. Refused unless "
             f"it matches shared/version.py, by the same rule and the same "
             f"function the Windows build uses."
    )
    parser.add_argument(
        "--target", metavar="OS", default=None,
        help="Name the archive for macos or linux instead of using this "
             "machine. A source archive's contents are identical on every "
             "platform -- nothing is compiled -- so this only changes the "
             "filename, and it is how the archive is built and inspected from "
             "a Windows checkout."
    )
    args = parser.parse_args()

    try:
        version = windows_build.check_requested_version(args.version)
        if args.check_only:
            check_manifest()
            _say("manifest checks passed")
            return 0
        build(version, args.target)
    except windows_build.BuildError as error:
        print(f"\nSOURCE RELEASE FAILED:\n{error}", file=sys.stderr)
        return 1
    except SourceReleaseError as error:
        print(f"\nSOURCE RELEASE FAILED:\n{error}", file=sys.stderr)
        return 1

    _say("done")
    _say("extract it, then run ./install_deps.sh and ./run.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
