"""Build commcut into a portable folder you can zip and hand to someone.

    python packaging/build.py                # onefile commcut.exe (shipping)
    python packaging/build.py --onedir       # folder build, starts faster
    python packaging/build.py --check-only   # pre-flight checks only
    python packaging/build.py --zip          # ...and zip it for distribution
    python packaging/build.py --zip --version v0.1.0   # a release

The output is dist/commcut-portable/:

    commcut.exe      self-extracting: unpacks Python + PySide6 + the app to a
                     temp folder and runs. No Python needed on the target.
    bin/win/         ffmpeg.exe, ffprobe.exe, libmpv-2.dll -- 366 MB, kept
                     beside the exe rather than inside it
    import/          finished clips to import go here; a source video is picked
                     from anywhere with a file dialog
    export/          named clips are written here
    commcut.log      written on first run
    settings.json    written when you save in Settings

Why the binaries are NOT inside the exe: onefile re-extracts its entire
payload on every launch. That used to be paid once per *window* -- each window
ran as its own process, so one editing session extracted the payload four
times -- and is now paid once, which is a better argument for this layout rather
than a different one. With the binaries bundled it would still be ~366 MB of
writes before a window appeared, every time. Kept outside, the exe carries only
Python and Qt -- around 46 MB -- and bin/win/ is found immediately, with no
extraction step.

Why onefile rather than a folder with a SFX installer: this is a portable
smoke-test build, so "copy the folder and run commcut.exe" is the whole
install story. There is no installer to run, no elevation prompt, and nothing
written outside the folder.

Note that empty folders do not survive being zipped, which is why import/ and
export/ ship with a placeholder file. See PLACEHOLDER_* below.

Releases
------------------------------------------------------------------------------

--zip writes dist/<name>.zip next to the folder, holding a single top-level
folder rather than a flat tree, so that extracting it does not scatter the app
across a folder the user has other files in and so that two releases extracted
side by side do not merge each other's settings.json and import/. A .sha256
sidecar is written with it.

--version names that folder and the zip, and is checked against the version in
shared/version.py: a tag that disagrees is refused. Without it the archive is
called commcut-portable-windows-x64.zip, which is what you want for a local
smoke test rather than a release. .github/workflows/release.yml is the only
caller that passes one.
"""

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import zipfile


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGING_DIR = os.path.join(PROJECT_ROOT, 'packaging')
SPEC_PATH = os.path.join(PACKAGING_DIR, 'commcut.spec')
DIST_DIR = os.path.join(PROJECT_ROOT, 'dist')

# This script is run by path, so sys.path[0] is packaging/ and shared/ is not
# importable without help. Note that `import packaging` itself would resolve to
# the PyInstaller dependency rather than to this folder: a directory with no
# __init__.py is only a namespace portion, and a real package elsewhere on the
# path always wins that.
sys.path.insert(0, PROJECT_ROOT)

from shared.version import VERSION  # noqa: E402  (needs the sys.path above)

ONEDIR_APP_DIR = os.path.join(DIST_DIR, 'commcut')
ONEFILE_EXE = os.path.join(DIST_DIR, 'commcut.exe')
PORTABLE_DIR = os.path.join(DIST_DIR, 'commcut-portable')

BIN_SOURCE_DIR = os.path.join(PROJECT_ROOT, 'bin', 'win')
BINARIES = ('ffmpeg.exe', 'ffprobe.exe', 'libmpv-2.dll')

UI_FILES = (
    'mainwindow.ui',
    'editor/editorwindow.ui',
    'scanner/scannerwindow.ui',
    'settings/settingswindow.ui',
    'importer/meshwindow.ui',
    'shared/tagform.ui',
    'importer/queuewindow.ui',
)

# Folders the app expects next to the executable. import/ and export/ are in
# APP_FOLDERS in shared/environment.py; temp/ is created on first run and is
# pure scratch, so it is not shipped.
PLACEHOLDER_FOLDERS = ('import', 'export')

IMPORT_PLACEHOLDER = """\
This folder is for importing finished clips into your library.

Copy clips here, then run commcut.exe and press "Import".

If each video has a .cnfo record beside it -- a folder exported from another
commcut, say -- commcut reads the tags straight out of the records. If they do
not, the Library Mesh Wizard asks you what each folder name in here means, and
uses your answers. It never guesses a tag from a name on its own.

To cut a new compilation into clips you do not need this folder at all. Press
"Editor" and pick the source video from anywhere on your computer. Its .cmct file
(the detected boundaries) is written beside the video, and the finished clips go
to the export folder.
"""

