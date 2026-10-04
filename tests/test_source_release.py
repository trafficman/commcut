"""The source release: what is in it, what cannot be, and what it runs.

`packaging/source_release.py` cannot be imported by name, for the reason
`tests/test_release_build.py` gives: `packaging/` is a namespace portion and the
real `packaging` package is installed alongside it, so a regular package beats a
namespace portion wherever it appears on `sys.path`. This file loads it by path.

Two things are under test and they are not the same thing. The **manifest** is
the archive's contents, and the interesting property is a refusal: an allow-list
cannot ship a developer's `export/` full of clips, and `test_nothing_the_app_owns_at_runtime_reaches_the_archive`
is the test that would notice if that stopped being true. The **launchers** are
three shell scripts, and the interesting property is that something executes
them -- `tests/test_release_build.py` has the same shape for the Windows build,
where the equivalent unrunnable thing is a four-minute PyInstaller invocation.

What is *not* here, and cannot be: whether commcut plays video on macOS or
Linux. `FakeBridge` stands in for libmpv by design, the launchers are only run
as far as their own logic here, and `docs/source-install.md` says in as many
words that a person on that machine is the only oracle for playback.
"""

import hashlib
import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile

import pytest

from shared.version import VERSION

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_RELEASE_PATH = os.path.join(PROJECT_ROOT, "packaging", "source_release.py")

#: The three files that start commcut. Everything in an archive is either one of
#: these or data.
LAUNCHERS = ("install_deps.sh", "run.sh", "commcut.command")

_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_HEREDOC = re.compile(r"<<'PYTHON_CHECK'[^\n]*\r?\n(.*?)\r?\nPYTHON_CHECK", re.S)


