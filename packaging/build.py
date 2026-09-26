"""Build commcut into a portable folder you can zip and hand to someone.

    python packaging/build.py                # onefile commcut.exe (shipping)
    python packaging/build.py --onedir       # folder build, starts faster
    python packaging/build.py --check-only   # pre-flight checks only

The output is dist/commcut-portable/:

    commcut.exe      self-extracting: unpacks Python + PySide6 + the app to a
                     temp folder and runs. No Python needed on the target.
    bin/win/         ffmpeg.exe, ffprobe.exe, libmpv-2.dll -- 366 MB, kept
                     beside the exe rather than inside it
    import/          drop a compilation video in here, named test.mp4
    export/          named clips are written here
    commcut.log      written on first run
    settings.json    written when you save in Settings

Why the binaries are NOT inside the exe: onefile re-extracts its entire
payload on every launch, and each window is a separate process (the main menu
stays open while the scanner runs, and the scanner launches the editor). With
the binaries bundled that is ~366 MB of extraction per window open. Kept
outside, the exe carries only Python and Qt -- around 120 MB -- and bin/win/
is found immediately, with no extraction step.

Why onefile rather than a folder with a SFX installer: this is a portable
smoke-test build, so "copy the folder and run commcut.exe" is the whole
install story. There is no installer to run, no elevation prompt, and nothing
written outside the folder.

Note that empty folders do not survive being zipped, which is why import/ and
export/ ship with a placeholder file. See PLACEHOLDER_* below.
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGING_DIR = os.path.join(PROJECT_ROOT, 'packaging')
SPEC_PATH = os.path.join(PACKAGING_DIR, 'commcut.spec')
DIST_DIR = os.path.join(PROJECT_ROOT, 'dist')

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
)

# Folders the app expects next to the executable. import/ and export/ are in
# APP_FOLDERS in shared/environment.py; temp/ is created on first run and is
# pure scratch, so it is not shipped.
PLACEHOLDER_FOLDERS = ('import', 'export')

IMPORT_PLACEHOLDER = """\
Put a compilation video in this folder and name it:

    test.mp4

Then run commcut.exe and press "Editor". That is the only filename the
smoke-test build looks for; shared/segments.py holds the one definition of it
(DEFAULT_SOURCE_NAME).

The editor writes test.cmct next to the video, and the same video is what
the segment scanner reads.
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
    args = parser.parse_args()

    try:
        preflight()
    except BuildError as error:
        print(f"\nBUILD FAILED (pre-flight):\n{error}", file=sys.stderr)
        return 1

    if args.check_only:
        _say("pre-flight checks passed")
        return 0

    try:
        run_pyinstaller(args.onedir)
        assemble_portable(args.onedir)
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