EXPORT_PLACEHOLDER = """\
Named clips are written here, in the folder tree that the folder organization
scheme in Settings describes. Nothing else lives in this folder.
"""

# Git LFS pointer files are ~130 bytes of ASCII. A real ffmpeg.exe is over a
# hundred megabytes. Anything under this threshold is a pointer, not a binary,
# and shipping one produces a build that installs cleanly and then fails the
# first time it tries to cut a clip -- with no useful error.
MIN_BINARY_BYTES = 1024 * 1024

# What the archive calls this platform. Data rather than an if-chain so that
# the second platform is one edit here. check_platform() is the only way to
# reach the zip code, so there is nothing to branch on yet.
ARTIFACT_PLATFORM = 'windows-x64'

# The name used with no --version, for a local smoke test rather than a
# release. Matches PORTABLE_DIR, so the folder and the zip it becomes agree.
DEFAULT_ARCHIVE_ROOT = 'commcut-portable'

# What has to be in the archive, as paths relative to the top-level folder and
# always with forward slashes -- they are zip paths, not OS paths. The onefile
# build gets no on-disk layout check (its payload unpacks at run time), so this
# is what verifies a shipping archive at all, and it holds for --onedir too.
REQUIRED_ARCHIVE_ENTRIES = (
    'commcut.exe',
    'bin/win/ffmpeg.exe',
    'bin/win/ffprobe.exe',
    'bin/win/libmpv-2.dll',
    'import/README.txt',
    'export/README.txt',
)

# Read the archive's contents in chunks rather than whole: the biggest file in
# it is a 130 MB ffmpeg.exe, and its hash does not fit in a reason.
_HASH_CHUNK_BYTES = 1024 * 1024


class BuildError(Exception):
    """A pre-flight or post-build check failed."""


def _say(message):
    print(f"[build] {message}", flush=True)


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

def check_platform():
    system = platform.system().lower()
    if system != 'windows':
        raise BuildError(
            f"commcut only ships a Windows build. This machine reports "
            f"'{system}', and bin/{system}/ holds no binaries, so the result "
            f"would be an app that cannot cut a clip."
        )


def check_binaries():
    """Fail early if the bundled binaries are Git LFS pointers, not binaries.

    The repo tracks bin/**/*.exe and *.dll through Git LFS. A checkout without
    `git lfs pull` leaves pointer text in their place, and PyInstaller (and a
    plain file copy) will bundle that text without complaint.
    """
    for name in BINARIES:
        path = os.path.join(BIN_SOURCE_DIR, name)
        if not os.path.exists(path):
            raise BuildError(
                f"Missing bundled binary: {path}\n"
                f"Run `git lfs install` and `git lfs pull` if this repo uses "
                f"LFS for bin/."
            )
        size = os.path.getsize(path)
        if size < MIN_BINARY_BYTES:
            raise BuildError(
                f"{name} is only {size} bytes, which is a Git LFS pointer, not "
                f"a real binary.\n"
                f"Run `git lfs install` and `git lfs pull`, then rebuild."
            )
        _say(f"  {name}: {size / (1024 * 1024):.1f} MB")


def preflight():
    _say("checking platform")
    check_platform()
    _say("checking bundled binaries")
    check_binaries()


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

def normalize_version(text):
    """The bare version in `text`: no whitespace, no leading `v`.

    A tag is `v` followed by the version and the version constant in
    shared/version.py is neither, so the prefix is stripped here rather than
    stored twice. `v0.1.0` and `0.1.0` name the same release, and a build that
    treated them as different would refuse every correctly-formed tag.
    """
    return text.strip().lstrip('vV')


def check_requested_version(requested):
    """Refuse a --version that is not the version in shared/version.py.

    This is the whole tag-must-match rule, and it lives here because this is
    the one place that can refuse before a 400 MB build has happened. A tag is
    otherwise indistinguishable from a good one until somebody reads the
    release page.

    Returns the bare version, or None when no version was requested -- a local
    smoke test does not need one and should not have to bump the code to zip a
    build it is about to throw away.
    """
    if requested is None:
        return None

    wanted = normalize_version(requested)
    if wanted != VERSION:
        raise BuildError(
            f"Asked for version {requested!r} but shared/version.py says "
            f"{VERSION!r}.\n"
            f"A release tag has to match the code it releases. Edit "
            f"VERSION in shared/version.py, or tag the version that is "
            f"already there."
        )
    return wanted


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def run_pyinstaller(onedir):
    _say(f"running PyInstaller ({'onedir' if onedir else 'onefile'})")
    environment = dict(os.environ)
    # The spec reads this to pick onefile vs onedir. Not a CLI flag because the
    # spec has to be runnable on its own, which is how you debug a spec.
    environment['COMMCUT_ONEFILE'] = '0' if onedir else '1'
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", SPEC_PATH, "--noconfirm", "--clean"],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
    )


