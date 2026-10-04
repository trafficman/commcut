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

import ast
import os

import pytest

import shared.environment as environment
from shared.environment import (
    APP_FOLDERS,
    MPV_VIDEO_OUTPUT,
    ensure_app_folders,
    get_binary_path,
    install_root,
    is_frozen,
    no_console_kwargs,
    resource_path,
    resource_root,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bin_dir_for(root):
    """The bin/<os>/ folder under `root` for whichever platform is running.

    Naming a specific folder here would be what makes this file fail on any
    platform but Windows; the mapping is the module's table, and the tests that
    assert the table itself are the ones below that check it directly.
    """
    folder = environment._OS_BIN_FOLDERS[environment._os_key()]
    return os.path.join(root, "bin", folder)


def bundles_its_own_binaries():
    """True when this platform expects its binaries in bin/<os>/."""
    return environment._os_key() in environment._BUNDLED_BINARY_PLATFORMS


# The .ui files, as they are looked up in code. Kept as (folder, name) pairs so
# the spec's datas mapping and this file cannot drift apart silently: the
# spec mirrors these subfolders into the payload precisely so that
# resource_path() is correct in both modes.
UI_FILES = (
    ("", "mainwindow.ui"),
    ("editor", "editorwindow.ui"),
    ("scanner", "scannerwindow.ui"),
    ("settings", "settingswindow.ui"),
    ("importer", "meshwindow.ui"),
    ("shared", "tagform.ui"),
    ("importer", "queuewindow.ui"),
    ("importer", "valueswindow.ui"),
)

# Read-only resources that are not .ui files. Deliberately a separate list: the
# walk further down asserts that *every* .ui in the tree is listed, and that would
# be the wrong claim to make about a screenshot dropped into docs/. Everything
# resource_path() is asked for has to be in PAYLOAD_FILES, and the banner is asked
# for.
BANNER_FILES = (
    ("assets", "commcut_banner.png"),
)

PAYLOAD_FILES = UI_FILES + BANNER_FILES


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

    assert environment.bin_dir() == bin_dir_for(install_dir)


def test_bin_dir_is_never_inside_the_payload(frozen):
    """The explicit form of the trap above: a payload-derived bin_dir is
    wrong even when resource_root() happens to equal the payload."""
    payload = os.path.join(frozen(), "_internal")
    frozen(meipass=payload)
    os.makedirs(payload, exist_ok=True)

    assert not environment.bin_dir().startswith(payload)


def test_unfrozen_bin_dir_matches_the_source_tree():
    assert environment.bin_dir() == bin_dir_for(PROJECT_ROOT)


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


@pytest.mark.skipif(
    not bundles_its_own_binaries(),
    reason="bin/<os>/ is a placeholder on a platform that uses the system's "
           "ffmpeg, so there is nothing bundled to check",
)
def test_bundled_ffmpeg_and_ffprobe_exist_in_the_source_tree():
    """Guards the build's pre-flight assumption. If these are LFS pointers the
    build still succeeds and only fails at export time."""
    for name in ("ffmpeg", "ffprobe"):
        assert os.path.isfile(get_binary_path(name)), name


@pytest.mark.skipif(
    bundles_its_own_binaries(),
    reason="a platform that bundles its binaries never consults these",
)
def test_a_platform_without_bundled_binaries_is_told_where_to_install_them():
    """The source installs' version of the pre-flight: the names it looks for
    are the ones a package manager actually provides."""
    with pytest.raises(FileNotFoundError) as error:
        get_binary_path("ffmpeg")

    assert "ffmpeg" in str(error.value)
    assert "install" in str(error.value).lower()


# ---------------------------------------------------------------------------
# Where the binaries come from
# ---------------------------------------------------------------------------

def test_a_bundled_binary_is_preferred_over_the_system(monkeypatch, tmp_path):
    """The one behaviour that must not drift on Windows: bin/win/ wins, and a
    system ffmpeg is never substituted for it."""
    if not bundles_its_own_binaries():
        pytest.skip("a platform that does not bundle has no bundled candidate")

    prefix = tmp_path / "system"
    prefix.mkdir()
    (prefix / "ffprobe.exe").write_bytes(b"not the one we ship")
    monkeypatch.setattr(environment, "_SYSTEM_BIN_PREFIXES",
                        {"windows": (str(prefix),)})

    assert get_binary_path("ffprobe") == os.path.join(
        environment.bin_dir(), "ffprobe.exe")


def test_a_platform_that_bundles_refuses_to_fall_back(monkeypatch, tmp_path):
    """Silence here would mean a packaged build quietly running an ffmpeg
    nobody tested, which is the failure this policy exists to prevent."""
    monkeypatch.setattr(environment.platform, "system", lambda: "Windows")
    monkeypatch.setattr(environment, "_bin_dir", lambda: str(tmp_path / "bin"))
    monkeypatch.setattr(environment, "_SYSTEM_BIN_PREFIXES",
                        {"windows": (str(tmp_path),)})
    present = tmp_path / "ffmpeg.exe"
    present.write_bytes(b"system ffmpeg")

    with pytest.raises(FileNotFoundError) as error:
        get_binary_path("ffmpeg")

    assert str(present) not in str(error.value)
    assert "will not fall back" in str(error.value)


def test_a_platform_that_does_not_bundle_resolves_from_a_prefix(
    monkeypatch, tmp_path
):
    """The macOS and Linux source installs: the user's own ffmpeg, found by an
    absolute path rather than by name."""
    monkeypatch.setattr(environment.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(environment, "_bin_dir", lambda: str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    prefix = tmp_path / "homebrew"
    prefix.mkdir()
    installed = prefix / "ffmpeg"
    installed.write_bytes(b"#!/bin/sh\n")
    installed.chmod(0o755)
    monkeypatch.setattr(environment, "_SYSTEM_BIN_PREFIXES",
                        {"darwin": (str(prefix),)})

    assert get_binary_path("ffmpeg") == str(installed)


def test_a_non_executable_file_does_not_count_as_installed(monkeypatch, tmp_path):
    """A file that is there but cannot be run is not a working ffmpeg, and
    reporting it as found turns into a permission error at the first cut.

    os.access is stubbed rather than relying on a real permission bit: Windows
    has no execute bit, so chmod cannot produce this state on the machine these
    tests mostly run on.
    """
    monkeypatch.setattr(environment.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(environment, "_bin_dir", lambda: str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    prefix = tmp_path / "brew"
    prefix.mkdir()
    (prefix / "ffmpeg").write_bytes(b"not executable")
    monkeypatch.setattr(environment, "_SYSTEM_BIN_PREFIXES",
                        {"darwin": (str(prefix),)})
    asked = []
    monkeypatch.setattr(
        environment.os, "access",
        lambda path, mode: asked.append(mode) or False)

    with pytest.raises(FileNotFoundError):
        get_binary_path("ffmpeg")

    assert os.X_OK in asked


def test_the_exhausted_search_names_every_place_it_looked(monkeypatch, tmp_path):
    """The failure a macOS user actually gets, which has to be actionable: the
    error is their only clue about where commcut was looking."""
    monkeypatch.setattr(environment.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(environment, "_bin_dir", lambda: str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    monkeypatch.setattr(environment, "_SYSTEM_BIN_PREFIXES",
                        {"darwin": (str(tmp_path / "a"), str(tmp_path / "b"))})

    with pytest.raises(FileNotFoundError) as error:
        get_binary_path("ffprobe")

    message = str(error.value)
    for expected in (str(tmp_path / "empty"), str(tmp_path / "a"),
                     str(tmp_path / "b")):
        assert expected in message


# ---------------------------------------------------------------------------
# libmpv
# ---------------------------------------------------------------------------

def test_the_env_var_overrides_the_libmpv_search(monkeypatch, tmp_path):
    """The documented escape hatch for a machine whose mpv does not install a
    loadable library anywhere commcut looks."""
    override = tmp_path / "mpv.framework" / "mpv"
    override.parent.mkdir()
    override.write_bytes(b"\xcf\xfa\xed\xfe")
    monkeypatch.setattr(environment.os, "environ",
                        {environment.MPV_LIBRARY_ENV_VAR: str(override)})

    assert environment.resolve_mpv_library() == str(override)


def test_a_set_but_missing_override_is_an_error_rather_than_a_fallback(
    monkeypatch, tmp_path
):
    """An override that silently does nothing is worse than no override: the
    user believes they have pointed commcut at the right library."""
    monkeypatch.setattr(environment.os, "environ",
                        {environment.MPV_LIBRARY_ENV_VAR:
                         str(tmp_path / "nope.dylib")})

    with pytest.raises(FileNotFoundError) as error:
        environment.resolve_mpv_library()

    assert environment.MPV_LIBRARY_ENV_VAR in str(error.value)


def test_a_relative_override_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(environment.os, "environ",
                        {environment.MPV_LIBRARY_ENV_VAR: "libmpv.2.dylib"})

    with pytest.raises(FileNotFoundError) as error:
        environment.resolve_mpv_library()

    assert "absolute" in str(error.value)


def test_libmpv_resolves_out_of_the_bundled_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(environment.platform, "system", lambda: "Windows")
    bundled = tmp_path / "bin"
    bundled.mkdir()
    (bundled / "libmpv-2.dll").write_bytes(b"MZ")
    monkeypatch.setattr(environment, "_bin_dir", lambda: str(bundled))
    monkeypatch.setattr(environment.os, "environ", {})

    assert environment.resolve_mpv_library() == str(bundled / "libmpv-2.dll")


def test_libmpv_resolves_out_of_a_system_prefix(monkeypatch, tmp_path):
    monkeypatch.setattr(environment.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(environment.os, "environ", {})
    prefix = tmp_path / "opt" / "homebrew"
    (prefix / "lib").mkdir(parents=True)
    (prefix / "lib" / "libmpv.2.dylib").write_bytes(b"\xcf\xfa\xed\xfe")
    monkeypatch.setattr(environment, "_bin_dir", lambda: str(tmp_path / "empty"))
    monkeypatch.setattr(environment, "_SYSTEM_BIN_PREFIXES",
                        {"darwin": (str(prefix),)})

    assert environment.resolve_mpv_library() == str(
        prefix / "lib" / "libmpv.2.dylib")


def test_a_missing_libmpv_explains_which_library_is_needed(monkeypatch, tmp_path):
    """The whole reason this resolution is rewritten: the alternative is a bare
    ctypes OSError from inside python-mpv naming none of these folders."""
    monkeypatch.setattr(environment.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(environment.os, "environ", {})
    monkeypatch.setattr(environment, "_bin_dir", lambda: str(tmp_path / "empty"))
    monkeypatch.setattr(environment, "_SYSTEM_BIN_PREFIXES",
                        {"darwin": (str(tmp_path / "brew"),)})

    with pytest.raises(FileNotFoundError) as error:
        environment.resolve_mpv_library()

    message = str(error.value)
    assert "libmpv" in message
    assert environment.MPV_LIBRARY_ENV_VAR in message


def test_libmpv_resolves_out_of_the_linux_multiarch_directory(
    monkeypatch, tmp_path
):
    """The directory a distro package actually uses.

    ``apt install libmpv2`` -- the command shared/environment.py's own error
    message tells the user to run -- installs
    ``/usr/lib/x86_64-linux-gnu/libmpv.so.2``. That is not ``/usr/lib``, and no
    prefix in _SYSTEM_BIN_PREFIXES contains it, so a prefix-only search misses
    exactly the case the error message recommends creating.

    The root is substituted rather than used verbatim because these paths are
    built with ``os.path.join``, which on a Windows host -- where this suite
    mostly runs -- would put backslashes in a path that has to be a POSIX one on
    the platform that cares.
    """
    monkeypatch.setattr(environment.platform, "system", lambda: "Linux")
    monkeypatch.setattr(environment.os, "environ", {})
    root = tmp_path / "usr" / "lib"
    multiarch = root / "x86_64-linux-gnu"
    multiarch.mkdir(parents=True)
    (multiarch / "libmpv.so.2").write_bytes(b"\x7fELF")
    monkeypatch.setattr(environment, "_bin_dir", lambda: str(tmp_path / "empty"))
    monkeypatch.setattr(environment, "_SYSTEM_BIN_PREFIXES", {"linux": ()})
    monkeypatch.setattr(environment, "_MULTIARCH_LIB_ROOT", str(root))
    monkeypatch.setattr(environment.sysconfig, "get_config_var",
                        lambda name: "x86_64-linux-gnu")

    assert environment.resolve_mpv_library() == str(multiarch / "libmpv.so.2")


def test_the_multiarch_triplet_is_read_from_sysconfig_rather_than_written_out(
    monkeypatch
):
    """A hardcoded x86_64 triplet would silently miss on aarch64, which is a real
    target for this app rather than a hypothetical one."""
    monkeypatch.setattr(environment.platform, "system", lambda: "Linux")
    monkeypatch.setattr(environment.sysconfig, "get_config_var",
                        lambda name: "aarch64-linux-gnu")

    assert os.path.join(environment._MULTIARCH_LIB_ROOT,
                        "aarch64-linux-gnu") in environment.system_lib_dirs()


def test_the_multiarch_root_is_the_one_a_distro_uses():
    """A string comparison, so it holds on any host. Changing this constant to
    anything else stops finding the libraries it exists to find."""
    assert environment._MULTIARCH_LIB_ROOT == "/usr/lib"


def test_macos_gets_no_multiarch_directory(monkeypatch):
    """The multiarch layout is a Linux dpkg convention. Adding it to macOS
    would put a path that never exists into the error message's search list."""
    monkeypatch.setattr(environment.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(environment.sysconfig, "get_config_var",
                        lambda name: "x86_64-linux-gnu")

    assert not any(directory.endswith("x86_64-linux-gnu")
                   for directory in environment.system_lib_dirs())


def test_the_exhausted_libmpv_search_names_the_multiarch_directory(
    monkeypatch, tmp_path
):
    """A list that silently dropped the directories it did not find would make
    the error message worse, not shorter."""
    monkeypatch.setattr(environment.platform, "system", lambda: "Linux")
    monkeypatch.setattr(environment.os, "environ", {})
    root = tmp_path / "usr" / "lib"
    monkeypatch.setattr(environment, "_bin_dir", lambda: str(tmp_path / "empty"))
    monkeypatch.setattr(environment, "_SYSTEM_BIN_PREFIXES", {"linux": ()})
    monkeypatch.setattr(environment, "_MULTIARCH_LIB_ROOT", str(root))
    monkeypatch.setattr(environment.sysconfig, "get_config_var",
                        lambda name: "x86_64-linux-gnu")

    with pytest.raises(FileNotFoundError) as error:
        environment.resolve_mpv_library()

    assert os.path.join(str(root), "x86_64-linux-gnu",
                        "libmpv.so.2") in str(error.value)


def test_the_import_context_answers_python_mpvs_own_lookup(
    monkeypatch, tmp_path
):
    """The part that is not obvious, and the whole reason the pre-load alone
    is not enough.

    python-mpv 1.0.8's POSIX branch asks find_library('mpv') and *raises* if
    that returns None -- it never checks whether libmpv is already mapped. So a
    library commcut resolved but find_library cannot name is one python-mpv
    refuses to load, and the fix has to be to answer the lookup.
    """
    bundled = tmp_path / "bin"
    bundled.mkdir()
    (bundled / "libmpv-2.dll").write_bytes(b"MZ")
    monkeypatch.setattr(environment.platform, "system", lambda: "Windows")
    monkeypatch.setattr(environment.os, "environ", {})
    monkeypatch.setattr(environment, "_bin_dir", lambda: str(bundled))
    monkeypatch.setattr(environment.ctypes, "CDLL",
                        lambda path, **kwargs: "handle")
    monkeypatch.setattr(environment, "_mpv_library_handle", None)
    asked = []

    with environment.mpv_import_context() as path:
        for name in environment._MPV_LOOKUP_NAMES["windows"]:
            asked.append(environment.ctypes.util.find_library(name))

    assert asked == [path] * len(environment._MPV_LOOKUP_NAMES["windows"])


def test_the_two_libmpv_tables_agree():
    """Every filename the resolver can return must also be a name the lookup
    override answers for.

    The two tables exist for the same call and could drift apart: a name added
    to one and not the other means resolving a library by a filename while
    python-mpv asks under a spelling this module does not intercept -- which is
    the source-install failure all over again. The POSIX branches ask for the
    bare 'mpv', so that one is required on every platform.
    """
    for system in environment._OS_BIN_FOLDERS:
        lookup = environment._MPV_LOOKUP_NAMES[system]
        assert "mpv" in lookup, system
        for name in environment._MPV_LIBRARY_NAMES[system]:
            assert name in lookup, (system, name)


def test_the_import_context_leaves_find_library_alone_afterwards(
    monkeypatch, tmp_path
):
    """It is a context manager for a reason: the override is not left behind
    for the rest of the process, so nothing else resolves a library through it.
    """
    bundled = tmp_path / "bin"
    bundled.mkdir()
    (bundled / "libmpv-2.dll").write_bytes(b"MZ")
    monkeypatch.setattr(environment.platform, "system", lambda: "Windows")
    monkeypatch.setattr(environment.os, "environ", {})
    monkeypatch.setattr(environment, "_bin_dir", lambda: str(bundled))
    monkeypatch.setattr(environment.ctypes, "CDLL",
                        lambda path, **kwargs: "handle")
    monkeypatch.setattr(environment, "_mpv_library_handle", None)
    before = environment.ctypes.util.find_library

    with environment.mpv_import_context():
        assert environment.ctypes.util.find_library is not before

    assert environment.ctypes.util.find_library is before


def test_the_import_context_restores_find_library_even_when_the_import_fails(
    monkeypatch, tmp_path
):
    """A failed `import mpv` is the normal case on a machine with no libmpv,
    and it must not leave a patched find_library behind for the error dialog
    and the log to resolve things through."""
    bundled = tmp_path / "bin"
    bundled.mkdir()
    (bundled / "libmpv-2.dll").write_bytes(b"MZ")
    monkeypatch.setattr(environment.platform, "system", lambda: "Windows")
    monkeypatch.setattr(environment.os, "environ", {})
    monkeypatch.setattr(environment, "_bin_dir", lambda: str(bundled))
    monkeypatch.setattr(environment.ctypes, "CDLL",
                        lambda path, **kwargs: "handle")
    monkeypatch.setattr(environment, "_mpv_library_handle", None)
    before = environment.ctypes.util.find_library

    with pytest.raises(RuntimeError):
        with environment.mpv_import_context():
            raise RuntimeError("import mpv failed")

    assert environment.ctypes.util.find_library is before


def test_the_import_context_defers_other_libraries_to_the_real_lookup(
    monkeypatch, tmp_path
):
    """Only libmpv's names are answered. Anything else still has to go through
    the real function, or this would be a process-wide change in how every
    ctypes consumer resolves a library.

    A name that genuinely does not exist resolves to None either way, so the
    discriminator is that it must not come back as our libmpv path.
    """
    bundled = tmp_path / "bin"
    bundled.mkdir()
    (bundled / "libmpv-2.dll").write_bytes(b"MZ")
    monkeypatch.setattr(environment.platform, "system", lambda: "Windows")
    # A real environment, so the delegated lookup has a PATH to search.
    monkeypatch.setattr(environment.os, "environ", dict(environment.os.environ))
    monkeypatch.setattr(environment, "_bin_dir", lambda: str(bundled))
    monkeypatch.setattr(environment.ctypes, "CDLL",
                        lambda path, **kwargs: "handle")
    monkeypatch.setattr(environment, "_mpv_library_handle", None)

    with environment.mpv_import_context() as path:
        assert environment.ctypes.util.find_library("no-such-library-xyz") is None

    assert path.endswith("libmpv-2.dll")


def test_setting_dyld_library_path_on_macos(monkeypatch, tmp_path):
    """A secondary measure, for libmpv's own transitive dylibs. Skipped on
    Linux, where LD_LIBRARY_PATH semantics for runtime dlopen are murkier."""
    library = tmp_path / "brew" / "lib" / "libmpv.2.dylib"
    library.parent.mkdir(parents=True)
    library.write_bytes(b"\xcf\xfa\xed\xfe")
    monkeypatch.setattr(environment.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(environment.os, "environ", {})
    monkeypatch.setattr(environment, "_bin_dir", lambda: str(tmp_path / "empty"))
    monkeypatch.setattr(environment, "_SYSTEM_BIN_PREFIXES",
                        {"darwin": (str(tmp_path / "brew"),)})
    monkeypatch.setattr(environment.ctypes, "CDLL",
                        lambda path, **kwargs: "handle")
    monkeypatch.setattr(environment, "_mpv_library_handle", None)

    with environment.mpv_import_context():
        pass

    assert str(library.parent) in environment.os.environ["DYLD_LIBRARY_PATH"]


def test_no_dyld_library_path_on_windows(monkeypatch, tmp_path):
    """A stray DYLD_* on Windows would be meaningless and misleading."""
    bundled = tmp_path / "bin"
    bundled.mkdir()
    (bundled / "libmpv-2.dll").write_bytes(b"MZ")
    monkeypatch.setattr(environment.platform, "system", lambda: "Windows")
    monkeypatch.setattr(environment.os, "environ", {})
    monkeypatch.setattr(environment, "_bin_dir", lambda: str(bundled))
    monkeypatch.setattr(environment.ctypes, "CDLL",
                        lambda path, **kwargs: "handle")
    monkeypatch.setattr(environment, "_mpv_library_handle", None)

    with environment.mpv_import_context():
        pass

    assert "DYLD_LIBRARY_PATH" not in environment.os.environ


def test_the_library_is_loaded_once_per_process(monkeypatch, tmp_path):
    """Two players in one process must share one mapped library, not re-load
    it -- and certainly not from a test, where the real one is 110 MB."""
    bundled = tmp_path / "bin"
    bundled.mkdir()
    (bundled / "libmpv-2.dll").write_bytes(b"MZ")
    monkeypatch.setattr(environment.platform, "system", lambda: "Windows")
    monkeypatch.setattr(environment.os, "environ", {})
    monkeypatch.setattr(environment, "_bin_dir", lambda: str(bundled))
    loads = []
    monkeypatch.setattr(
        environment.ctypes, "CDLL",
        lambda path, **kwargs: loads.append(path) or "handle")
    monkeypatch.setattr(environment, "_mpv_library_handle", None)

    environment.load_mpv_library()
    environment.load_mpv_library()

    assert loads == [str(bundled / "libmpv-2.dll")]


# ---------------------------------------------------------------------------
# mpv video output
# ---------------------------------------------------------------------------

def test_every_platform_with_a_bin_folder_has_a_video_output():
    """The table-completeness claim, which runs on any host.

    The obvious version of this test -- asserting video_output() equals
    MPV_VIDEO_OUTPUT[platform.system()] -- indexes the same dict with the same
    key the function uses, so it cannot fail on any platform and proves
    nothing. This is the version that can.
    """
    for system in environment._OS_BIN_FOLDERS:
        assert environment.MPV_VIDEO_OUTPUT.get(system), system


def test_every_platform_gets_a_non_empty_video_output():
    assert environment.video_output()
    assert isinstance(environment.video_output(), str)


def test_every_platform_that_does_not_bundle_knows_where_to_look():
    """Otherwise the platform silently has no way to find ffmpeg at all, and
    the failure is a FileNotFoundError with nothing in it."""
    for system in environment._OS_BIN_FOLDERS:
        if system in environment._BUNDLED_BINARY_PLATFORMS:
            continue
        assert environment._SYSTEM_BIN_PREFIXES.get(system), system


def test_every_platform_that_does_not_bundle_has_a_library_directory(
    monkeypatch
):
    """The ffmpeg prefixes and the libmpv directories are separate lists, so a
    platform can satisfy the check above and still have nowhere to look for a
    library -- which is a FileNotFoundError naming two empty folders."""
    for system in environment._OS_BIN_FOLDERS:
        if system in environment._BUNDLED_BINARY_PLATFORMS:
            continue
        monkeypatch.setattr(environment.platform, "system", lambda: system)
        assert environment.system_lib_dirs(), system


def test_every_platform_knows_its_libmpv_filename():
    for system in environment._OS_BIN_FOLDERS:
        assert environment._MPV_LIBRARY_NAMES.get(system), system


def test_windows_keeps_direct3d():
    """The WA_NativeWindow attribute on the video frame exists for this driver.
    Changing it on Windows is not a portability tweak, it is a break."""
    assert MPV_VIDEO_OUTPUT["windows"] == "direct3d"


def test_video_output_raises_for_an_unsupported_os(monkeypatch):
    monkeypatch.setattr(environment.platform, "system", lambda: "Plan9")

    with pytest.raises(EnvironmentError):
        environment.video_output()


# ---------------------------------------------------------------------------
# Window building
# ---------------------------------------------------------------------------

def test_every_registered_window_resolves_to_a_builder():
    """The registry is the only place that knows which windows exist and how to
    build each one. A name pointing at a module that does not exist, or at an
    attribute that is not callable, fails here rather than when a user presses
    a button — and it is the same failure frozen and unfrozen, because the
    registry is Python rather than a script on disk."""
    import importlib

    from shared.session import WINDOW_NAMES, _BUILDERS

    assert set(_BUILDERS) == set(WINDOW_NAMES)
    for name, (module_name, builder_name) in _BUILDERS.items():
        module = importlib.import_module(module_name)
        builder = getattr(module, builder_name, None)
        assert callable(builder), f"{name}: {module_name}.{builder_name}"


def test_every_builder_takes_the_application_first():
    """The shell passes the QApplication to every builder without knowing which
    ones need it, so a builder whose signature does not start with one is a
    TypeError the first time that window is opened."""
    import inspect

    from shared.session import _BUILDERS

    for name, (module_name, builder_name) in _BUILDERS.items():
        import importlib
        builder = getattr(importlib.import_module(module_name), builder_name)
        parameters = list(inspect.signature(builder).parameters)
        assert parameters and parameters[0] == "app", (
            f"{name}: {module_name}.{builder_name}{tuple(parameters)}")


def test_the_shell_names_the_windows_it_accepts():
    """An unknown window is a programming error, and the message should say what
    the options are rather than just that the name was wrong."""
    from shared.session import Shell, WINDOW_NAMES

    with pytest.raises(ValueError) as error:
        Shell.open(None, "not-a-window")

    for name in WINDOW_NAMES:
        assert name in str(error.value)


# ---------------------------------------------------------------------------
# No console windows
# ---------------------------------------------------------------------------

#: Modules that ship in the app and so may start a child process. core.py is
#: excluded on purpose: nothing imports it, PyInstaller never sees it, and it is
#: kept only as history. The gap this leaves is that wiring core.py back into a
#: window would reintroduce the pop-ups without failing here. experiments/ is
#: excluded for the same reason prototypes/ is: it is never bundled, so the rule
#: it would be measured against does not apply to it.
NOT_SHIPPED = ("core.py",)

EXCLUDED_FOLDERS = ("tests", "prototypes", "experiments", "packaging", "docs",
                    ".github")


def _app_modules():
    """Every .py file the app ships, as root-relative posix paths."""
    found = []
    for directory, subfolders, files in os.walk(PROJECT_ROOT):
        relative = os.path.relpath(directory, PROJECT_ROOT)
        subfolders[:] = [
            name for name in subfolders
            if not name.startswith(".") and name not in EXCLUDED_FOLDERS
        ]
        for name in sorted(files):
            if not name.endswith(".py") or name in NOT_SHIPPED:
                continue
            found.append(os.path.join(relative, name).replace(os.sep, "/").lstrip("./"))
    return sorted(found)


def _spawn_calls(path):
    """(imports the helper, [(line, call)]) for `path`.

    Whether the name is imported is part of the same question as whether it is
    called: a call without the import is a NameError the first time that ffmpeg
    runs, which in the shipped build is a user pressing Export.
    """
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)

    imports_helper = any(
        (
            isinstance(node, ast.ImportFrom)
            and any(alias.name == "no_console_kwargs" for alias in node.names)
        ) or (
            isinstance(node, ast.Import)
            and any(alias.name == "no_console_kwargs" for alias in node.names)
        )
        for node in ast.walk(tree)
    )

    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if (
            isinstance(function, ast.Attribute)
            and function.attr in ("run", "Popen")
            and isinstance(function.value, ast.Name)
            and function.value.id == "subprocess"
        ):
            calls.append((node.lineno, node))
    return imports_helper, calls


def _suppresses_the_console(call):
    """Whether the call carries the flag, by whatever spelling it uses."""
    for keyword in call.keywords:
        if keyword.arg == "creationflags":
            return True
        unpacked = keyword.value
        if (
            keyword.arg is None
            and isinstance(unpacked, ast.Call)
            and isinstance(unpacked.func, ast.Name)
            and unpacked.func.id == "no_console_kwargs"
        ):
            return True
    return False


def test_a_windows_child_is_told_not_to_open_a_console(monkeypatch):
    """A console program started by a console-less parent is given a new,
    *visible* console window. commcut.exe is GUI-subsystem and has no console,
    so this is what every ffmpeg call in the shipped build needs."""
    monkeypatch.setattr(environment.platform, "system", lambda: "Windows")

    assert no_console_kwargs() == {
        "creationflags": environment.subprocess.CREATE_NO_WINDOW}


def test_a_platform_without_console_windows_gets_no_flags(monkeypatch):
    """macOS and Linux have no equivalent and no problem: the flag does not
    exist there, and passing anything would be a TypeError."""
    for system in ("Darwin", "Linux"):
        monkeypatch.setattr(environment.platform, "system", lambda: system)

        assert no_console_kwargs() == {}, system


def test_each_console_call_gets_its_own_dict_to_merge_into(monkeypatch):
    """Callers splat this into their own keyword arguments, so a shared module
    level dict could be mutated by one call site and change another's."""
    monkeypatch.setattr(environment.platform, "system", lambda: "Windows")

    first = no_console_kwargs()
    first["creationflags"] = "clobbered"

    assert no_console_kwargs()["creationflags"] == environment.subprocess.CREATE_NO_WINDOW


def test_every_app_spawn_site_suppresses_the_console():
    """The regression guard. A new ffmpeg call that forgets the flag is
    invisible from a source run -- the developer has a terminal, the child joins
    it, and no window ever appears -- so the only place it can be caught is
    here."""
    unguarded = []
    for relative in _app_modules():
        imports_helper, calls = _spawn_calls(os.path.join(PROJECT_ROOT, relative))
        for lineno, call in calls:
            if not _suppresses_the_console(call) or not imports_helper:
                unguarded.append(f"{relative}:{lineno}")

    assert not unguarded, (
        "these spawn a child without no_console_kwargs() imported and splatted "
        f"in, so a packaged build pops a console window for each: "
        f"{', '.join(unguarded)}"
    )


def test_the_console_scan_actually_finds_the_app_spawn_sites():
    """A sweep that finds nothing would pass the test above for the wrong
    reason -- a typo in the pattern, or a module that stopped importing
    subprocess, and the guard would be worth nothing."""
    found = {
        relative
        for relative in _app_modules()
        if _spawn_calls(os.path.join(PROJECT_ROOT, relative))[1]
    }

    assert "shared/ffmpeg.py" in found
    assert "shared/mpv.py" in found
    assert "shared/segments.py" in found
    assert "scanner/scanner.py" in found
    # mainwindow.py used to be here too, launching the other windows as child
    # processes; picker/picker.py launched the scanner the same way. Both are
    # gone or converted, so neither may spawn: a subprocess in a window would
    # mean the process model is not actually gone. The check names the windows
    # that exist rather than the one that was deleted, so that adding a window
    # without thinking about it is a visible gap rather than a silent pass.
    assert "mainwindow.py" not in found
    assert "settings/settings.py" not in found
    assert "editor/editor.py" not in found


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
# Startup
# ---------------------------------------------------------------------------

def _start(monkeypatch, argv, menu_return=0):
    """Run main.main() with the filesystem, the app and the loop stubbed out.

    The real main() creates the app folders and, on failure, pops a modal
    dialog. Neither belongs in a test, and neither belongs in the assertion.
    """
    import main
    from shared import diagnostics

    monkeypatch.setattr(main, "ensure_app_folders", lambda: [])
    seen = []
    monkeypatch.setattr(
        main, "run_main_menu",
        lambda given: seen.append(list(given)) or menu_return)
    reported = []
    monkeypatch.setattr(
        diagnostics, "fatal",
        lambda title, text: reported.append((title, text)))
    return main.main(argv), seen, reported


def test_argv_reaches_the_application_and_nothing_else(monkeypatch):
    """There is one entry point now. argv goes to the QApplication, which is
    the only part of it still read — it used to be the dispatcher that sent
    `--window <name>` to another process."""
    code, seen, reported = _start(monkeypatch, ["commcut.exe", "leftover"])

    assert code == 0
    assert seen == [["commcut.exe", "leftover"]]
    assert not reported


def test_a_window_flag_is_no_longer_special(monkeypatch):
    """`commcut --window scanner` must not try to open the scanner, and must not
    complain either. The flag used to be the whole mechanism for handing a
    source video between windows; if anything still routes on it, that is a
    ghost of the process model rather than a feature."""
    code, seen, reported = _start(
        monkeypatch, ["commcut.exe", "--window", "scanner"])

    assert code == 0
    assert not reported
    assert seen == [["commcut.exe", "--window", "scanner"]]


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
    opened = []
    monkeypatch.setattr(main, "run_main_menu", lambda argv: opened.append(1))

    code = main.main(["commcut.exe"])

    assert code == 1
    assert reported
    assert "access denied" in reported[0][1]
    assert not opened


# ---------------------------------------------------------------------------
# Bundled resources
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("folder,name", PAYLOAD_FILES)
def test_bundled_resource_resolves_unfrozen(folder, name):
    """Unfrozen, resource_path must land on the file in the source tree."""
    path = resource_path(folder, name) if folder else resource_path(name)

    assert os.path.isfile(path), path


def test_source_and_payload_layouts_agree():
    """The subfolder each payload file sits in, in the source tree, is the one the
    spec must mirror. Read the spec's datas list and compare, so a file that is
    moved cannot be silently mis-bundled."""
    spec_path = os.path.join(PROJECT_ROOT, "packaging", "commcut.spec")
    with open(spec_path, encoding="utf-8") as handle:
        spec_text = handle.read()

    for folder, name in PAYLOAD_FILES:
        # Forward slashes regardless of host: PyInstaller datas entries are
        # always written that way, including the separator inside the path.
        source = f"{folder}/{name}" if folder else name
        destination = folder if folder else "."
        assert f"('{source}', '{destination}')" in spec_text, source


def test_every_ui_file_in_the_tree_is_listed_and_bundled():
    """The reverse direction, which is the one that bites.

    A new window's .ui that is written and loaded in code but never added to
    the spec works perfectly from source and fails only in a packaged build,
    where the payload has no copy of it. Iterating UI_FILES above cannot catch
    that -- it only checks the files the list already knows about -- so walk
    the tree instead and require every .ui to be listed.
    """
    spec_path = os.path.join(PROJECT_ROOT, "packaging", "commcut.spec")
    with open(spec_path, encoding="utf-8") as handle:
        spec_text = handle.read()

    skip = {"prototypes", "experiments", "packaging", "dist", "__pycache__",
            ".git"}

    def pruned(dirnames):
        # Hidden folders (agent worktrees, VCS internals) hold copies of this
        # tree and are not part of the shipped app.
        return [d for d in dirnames if d not in skip and not d.startswith(".")]

    for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT):
        dirnames[:] = pruned(dirnames)
        for name in filenames:
            if not name.endswith(".ui"):
                continue
            relative = os.path.relpath(
                os.path.join(dirpath, name), PROJECT_ROOT)
            # UI_FILES pairs are (subfolder, file name), with "" for the root.
            folder = os.path.dirname(relative).replace(os.sep, "/")
            assert (folder, name) in UI_FILES, f"{relative} is not in UI_FILES"
            source = f"{folder}/{name}" if folder else name
            assert f"('{source}', '{folder or '.'}')" in spec_text, relative


# ---------------------------------------------------------------------------
# Bundled modules
# ---------------------------------------------------------------------------

def _spec_hiddenimports():
    """The spec's hiddenimports list, read by parsing the spec.

    Parsed rather than substring-matched, which is what the .ui tests above do
    and which would be wrong here: the claim is membership of a list, and a
    module named in a comment satisfies a substring test without being in it.
    """
    spec_path = os.path.join(PROJECT_ROOT, "packaging", "commcut.spec")
    with open(spec_path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=spec_path)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(t, "id", None) == "hiddenimports"
                   for t in node.targets):
            continue
        return [element.value for element in node.value.elts]
    raise AssertionError(f"{spec_path} assigns no hiddenimports")


def test_every_window_the_shell_can_open_is_bundled():
    """Every module in session._BUILDERS has to be in the spec's hiddenimports.

    This is the guard on the one place the move to one process broke the build.
    The windows used to be Analysis() entry points, so PyInstaller bundled them
    by construction and nothing had to import them. Shell._resolve now loads
    them with importlib.import_module(module_name) on a variable, and
    modulegraph cannot follow that -- its _Visitor implements visit_Import and
    visit_ImportFrom and aliases every other expression node, visit_Call
    included, to a no-op.

    Nothing else in the suite or in build.py would catch a spec that lost them.
    The tests here import the windows straight from the source tree, where
    importlib.import_module works, and build.py checks the .ui files and bin/
    rather than the module set. The result is a build that starts, shows the
    main menu, and then fails to open either of its two buttons.

    This only has force because the names are known to be real:
    test_every_registered_window_resolves_to_a_builder imports each one. A
    rename reflected in the spec but not in _BUILDERS would otherwise satisfy
    this test on a name that bundles nothing.

    The reverse direction -- a hiddenimport left behind by a rename -- is not
    asserted, because a module bundled on the strength of an entry the shell no
    longer uses is dead weight rather than a broken build.
    """
    from shared.session import _BUILDERS

    reachable = {module_name for module_name, _ in _BUILDERS.values()}
    bundled = set(_spec_hiddenimports())
    missing = sorted(reachable - bundled)
    assert not missing, (
        f"{', '.join(missing)} can be opened by the shell but is not in the "
        f"spec's hiddenimports, so a packaged build would not contain it. "
        f"Add it there, or a frozen build starts and then cannot open a window."
    )