def _load_source_release():
    spec = importlib.util.spec_from_file_location(
        "commcut_source_release", SOURCE_RELEASE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


source_release = _load_source_release()


# ---------------------------------------------------------------------------
# A project root to build from
# ---------------------------------------------------------------------------

@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A copy of the manifest's entries in a scratch folder, as PROJECT_ROOT.

    A copy rather than the repository itself, because the leak test has to *add*
    the very things the manifest must refuse -- a developer's `export/` full of
    clips, a `.venv/`, a `settings.json` -- and doing that to the checkout would
    mean creating and then deleting files in the working tree for every run.
    """
    root = tmp_path / "project"
    root.mkdir()

    for entry in source_release.SOURCE_ENTRIES:
        absolute = os.path.join(PROJECT_ROOT, *entry.split("/"))
        copied = os.path.join(str(root), *entry.split("/"))
        os.makedirs(os.path.dirname(copied), exist_ok=True)
        if os.path.isdir(absolute):
            shutil.copytree(absolute, copied,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(absolute, copied)

    monkeypatch.setattr(source_release, "PROJECT_ROOT", str(root))
    return root


@pytest.fixture
def dist(tmp_path, monkeypatch):
    """Where the archive is written, so nothing lands in the real dist/."""
    folder = tmp_path / "dist"
    folder.mkdir()
    monkeypatch.setattr(source_release, "DIST_DIR", str(folder))
    return folder


def _build(tree, dist, version=None):
    """Run the real build end to end and return (archive path, members)."""
    archive = source_release.build(version=version, target="linux")
    with tarfile.open(archive, "r:gz") as tar:
        return archive, tar.getmembers()


def _payload(members, name):
    """The bytes of one member, addressed by its path inside the folder."""
    wanted = [m for m in members if m.name.endswith("/" + name)
              or m.name == name]
    assert len(wanted) == 1, f"{name}: {[m.name for m in wanted]}"
    return wanted[0]


def _read(members, archive, name):
    with tarfile.open(archive, "r:gz") as tar:
        return tar.extractfile(_payload(members, name)).read()


# ---------------------------------------------------------------------------
# The manifest covers what runs
# ---------------------------------------------------------------------------

def test_every_module_in_the_app_directories_is_in_the_manifest(tree):
    """Walking the tree rather than iterating the manifest.

    The same reason `test_every_ui_file_in_the_tree_is_listed_and_bundled`
    exists: a list checked only against itself cannot notice a file that was
    added to a window's folder and never shipped, and the failure is an archive
    that installs cleanly and then cannot open a window.
    """
    shipped = set(source_release.manifest_files())
    missing = []
    for folder in ("shared", "editor", "scanner", "settings", "importer",
                   "assets"):
        for path, _dirs, files in os.walk(os.path.join(str(tree), folder)):
            for name in files:
                if name.endswith((".py", ".ui", ".png")):
                    relative = os.path.relpath(
                        os.path.join(path, name), str(tree))
                    relative = relative.replace(os.sep, "/")
                    if relative not in shipped:
                        missing.append(relative)

    assert not missing, f"in the tree but not in the archive: {missing}"


def test_the_payload_files_resource_path_asks_for_are_all_present():
    """These are the read-only files `shared/environment.resource_path` is
    called with, which is an expression evaluated at run time rather than an
    import PyInstaller or an archive could follow."""
    shipped = set(source_release.manifest_files())

    for payload_file in source_release.windows_build.PAYLOAD_FILES:
        assert payload_file in shipped, payload_file


def test_every_window_module_the_shell_can_open_is_present():
    """Same claim as `test_every_window_the_shell_can_open_is_bundled` in the
    frozen suite, against the other packaging path."""
    from shared.session import _BUILDERS

    shipped = set(source_release.manifest_files())
    for module_name, _builder in _BUILDERS.values():
        relative = module_name.replace(".", "/") + ".py"
        assert relative in shipped, relative


def test_the_three_launchers_and_the_entry_point_are_present(tree):
    shipped = set(source_release.manifest_files())

    for name in (*LAUNCHERS, "main.py", "requirements.txt"):
        assert name in shipped, name


def test_every_markdown_file_in_the_repository_is_shipped():
    """"All of them" is the rule, and this is what makes it safe.

    `tests/test_docs.py` requires that every relative markdown link in the tree
    resolves to a `.md` file, so including every `.md` file is provably enough
    for no link inside a source release to dangle. That guarantee is worth
    something only as long as the count is checked, which is what this does.
    """
    shipped = set(source_release.manifest_files())
    present = set()
    for folder in (PROJECT_ROOT,):
        for dirpath, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in {
                ".git", ".kilo", "__pycache__", "build", "dist", ".venv",
                ".pytest_cache", "bin", "tests", "import", "export", "temp"}]
            for name in files:
                if name.endswith(".md"):
                    relative = os.path.relpath(os.path.join(dirpath, name),
                                               folder)
                    present.add(relative.replace(os.sep, "/"))

    assert present - shipped == set(), sorted(present - shipped)


def test_no_link_inside_the_archive_dangles(tree, dist):
    """The rule above, checked on the artifact rather than on the rule.

    Cheap insurance against a future document that links to something which is
    not markdown -- `test_docs.py` would catch that in the repository, but the
    archive is a different tree and this is what a reader of the release
    actually gets.
    """
    archive, members = _build(tree, dist)

    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(str(tmp := dist.parent / "extracted"),
                       filter="data")
    root = tmp / source_release.archive_root(None)

    broken = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith(".md"):
                continue
            text = open(os.path.join(dirpath, name), encoding="utf-8").read()
            for raw in _LINK.findall(text):
                target = raw.split("#", 1)[0].strip()
                if not target or "://" in target:
                    continue
                resolved = os.path.normpath(
                    os.path.join(dirpath, target))
                if not os.path.exists(resolved):
                    broken.append(f"{name} -> {target}")

    assert not broken, broken


# ---------------------------------------------------------------------------
# What cannot be in it
# ---------------------------------------------------------------------------

def test_nothing_the_app_owns_at_runtime_reaches_the_archive(tree, dist):
    """The leak test, and the reason the manifest is an allow-list.

    `import/`, `export/` and `temp/` are not in `.gitignore`: for a source
    install the project root *is* the install root, so they sit at the repo
    root holding whatever the developer last exported. A walker that took
    "everything, minus what I remembered to exclude" would put a user's videos
    into a public release. This plants all of it and requires that none of it
    arrives.
    """
    root = str(tree)

    for folder in ("import", "export", "temp", ".venv", "tests",
                   "prototypes", "bin", "build", "dist", ".github",
                   "picker", "experiments"):
        os.makedirs(os.path.join(root, folder), exist_ok=True)
        with open(os.path.join(root, folder, "a-private-clip.mp4"), "wb") as f:
            f.write(b"not yours to publish")

    for name in ("settings.json", "vocabulary.json", "commcut.log", "core.py",
                 "results_post_refactor.json"):
        with open(os.path.join(root, name), "w", encoding="utf-8") as handle:
            handle.write("{}" if name.endswith(".json") else "private")

    # A __pycache__ inside a directory that *is* shipped, which is the case an
    # extension check alone would miss.
    cache = os.path.join(root, "shared", "__pycache__")
    os.makedirs(cache, exist_ok=True)
    with open(os.path.join(cache, "environment.cpython-311.pyc"), "wb") as f:
        f.write(b"\x00\x01")

    archive, members = _build(tree, dist)
    names = [m.name for m in members]

    for fragment in ("import/", "export/", "temp/", ".venv", "tests/",
                     "prototypes", "bin/", "build/", "dist/", ".github",
                     "picker/", "__pycache__", ".pyc", "core.py",
                     "settings.json", "vocabulary.json", "commcut.log",
                     "results_post_refactor", "a-private-clip"):
        assert not any(fragment in name for name in names), fragment

    # And the file that legitimately looks like the thing being hunted for is
    # still there, so this cannot pass by refusing to build anything.
    assert any(name.endswith("/shared/environment.py") for name in names)


def test_only_the_experiment_index_is_shipped_from_experiments(tree):
    """`AGENTS.md` and three documents link into `experiments/README.md`, so it
    ships; the experiment code beside it never does."""
    shipped = [name for name in source_release.manifest_files()
               if name.startswith("experiments/")]

    assert shipped == ["experiments/README.md"]


@pytest.mark.parametrize("refused", [
    "bin", "bin/mac", "tests/test_docs.py", "prototypes/editor.py",
    "import/a.mp4", "export/b.mp4", "temp/clip.mp4", ".venv/bin/python",
    "shared/__pycache__/environment.cpython-311.pyc", "settings.json",
    "vocabulary.json", "commcut.log", "core.py", "build/lib.py",
    "dist/x.tar.gz", ".github/workflows/release.yml", "picker/old.py",
    "a-private.mp4", "a-private.cmct",
])
def test_the_forbidden_list_refuses(refused):
    assert source_release._is_forbidden(refused) is not None


@pytest.mark.parametrize("allowed", [
    "main.py", "shared/environment.py", "docs/README.md",
    "docs/guides/filler_and_you.md", "packaging/README.md",
    "experiments/README.md", "requirements.txt", "assets/commcut_banner.png",
    "editor/editorwindow.ui",
])
def test_the_forbidden_list_does_not_refuse_the_application(allowed):
    assert source_release._is_forbidden(allowed) is None


def test_a_manifest_entry_that_is_missing_is_refused(monkeypatch, tmp_path):
    """An archive missing a module installs cleanly and then fails to start, so
    the builder refuses rather than writing one."""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(source_release, "PROJECT_ROOT", str(empty))

    with pytest.raises(source_release.SourceReleaseError) as error:
        source_release.manifest_files()

    assert "main.py" in str(error.value)


def test_a_widened_manifest_is_caught_by_the_tripwire(monkeypatch, tmp_path):
    """FORBIDDEN_PATHS is unreachable while the manifest is an allow-list. This
    is what happens if somebody adds a folder to it without noticing."""
    root = tmp_path / "project"
    (root / "export").mkdir(parents=True)
    (root / "export" / "clip.mp4").write_bytes(b"x")
    monkeypatch.setattr(source_release, "PROJECT_ROOT", str(root))
    monkeypatch.setattr(source_release, "SOURCE_ENTRIES", ("export",))

    with pytest.raises(source_release.SourceReleaseError) as error:
        source_release.manifest_files()

    assert "forbidden list" in str(error.value)


# ---------------------------------------------------------------------------
# The artifact
# ---------------------------------------------------------------------------

def test_the_archive_holds_one_top_level_folder(tree, dist):
    """Same shape as the Windows zip, for the same reason: extracting must not
    scatter the app, and two releases side by side must not merge their
    settings.json."""
    _archive, members = _build(tree, dist)

    roots = {m.name.split("/")[0] for m in members}
    assert roots == {source_release.archive_root(None)}


def test_the_launchers_are_executable_and_nothing_else_is(tree, dist):
    """tar takes its modes from the TarInfo, not the filesystem.

    A Windows checkout has no execute bit and `core.fileMode` is off there, so a
    mode read from disk would be whatever the builder's umask decided -- and
    tar's own default strips the execute bit, producing an archive whose
    launcher cannot be run. This is the assertion that says so.
    """
    _archive, members = _build(tree, dist)

    for member in members:
        leaf = member.name.split("/")[-1]
        if leaf in LAUNCHERS:
            assert stat.S_IMODE(member.mode) == 0o755, member.name
        elif not member.isdir():
            assert stat.S_IMODE(member.mode) == 0o644, member.name


def test_no_shipped_script_has_a_crlf_line_ending(tree, dist):
    """`\\r: command not found`, on the first line it tries to run.

    `.gitattributes` already forces `eol=lf` on these, so a checkout cannot hold
    CRLF; the builder normalizes anyway, because a release artifact should not
    depend on that being true.
    """
    archive, members = _build(tree, dist)

    for name in LAUNCHERS:
        payload = _read(members, archive, name)
        assert b"\r\n" not in payload, name
        assert payload.startswith(b"#!"), name


def test_two_builds_of_one_tree_are_byte_identical(tree, dist):
    """The Windows build cannot do this -- PyInstaller embeds a build timestamp
    -- and settles for a stable entry order instead. Here mtime, uid, gid and
    the gzip header are all fixed, so the sha256 sidecar means something."""
    first, _ = _build(tree, dist)
    first_bytes = open(first, "rb").read()
    second, _ = _build(tree, dist)

    assert first == second
    assert open(second, "rb").read() == first_bytes


def test_the_sidecar_matches_the_archive(tree, dist):
    archive, _members = _build(tree, dist)

    digest = hashlib.sha256(open(archive, "rb").read()).hexdigest()
    sidecar = open(f"{archive}.sha256", encoding="utf-8").read()

    assert sidecar.split()[0] == digest
    assert os.path.basename(archive) in sidecar


def test_entry_order_is_sorted_and_grouped_by_folder(tree, dist):
    """Two archives of one tree then diff on their contents rather than on
    their ordering, which is what makes the previous assertion readable."""
    _archive, members = _build(tree, dist)
    files = [m.name for m in members if not m.isdir()]

    assert files == sorted(files)
    assert all(name.startswith("commcut-portable/") for name in files)


def test_the_archive_is_named_for_the_platform_it_is_built_for():
    """The contents are platform-independent -- nothing is compiled -- so the
    only thing that varies is the name, and `--target` is how a Windows checkout
    produces one to look at."""
    assert source_release.archive_name(target="macos") == (
        "commcut-portable-source-macos-"
        f"{source_release.artifact_arch()}.tar.gz")
    assert source_release.archive_name(target="linux") == (
        "commcut-portable-source-linux-"
        f"{source_release.artifact_arch()}.tar.gz")


def test_the_two_platform_names_are_the_only_ones():
    with pytest.raises(source_release.SourceReleaseError) as error:
        source_release.artifact_platform(target="windows")

    assert "macos" in str(error.value)


def test_a_version_must_match_shared_version_py():
    """The tag-must-match rule, reused rather than reimplemented.

    Asserting this here is a claim that `source_release.py` reaches
    `build.check_requested_version` and not a copy of it: a mistagged source
    release would otherwise be a page that contradicts the code inside it.
    """
    assert source_release.windows_build.normalize_version(
        f"v{VERSION}") == VERSION

    with pytest.raises(source_release.windows_build.BuildError):
        source_release.windows_build.check_requested_version("v0.0.0-not-real")


# ---------------------------------------------------------------------------
# The launchers
# ---------------------------------------------------------------------------

def _script(name):
    return open(os.path.join(PROJECT_ROOT, name), encoding="utf-8").read()


@pytest.mark.parametrize("name", LAUNCHERS)
def test_every_launcher_moves_to_its_own_folder_first(name):
    """Finder runs a double-clicked `.command` with `$HOME` as its working
    directory. Without this, `main.py` and `requirements.txt` are not found."""
    text = _script(name)

    assert 'CDPATH= cd -- "$(dirname -- "$0")" && pwd' in text
    # Not necessarily `set -e`: commcut.command has to survive run.sh failing,
    # which is the whole reason it exists.
    assert re.search(r"^set -[a-z]+$", text, re.M), name


@pytest.mark.skipif(shutil.which("sh") is None,
                    reason="no POSIX sh on this machine")
def test_every_launcher_parses_as_posix_shell():
    """A syntax error in one of these ships and is found by the first user who
    double-clicks it.

    Skipped wherever there is no `sh` -- a Windows checkout has none -- which is
    also why the macOS and Linux CI jobs that build the source release matter:
    they are the only place this runs, and they are the platforms the scripts
    are for.
    """
    for name in LAUNCHERS:
        result = subprocess.run(["sh", "-n", os.path.join(PROJECT_ROOT, name)],
                                capture_output=True, text=True)
        assert result.returncode == 0, f"{name}: {result.stderr}"


def test_run_sh_execs_the_venv_interpreter():
    """`exec`, not a subshell: commcut should *be* the process, so Ctrl-C
    reaches the app rather than a wrapper holding it open."""
    text = _script("run.sh")

    assert 'exec "$VENV_PYTHON" main.py "$@"' in text


def test_run_sh_refuses_rather_than_falling_back_to_a_system_python():
    """A launcher that quietly used whatever `python3` it found would run the
    app against the wrong PySide6, or none at all."""
    text = _script("run.sh")

    assert "install_deps.sh" in text
    assert "exit 1" in text


def test_the_command_wrapper_waits_so_an_error_stays_readable():
    """It deliberately does not `exec`: an exec replaces this shell, and there
    would be nothing left to print or to wait with."""
    text = _script("commcut.command")

    assert not re.search(r"^\s*exec\s", text, re.M), "commcut.command execs"
    assert re.search(r"^\s*read -r\b", text, re.M)
    assert 'sh ./run.sh "$@"' in text


def test_install_deps_never_asks_for_root():
    """`sudo` from a script a user double-clicked is not a thing this release
    does. It reports what is missing and prints the command instead -- so
    `sudo` is expected to appear in the *prose*, and nowhere a shell could run
    it.
    """
    text = _script("install_deps.sh")

    assert not re.search(r"^sudo\s", text, re.M), "runs sudo as a command"
    assert not re.search(r"[;&|]\s*sudo\s", text), "chains a sudo command"
    # Not vacuous: it does tell the user what to run.
    assert "sudo apt install python3 python3-venv" in text


def test_install_deps_only_runs_a_package_manager_when_asked():
    text = _script("install_deps.sh")

    assert "brew install python ffmpeg mpv" in text
    assert 'if [ "$want_brew" -eq 1 ]; then' in text


def test_install_deps_builds_a_venv_and_installs_the_pins():
    """The venv is not a convenience: PEP 668 makes `pip install` into a
    Homebrew or Debian/Ubuntu system interpreter an error."""
    text = _script("install_deps.sh")

    assert '-m venv "$VENV_DIR"' in text
    assert '--requirement "$REQUIREMENTS"' in text
    assert "python3-venv" in text


def test_the_install_scripts_embedded_check_block_is_valid_python():
    """A shell heredoc full of Python is not compiled by anything, so a syntax
    error in it ships and is found by the first user who runs the installer."""
    block = _HEREDOC.search(_script("install_deps.sh"))
    assert block is not None, "install_deps.sh has no PYTHON_CHECK heredoc"

    compile(block.group(1), "install_deps.sh:PYTHON_CHECK", "exec")


def _check_block(tmp_path, environment_body, ffmpeg_body, target="linux"):
    """Run install_deps.sh's embedded check against a fake `shared` package.

    The block does `sys.path.insert(0, ".")` and imports `shared.environment`,
    so a scratch folder holding a shim is enough to drive every branch without
    installing ffmpeg, libmpv or an encoder on the machine running the suite.
    That is the point: the alternative is a test that only passes on a machine
    that already has all three.
    """
    block = _HEREDOC.search(_script("install_deps.sh")).group(1)
    root = tmp_path / target
    (root / "shared").mkdir(parents=True)
    (root / "shared" / "environment.py").write_text(environment_body,
                                                    encoding="utf-8")
    (root / "shared" / "ffmpeg.py").write_text(ffmpeg_body, encoding="utf-8")
    script = root / "check.py"
    script.write_text(block, encoding="utf-8", newline="\n")

    return subprocess.run([sys.executable, str(script), target],
                          cwd=str(root), capture_output=True, text=True)


_UNRESOLVABLE = (
    "MPV_LIBRARY_ENV_VAR = 'COMMCUT_MPV_LIB'\n"
    "REQUIRED_VIDEO_ENCODER = 'libx264'\n"
    "def get_binary_path(name):\n"
    "    raise FileNotFoundError('looked in /opt/homebrew/bin and /usr/bin')\n"
    "def resolve_mpv_library():\n"
    "    raise FileNotFoundError('looked in /usr/lib/x86_64-linux-gnu')\n"
)
_NO_ENCODER = "def check_video_encoder(path=None):\n    return False\n"


def test_the_check_prints_the_brew_command_on_macos(tmp_path):
    """The two platforms do not install their binaries the same way, and the
    block is the only place that knows which one it is.

    Asserted against the whole ffmpeg+libmpv line, not just "apt install":
    the explanation below it deliberately names both package managers in
    passing, because `apt install mpv` is the command that installs the player
    rather than the library and the user should be warned about it either way.
    """
    result = _check_block(tmp_path, _UNRESOLVABLE, _NO_ENCODER, target="macos")

    assert result.returncode == 1
    assert "[commcut]   brew install ffmpeg mpv" in result.stdout
    assert "apt install ffmpeg libmpv2" not in result.stdout


def test_the_check_prints_the_apt_command_on_linux(tmp_path):
    result = _check_block(tmp_path, _UNRESOLVABLE, _NO_ENCODER, target="linux")

    assert result.returncode == 1
    assert "[commcut]   apt install ffmpeg libmpv2" in result.stdout
    assert "brew install ffmpeg mpv" not in result.stdout


def test_the_check_warns_about_the_player_on_either_platform(tmp_path):
    """`brew install mpv` and `apt install mpv` both install the *player*. This
    is the single most common reason a source install will not start, so it is
    said whichever platform is being installed."""
    for target in ("macos", "linux"):
        result = _check_block(tmp_path, _UNRESOLVABLE, _NO_ENCODER,
                              target=target)
        assert "`brew install mpv` and `apt install mpv` install the mpv" \
            in result.stdout


def test_the_check_reports_everything_present_when_it_is(tmp_path):
    result = _check_block(
        tmp_path,
        "MPV_LIBRARY_ENV_VAR = 'COMMCUT_MPV_LIB'\n"
        "REQUIRED_VIDEO_ENCODER = 'libx264'\n"
        "def get_binary_path(name):\n"
        "    return '/usr/bin/' + name\n"
        "def resolve_mpv_library():\n"
        "    return '/usr/lib/libmpv.so.2'\n",
        "def check_video_encoder(path=None):\n"
        "    return True\n")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "MISSING" not in result.stdout
    for expected in ("ffmpeg", "ffprobe", "libmpv", "libx264"):
        assert expected in result.stdout


def test_the_check_names_the_app_s_own_message_for_what_is_missing(tmp_path):
    """The app's resolvers already name every folder they searched and every
    command that helps. A shell-side reimplementation of that search would be a
    second answer to the same question."""
    result = _check_block(
        tmp_path,
        "MPV_LIBRARY_ENV_VAR = 'COMMCUT_MPV_LIB'\n"
        "REQUIRED_VIDEO_ENCODER = 'libx264'\n"
        "def get_binary_path(name):\n"
        "    raise FileNotFoundError('looked in /opt/homebrew/bin and /usr/bin')\n"
        "def resolve_mpv_library():\n"
        "    raise FileNotFoundError('looked in /usr/lib/x86_64-linux-gnu')\n",
        "def check_video_encoder(path=None):\n"
        "    return False\n")

    assert result.returncode == 1
    assert "looked in /opt/homebrew/bin" in result.stdout
    assert "looked in /usr/lib/x86_64-linux-gnu" in result.stdout
    assert "apt install ffmpeg libmpv2" in result.stdout
    assert "COMMCUT_MPV_LIB" in result.stdout


def test_the_check_skips_the_encoder_probe_when_ffmpeg_is_missing(tmp_path):
    """Reporting a second problem the user does not have sends them to install
    an encoder when the real answer is that ffmpeg is not there."""
    result = _check_block(
        tmp_path,
        "MPV_LIBRARY_ENV_VAR = 'COMMCUT_MPV_LIB'\n"
        "REQUIRED_VIDEO_ENCODER = 'libx264'\n"
        "def get_binary_path(name):\n"
        "    raise FileNotFoundError('no ffmpeg')\n"
        "def resolve_mpv_library():\n"
        "    return '/usr/lib/libmpv.so.2'\n",
        "def check_video_encoder(path=None):\n"
        "    return False\n")

    assert result.returncode == 1
    assert "libx264" not in result.stdout


def test_a_build_without_libx264_says_so(tmp_path):
    """Scanning and preview work without it and *only* export fails, with a raw
    "Unknown encoder" line once per clip. This is the one that names it up
    front."""
    result = _check_block(
        tmp_path,
        "MPV_LIBRARY_ENV_VAR = 'COMMCUT_MPV_LIB'\n"
        "REQUIRED_VIDEO_ENCODER = 'libx264'\n"
        "def get_binary_path(name):\n"
        "    return '/usr/bin/' + name\n"
        "def resolve_mpv_library():\n"
        "    return '/usr/lib/libmpv.so.2'\n",
        "def check_video_encoder(path=None):\n"
        "    return False\n")

    assert result.returncode == 1
    assert "libx264" in result.stdout
    assert "MISSING  libx264" in result.stdout


# ---------------------------------------------------------------------------
# The repository's own rules, restated where they now apply to three more files
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", LAUNCHERS)
def test_the_launchers_are_forced_to_lf_by_gitattributes(name):
    """`.gitattributes` is the guarantee that a checkout cannot hold CRLF, and
    it only applies to a path the file names."""
    attributes = open(os.path.join(PROJECT_ROOT, ".gitattributes"),
                      encoding="utf-8").read()

    assert "*.sh text eol=lf" in attributes
    assert "*.command text eol=lf" in attributes
    assert name.endswith((".sh", ".command"))


@pytest.mark.parametrize("name", LAUNCHERS)
def test_the_launchers_on_disk_have_lf_endings(name):
    """What `.gitattributes` promises a checkout looks like."""
    payload = open(os.path.join(PROJECT_ROOT, name), "rb").read()

    assert b"\r\n" not in payload, name


def test_the_virtual_environment_is_gitignored():
    """`.venv/` is inside `install_root()` for a source install -- the same
    folder settings.json and import/ live in -- so it must not be committable."""
    ignore = open(os.path.join(PROJECT_ROOT, ".gitignore"),
                  encoding="utf-8").read()

    assert ".venv/" in ignore