def _verify_payload(root):
    """Assert the .ui files landed where shared/environment.py will look.

    They are bundled, so they extract to sys._MEIPASS, not to the folder the
    exe sits in -- but their layout *inside* the payload must mirror the source
    tree, because resource_path() is called with one expression either way.
    """
    for relative in UI_FILES:
        if not os.path.exists(os.path.join(root, *relative.split('/'))):
            raise BuildError(
                f"{relative} is missing from the build. The .ui files must keep "
                f"their source-tree subfolders so resource_path() resolves the "
                f"same way frozen and unfrozen."
            )


def _assert_no_strays(root):
    """Nothing historical or test-only should ever be bundled."""
    for unwanted in ('prototypes', 'tests', '_internal'):
        if os.path.exists(os.path.join(root, unwanted)):
            raise BuildError(
                f"{unwanted}/ is present in the build. Nothing in the shipped "
                f"tree should import it."
            )


def assemble_portable(onedir):
    """Build dist/commcut-portable/ with everything the app needs beside it."""
    if os.path.exists(PORTABLE_DIR):
        shutil.rmtree(PORTABLE_DIR)
    os.makedirs(PORTABLE_DIR)

    if onedir:
        _say("assembling the portable folder (onedir)")
        # The onedir tree is already flat next to its exe.
        for entry in os.listdir(ONEDIR_APP_DIR):
            source = os.path.join(ONEDIR_APP_DIR, entry)
            target = os.path.join(PORTABLE_DIR, entry)
            if os.path.isdir(source):
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        _verify_payload(PORTABLE_DIR)
        _assert_no_strays(PORTABLE_DIR)
    else:
        _say("assembling the portable folder (onefile)")
        if not os.path.exists(ONEFILE_EXE):
            raise BuildError(f"PyInstaller did not produce {ONEFILE_EXE}")
        shutil.copy2(ONEFILE_EXE, os.path.join(PORTABLE_DIR, 'commcut.exe'))
        # A onefile payload is unpacked to a temp folder at run time, so the
        # .ui files cannot be checked on disk here. Their layout is asserted
        # by tests/test_frozen_mode.py instead.

    # bin/ beside the exe, which is what install_root() resolves against when
    # frozen. This is the one piece that must NOT be inside the exe.
    _say("copying bin/win")
    target_bin = os.path.join(PORTABLE_DIR, 'bin', 'win')
    os.makedirs(target_bin, exist_ok=True)
    for name in BINARIES:
        shutil.copy2(os.path.join(BIN_SOURCE_DIR, name),
                     os.path.join(target_bin, name))

    # import/ and export/ ship with a placeholder because empty folders do not
    # survive being zipped, and a distributable that arrives with no import/
    # looks broken.
    for name in PLACEHOLDER_FOLDERS:
        folder = os.path.join(PORTABLE_DIR, name)
        os.makedirs(folder, exist_ok=True)
        note = (IMPORT_PLACEHOLDER if name == 'import' else EXPORT_PLACEHOLDER)
        with open(os.path.join(folder, 'README.txt'), 'w',
                  encoding='utf-8', newline='\r\n') as handle:
            handle.write(note)

    total = sum(
        os.path.getsize(os.path.join(dirpath, file))
        for dirpath, _, files in os.walk(PORTABLE_DIR)
        for file in files
    )
    _say(f"  {total / (1024 * 1024):.0f} MB in {PORTABLE_DIR}")


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------

def _archive_root(version):
    """The single top-level folder the archive holds."""
    return f'commcut-{version}' if version else DEFAULT_ARCHIVE_ROOT


def archive_name(version):
    """The zip's filename, with no directory part."""
    return f'{_archive_root(version)}-{ARTIFACT_PLATFORM}.zip'


