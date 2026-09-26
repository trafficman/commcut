"""Tests for how paths and child launches resolve inside a packaged build.

The suite runs unfrozen, so nothing here builds an executable. Instead it
monkeypatches the three attributes PyInstaller sets -- ``sys.frozen``,
``sys._MEIPASS``, and ``sys.executable`` -- and asserts the resolution logic
lands where the shipped build will put things.

That distinction is the whole point: unfrozen, there is exactly one project
root and the two-root split is academic. Frozen, get them wrong and the app
starts and then cannot find ffmpeg, its .ui files, or libmpv. The assertions
below are the contract the spec and build.py have to keep satisfying.
"""

import os

import pytest

import shared.environment as environment
from shared.environment import (
    APP_FOLDERS,
    MPV_VIDEO_OUTPUT,
    WINDOW_NAMES,
    ensure_app_folders,
    get_binary_path,
    install_root,
    is_frozen,
    launch_command,
    resource_path,
    resource_root,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# The .ui files, as they are looked up in code. Kept as (folder, name) pairs so
# the spec's datas mapping and this file cannot drift apart silently: the
# spec mirrors these subfolders into the payload precisely so that
# resource_path() is correct in both modes.
UI_FILES = (
    ("", "mainwindow.ui"),
    ("editor", "editorwindow.ui"),
    ("scanner", "scannerwindow.ui"),
    ("settings", "settingswindow.ui"),
)


@pytest.fixture
def frozen(monkeypatch, tmp_path):
    """Make the process look like a packaged build, rooted at tmp_path."""

    def apply(meipass=None, executable=None):
        install_dir = str(tmp_path / "install")
        os.makedirs(install_dir, exist_ok=True)
        monkeypatch.setattr(environment.sys, "frozen", True, raising=False)
        monkeypatch.setattr(
            environment.sys, "_MEIPASS",
            str(meipass if meipass is not None else tmp_path / "payload"),
            raising=False)
        monkeypatch.setattr(
            environment.sys, "executable",
            str(executable if executable is not None
                else os.path.join(install_dir, "commcut.exe")))
        return install_dir

    return apply


# ---------------------------------------------------------------------------
# The two roots
# ---------------------------------------------------------------------------

def test_unfrozen_roots_collapse_to_the_project_root():
    """From source there is one root, which is what makes running the app
    directly a valid way to develop it."""
    assert is_frozen() is False
    assert resource_root() == PROJECT_ROOT
    assert install_root() == PROJECT_ROOT


def test_frozen_install_root_is_the_executable_folder(frozen):
    """User data must not live in the extraction directory: that folder is
    deleted when the process exits, so settings.json and export/ would be lost
    between runs."""
    install_dir = frozen()

    assert install_root() == install_dir
    assert install_root() != resource_root()


def test_frozen_resource_root_is_the_payload(frozen, tmp_path):
    """Read-only bundled data comes from inside the payload."""
    frozen(meipass=tmp_path / "payload")
    (tmp_path / "payload").mkdir(exist_ok=True)

    assert resource_root() == str(tmp_path / "payload")


def test_contents_directory_flat_layout_makes_the_roots_agree(frozen, tmp_path):
    """The shipped spec sets contents_directory='.', so the payload sits in the
    same folder as the executable and the two roots coincide. This is the
    layout the app is actually tested against; the pair of tests above cover
    the general behaviour that onefile would need."""
    install_dir = frozen(meipass=tmp_path / "install")

    assert resource_root() == install_root() == install_dir


def test_resource_path_joins_against_the_resource_root(frozen, tmp_path):
    frozen(meipass=tmp_path / "payload")

    assert resource_path("settings", "settingswindow.ui") == os.path.join(
        str(tmp_path / "payload"), "settings", "settingswindow.ui")


# ---------------------------------------------------------------------------
# Bundled binaries
# ---------------------------------------------------------------------------

def test_frozen_bin_dir_sits_beside_the_executable(frozen):
    """bin/<os> is resolved against the install root, not against __file__.

    Resolving it against __file__ is the trap: frozen, __file__ points into
    the payload, which would send every ffmpeg/ffprobe call to a temporary
    directory while the app's own data lives beside the exe.
    """
    install_dir = frozen()

    assert environment.bin_dir() == os.path.join(install_dir, "bin", "win")


def test_bin_dir_is_never_inside_the_payload(frozen):
    """The explicit form of the trap above: a payload-derived bin_dir is
    wrong even when resource_root() happens to equal the payload."""
    payload = os.path.join(frozen(), "_internal")
    frozen(meipass=payload)
    os.makedirs(payload, exist_ok=True)

    assert not environment.bin_dir().startswith(payload)


def test_unfrozen_bin_dir_matches_the_source_tree():
    assert environment.bin_dir() == os.path.join(PROJECT_ROOT, "bin", "win")


def test_bin_folder_is_chosen_per_os(monkeypatch):
    """The mapping has to be data, not a chain of ifs, or the next platform
    has to edit code rather than a table."""
    for system, folder in (("Windows", "win"), ("Linux", "linux"),
                           ("Darwin", "mac")):
        monkeypatch.setattr(environment.platform, "system",
                            lambda value=system: value)
        assert environment.bin_dir().endswith(
            os.path.join("bin", folder)), system


def test_unsupported_os_raises_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(environment.platform, "system", lambda: "Plan9")

    with pytest.raises(EnvironmentError):
        environment.bin_dir()


def test_missing_binary_names_the_folder_it_looked_in():
    """A missing binary must say where it looked, or a packaged build that
    cannot find ffmpeg reports nothing useful at all."""
    with pytest.raises(FileNotFoundError) as error:
        get_binary_path("definitely-not-a-real-binary")

    message = str(error.value)
    assert "definitely-not-a-real-binary" in message
    assert environment.bin_dir() in message


def test_bundled_ffmpeg_and_ffprobe_exist_in_the_source_tree():
    """Guards the build's pre-flight assumption. If these are LFS pointers the
    build still succeeds and only fails at export time."""
    for name in ("ffmpeg", "ffprobe"):
        assert os.path.isfile(get_binary_path(name)), name


# ---------------------------------------------------------------------------
# mpv video output
# ---------------------------------------------------------------------------

def test_video_output_has_an_entry_for_this_platform():
    assert environment.video_output() == MPV_VIDEO_OUTPUT[
        environment.platform.system().lower()]


def test_windows_keeps_direct3d():
    """The WA_NativeWindow attribute on the video frame exists for this driver.
    Changing it on Windows is not a portability tweak, it is a break."""
    assert MPV_VIDEO_OUTPUT["windows"] == "direct3d"


def test_video_output_raises_for_an_unsupported_os(monkeypatch):
    monkeypatch.setattr(environment.platform, "system", lambda: "Plan9")

    with pytest.raises(EnvironmentError):
        environment.video_output()


# ---------------------------------------------------------------------------
# Child window launching
# ---------------------------------------------------------------------------

def test_unfrozen_launch_uses_the_window_script():
    """Running from source must keep launching the real script, or the suite
    would no longer be exercising the code that ships."""
    for name in WINDOW_NAMES:
        command = launch_command(name)
        assert command[0] == environment.sys.executable
        assert command[1].endswith(".py")
        assert os.path.isfile(command[1]), name


def test_frozen_launch_re_executes_the_binary(frozen):
    """The packaged build has no .py files, so it re-runs itself with a flag
    that main.py dispatches on."""
    install_dir = frozen()

    assert launch_command("scanner") == [
        os.path.join(install_dir, "commcut.exe"), "--window", "scanner"]


def test_frozen_launch_ignores_a_missing_script(frozen, tmp_path):
    """Frozen, the scripts genuinely do not exist, so the existence check must
    not run. A check that fired here would make every child launch fail."""
    frozen()

    assert len(launch_command("editor")) == 3


def test_launch_refuses_an_unknown_window():
    with pytest.raises(ValueError) as error:
        launch_command("not-a-window")

    for name in WINDOW_NAMES:
        assert name in str(error.value)


def test_every_registered_window_has_a_script():
    """The registry is the only place that knows which windows exist; if a
    window is added without a script, the packaged build finds out at runtime
    and the source build does not."""
    for name in WINDOW_NAMES:
        assert os.path.isfile(
            os.path.join(PROJECT_ROOT, launch_command(name)[1]))


# ---------------------------------------------------------------------------
# Writable app folders
# ---------------------------------------------------------------------------

def test_ensure_app_folders_creates_them_all(monkeypatch, tmp_path):
    monkeypatch.setattr(environment, "install_root", lambda: str(tmp_path))

    created = ensure_app_folders()

    assert len(created) == len(APP_FOLDERS)
    for name in APP_FOLDERS:
        assert (tmp_path / name).is_dir(), name


def test_ensure_app_folders_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(environment, "install_root", lambda: str(tmp_path))

    ensure_app_folders()
    (tmp_path / "export" / "keepme.mp4").write_bytes(b"x")
    ensure_app_folders()

    assert (tmp_path / "export" / "keepme.mp4").read_bytes() == b"x"


def test_non_writable_install_root_raises(monkeypatch, tmp_path):
    """Reported, not swallowed: every later save and export would fail the
    same way with a far less obvious message."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    monkeypatch.setattr(environment, "install_root", lambda: str(blocker))

    with pytest.raises(OSError):
        ensure_app_folders()


# ---------------------------------------------------------------------------
# Argv dispatch
# ---------------------------------------------------------------------------

def _dispatch(monkeypatch, argv):
    """Run main.main() with the filesystem and dialogs stubbed out.

    The real main() creates the app folders and, on failure, pops a modal
    dialog. Neither belongs in a test.
    """
    import main
    from shared import diagnostics

    monkeypatch.setattr(main, "ensure_app_folders", lambda: [])
    reported = []
    monkeypatch.setattr(
        diagnostics, "fatal",
        lambda title, text: reported.append((title, text)))
    return main.main(argv), reported


def test_window_flag_without_a_name_is_rejected(monkeypatch):
    """`commcut --window` names no window, so it must exit rather than open
    the main menu and leave the user wondering."""
    code, reported = _dispatch(monkeypatch, ["commcut.exe", "--window"])

    assert code == 2
    assert reported
    for name in WINDOW_NAMES:
        assert name in reported[0][1]


def test_unknown_window_is_reported_not_raised(monkeypatch):
    """An uncaught error here would reach the bootloader's modal traceback
    dialog and hang. main() must turn it into an exit code instead."""
    code, reported = _dispatch(
        monkeypatch, ["commcut.exe", "--window", "nope"])

    assert code == 1
    assert reported
    assert "nope" in reported[0][1]


def test_a_window_that_cannot_start_is_reported_not_raised(monkeypatch):
    """Same guarantee for a real window failing during startup -- the missing
    source video is the case that actually happens on a fresh install."""
    import main

    monkeypatch.setattr(main, "ensure_app_folders", lambda: [])
    monkeypatch.setattr(
        main, "_run_window",
        lambda name, rest: (_ for _ in ()).throw(
            FileNotFoundError(f"No source video found.\n\n{'-' * 40}")))
    reported = []
    from shared import diagnostics
    monkeypatch.setattr(
        diagnostics, "fatal", lambda title, text: reported.append((title, text)))

    code = main.main(["commcut.exe", "--window", "scanner"])

    assert code == 1
    assert reported
    assert "No source video" in reported[0][1]


def test_known_window_is_dispatched(monkeypatch):
    """The happy path must actually reach the window's own entry point."""
    import main

    monkeypatch.setattr(main, "ensure_app_folders", lambda: [])
    seen = []
    monkeypatch.setattr(
        main, "_run_window", lambda name, rest: seen.append((name, rest)) or 7)

    code = main.main(["commcut.exe", "--window", "editor"])

    assert code == 7
    assert seen == [("editor", [])]


def test_bootstrap_failure_exits_before_opening_anything(monkeypatch):
    """An unwritable install root is reported once, up front, rather than
    resurfacing later as a failed save or a failed export."""
    import main
    from shared import diagnostics

    monkeypatch.setattr(
        main, "ensure_app_folders",
        lambda: (_ for _ in ()).throw(OSError("access denied")))
    reported = []
    monkeypatch.setattr(
        diagnostics, "fatal", lambda title, text: reported.append((title, text)))

    code = main.main(["commcut.exe"])

    assert code == 1
    assert reported
    assert "access denied" in reported[0][1]


# ---------------------------------------------------------------------------
# Bundled .ui files
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("folder,name", UI_FILES)
def test_ui_file_resolves_unfrozen(folder, name):
    """Unfrozen, resource_path must land on the file in the source tree."""
    path = resource_path(folder, name) if folder else resource_path(name)

    assert os.path.isfile(path), path


def test_source_and_payload_layouts_agree():
    """The subfolder each .ui file sits in, in the source tree, is the one the
    spec must mirror. Read the spec's datas list and compare, so a .ui file
    that is moved cannot be silently mis-bundled."""
    spec_path = os.path.join(PROJECT_ROOT, "packaging", "commcut.spec")
    with open(spec_path, encoding="utf-8") as handle:
        spec_text = handle.read()

    for folder, name in UI_FILES:
        # Forward slashes regardless of host: PyInstaller datas entries are
        # always written that way, including the separator inside the path.
        source = f"{folder}/{name}" if folder else name
        destination = folder if folder else "."
        assert f"('{source}', '{destination}')" in spec_text, source