def _relative_paths(root):
    """Every file under `root`, as forward-slash relative paths, sorted.

    Sorted so two runs over the same tree produce the same archive in the same
    order. The bytes are not reproducible anyway -- PyInstaller embeds a build
    timestamp -- but a stable order makes a diff of two archives meaningful.
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            absolute = os.path.join(dirpath, name)
            relative = os.path.relpath(absolute, root)
            found.append(relative.replace(os.sep, '/'))
    return found


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b''):
            digest.update(block)
    return digest.hexdigest()


def write_sha256_sidecar(zip_path):
    """Write <zip>.sha256 next to the archive and return the digest.

    Two spaces, the way sha256sum writes it, so `sha256sum -c` accepts the file
    unmodified on Git Bash and a PowerShell `Get-FileHash` comparison is a
    substring test. The sidecar is a property of the bytes, so it is written
    here rather than by whatever ships the archive.
    """
    digest = _sha256(zip_path)
    name = os.path.basename(zip_path)
    with open(f'{zip_path}.sha256', 'w', encoding='utf-8', newline='\n') as out:
        out.write(f'{digest}  {name}\n')
    return digest


def zip_portable(version=None):
    """Zip PORTABLE_DIR and return the path. Refuses if the layout is wrong.

    `version` names the top-level folder and the file; None means
    commcut-portable-windows-x64.zip, which is the local smoke-test form.

    The layout check is what verifies a shipping archive. A onefile payload is
    unpacked at run time, so `_verify_payload` cannot see it on disk, and a
    build that installs cleanly and then cannot find its own ffmpeg is exactly
    the failure that is easiest to ship and hardest to diagnose.
    """
    root = _archive_root(version)
    present = set(_relative_paths(PORTABLE_DIR))
    missing = [e for e in REQUIRED_ARCHIVE_ENTRIES if e not in present]
    if missing:
        raise BuildError(
            f"The portable folder is missing {len(missing)} of the "
            f"{len(REQUIRED_ARCHIVE_ENTRIES)} files a distributable needs, so "
            f"it will not be zipped:\n\n"
            + "\n".join(f"  {e}" for e in missing)
            + f"\n\nin {PORTABLE_DIR}\n\n"
            f"bin/win/ is the one piece that must be beside the exe and is "
            f"copied in after PyInstaller runs; a build made without a "
            f"`git lfs pull` has pointer text there instead of binaries."
        )

    os.makedirs(DIST_DIR, exist_ok=True)
    zip_path = os.path.join(DIST_DIR, archive_name(version))

    _say(f"zipping {root}/")
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for relative in sorted(present):
            archive.write(
                os.path.join(PORTABLE_DIR, *relative.split('/')),
                f'{root}/{relative}',
            )

    digest = write_sha256_sidecar(zip_path)
    size = os.path.getsize(zip_path)
    _say(f"  {size / (1024 * 1024):.0f} MB {zip_path}")
    _say(f"  sha256 {digest}")
    return zip_path


def main():
    parser = argparse.ArgumentParser(
        description="Build commcut into a portable folder.")
    parser.add_argument(
        "--onedir", action="store_true",
        help="Build the folder form instead of the self-extracting exe. "
             "Starts faster and is the one to debug against.")
    parser.add_argument(
        "--check-only", action="store_true",
        help="Run the pre-flight checks and stop.")
    parser.add_argument(
        "--zip", action="store_true", dest="make_zip",
        help="Also write the distributable as a zip, with a sha256 sidecar. "
             "The archive holds one top-level folder, not a flat tree.")
    parser.add_argument(
        "--version", metavar="TAG", default=None,
        help=f"Name the archive for a release, e.g. v{VERSION}. Refused unless "
             f"it matches shared/version.py, so a mistagged release fails the "
             f"build. Only .github/workflows/release.yml needs this.")
    args = parser.parse_args()

    try:
        preflight()
        version = check_requested_version(args.version)
    except BuildError as error:
        print(f"\nBUILD FAILED (pre-flight):\n{error}", file=sys.stderr)
        return 1

    if args.check_only:
        _say("pre-flight checks passed")
        return 0

    try:
        run_pyinstaller(args.onedir)
        assemble_portable(args.onedir)
        if args.make_zip:
            zip_portable(version)
    except subprocess.CalledProcessError as error:
        print(f"\nBUILD FAILED: {error}", file=sys.stderr)
        return 1
    except BuildError as error:
        print(f"\nBUILD FAILED:\n{error}", file=sys.stderr)
        return 1

    _say("done")
    _say(f"run {os.path.join(PORTABLE_DIR, 'commcut.exe')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
