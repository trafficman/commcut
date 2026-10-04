"""The release archive: what goes in it, and what refuses to be built.

`packaging/build.py` cannot be imported by name. `packaging/` has no
`__init__.py`, so it is only a namespace portion, and the `packaging` that
PyInstaller depends on is a real package elsewhere on the path -- a regular
package beats a namespace portion no matter which comes first on `sys.path`.
`import packaging.build` therefore resolves the dependency and then reports no
`build` inside it. This module loads the file by path instead, which is the
only route to it and is why the same trick is needed here and in
`test_frozen_mode.py`.

PyInstaller is never invoked. What is under test is the packaging logic around
the build -- the archive layout, the entry list, the version rule -- and that
logic is worth testing without a four-minute build behind it. Whether the exe
in the archive actually runs is not something this file can say; it is checked
by hand on a real machine, which `docs/testing.md` says is the only way.
"""

import hashlib
import importlib.util
import os
import zipfile
from pathlib import Path

import pytest

from shared.version import VERSION

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_PATH = os.path.join(PROJECT_ROOT, "packaging", "build.py")


def _load_build():
    """`packaging/build.py` as a module, by path rather than by name."""
    spec = importlib.util.spec_from_file_location("commcut_build", BUILD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = _load_build()


def _write(path, text, newline=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline=newline) as handle:
        handle.write(text)


def _make_portable(root):
    """A dist/commcut-portable/ with everything assemble_portable() would leave.

    The binaries are a few bytes rather than 130 MB: nothing here reads a size,
    and the LFS-pointer guard belongs to `check_binaries`, which has its own
    threshold. Asserting on bytes instead would make this file a slow copy of
    the build.

    The placeholders are written with CRLF because assemble_portable() does, so
    the comparison below is against the bytes that actually ship. A fake that
    differs from the build in the one detail under test proves nothing.
    """
    _write(root / "commcut.exe", "exe")
    for name in ("ffmpeg.exe", "ffprobe.exe", "libmpv-2.dll"):
        _write(root / "bin" / "win" / name, "binary")
    _write(root / "import" / "README.txt", build.IMPORT_PLACEHOLDER, "\r\n")
    _write(root / "export" / "README.txt", build.EXPORT_PLACEHOLDER, "\r\n")
    return root


@pytest.fixture
def portable(tmp_path, monkeypatch):
    """A fake portable folder, and a dist/ to write the archive into."""
    source = _make_portable(tmp_path / "commcut-portable")
    dist = tmp_path / "dist"
    monkeypatch.setattr(build, "PORTABLE_DIR", str(source))
    monkeypatch.setattr(build, "DIST_DIR", str(dist))
    return source, dist


def _archive(version):
    """Zip the fixture's portable folder and return the archive as a Path."""
    return Path(build.zip_portable(version))


# ---------------------------------------------------------------------------
# The version rule
# ---------------------------------------------------------------------------

def test_the_version_constant_is_bare():
    """shared/version.py holds the number, not the tag.

    The `v` is a tag convention and is stripped on the way in, so storing it
    would mean two spellings of the same release and a build that refuses a
    correctly-formed tag. Asserted on the constant because everything else here
    compares against it.
    """
    assert VERSION == VERSION.strip()
    assert not VERSION.lower().startswith("v")


def test_a_tag_and_a_bare_version_name_the_same_release():
    """`v0.1.0` and `0.1.0` must both be accepted.

    The workflow is handed a tag, a person is more likely to type the bare
    version, and treating them as different would refuse one of them.
    """
    assert build.normalize_version(f"v{VERSION}") == VERSION
    assert build.normalize_version(VERSION) == VERSION
    assert build.normalize_version(f"  V{VERSION}  ") == VERSION
    assert build.check_requested_version(f"v{VERSION}") == VERSION
    assert build.check_requested_version(VERSION) == VERSION


def test_a_tag_that_disagrees_with_the_code_is_refused():
    """A mistagged release fails the build instead of becoming the version.

    This is the rule the whole constant exists for. It is checked before
    PyInstaller runs precisely because the alternative is a 400 MB build
    followed by a release page that disagrees with the code in it, and neither
    is obvious to whoever finds it.
    """
    with pytest.raises(build.BuildError) as raised:
        build.check_requested_version("v99.99.99")

    message = str(raised.value)
    assert "99.99.99" in message
    assert VERSION in message


def test_no_version_is_a_local_smoke_test_not_a_refusal():
    """--version is optional.

    A developer zipping a build to try locally should not have to edit the
    source to bump a number they are about to throw away.
    """
    assert build.check_requested_version(None) is None


# ---------------------------------------------------------------------------
# The archive
# ---------------------------------------------------------------------------

def test_the_archive_is_named_for_its_version_and_platform():
    """The filename says what it is, and the no-version form says that too."""
    assert build.archive_name(VERSION) == f"commcut-{VERSION}-windows-x64.zip"
    assert (build.archive_name(None)
            == "commcut-portable-windows-x64.zip")


def test_the_archive_holds_one_top_level_folder(portable):
    """Not a flat tree.

    Extracting a flat archive scatters the app across whatever folder the user
    extracts into, and two releases extracted to the same place share a
    settings.json and an import/ folder. One versioned folder is what makes
    "extract, open, run" a single step and keeps successive releases apart.
    """
    _, dist = portable
    zip_path = _archive(VERSION)
    root = f"commcut-{VERSION}/"

    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()

    assert names, "the archive is empty"
    assert all(name.startswith(root) for name in names), names
    assert {name.split("/")[0] for name in names} == {root.rstrip("/")}


def test_the_archive_carries_everything_the_app_needs(portable):
    """The six files a distributable cannot do without, at the right paths.

    Walks the archive rather than the list: iterating the list only checks the
    entries it already knows about, so an entry that is required and never
    written would pass. `bin/win/` is the one that matters -- it is beside the
    exe because a onefile payload re-extracts per window, and
    `install_root()` resolves against the exe's folder, not the payload.
    """
    _, _ = portable
    zip_path = _archive(VERSION)
    root = f"commcut-{VERSION}/"

    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())

    missing = [e for e in build.REQUIRED_ARCHIVE_ENTRIES
               if f"{root}{e}" not in names]
    assert not missing, f"not in the archive: {missing}"


def test_a_missing_binary_stops_the_archive(portable):
    """A distributable that cannot find ffmpeg is refused, not shipped.

    This is the failure `check_binaries` exists to prevent at the other end,
    and it is worth having at this end too: the archive is what a user
    downloads, and an archive missing its binaries installs cleanly and then
    fails the first time it tries to cut a clip, with no useful error.
    """
    source, dist = portable
    os.remove(source / "bin" / "win" / "ffmpeg.exe")

    with pytest.raises(build.BuildError) as raised:
        build.zip_portable(VERSION)

    assert "bin/win/ffmpeg.exe" in str(raised.value)
    assert not dist.exists(), "a refused archive must not leave a file behind"


def test_the_placeholders_survive_being_zipped(portable):
    """import/ and export/ keep their README, so the folders are not empty.

    An empty folder does not survive being zipped, so a distributable that
    arrives with no import/ looks broken before the user has done anything
    wrong. The placeholder is the whole reason those two folders are written
    rather than created empty.

    The line endings are part of it: the constant holds newlines, and
    assemble_portable() writes it with ``newline='\\r\\n'`` so that Notepad on
    Windows renders it rather than showing one long line. The comparison is
    against the constant with that translation applied, and CRLF is asserted
    separately so the translation cannot quietly stop happening.
    """
    _, _ = portable
    zip_path = _archive(VERSION)
    root = f"commcut-{VERSION}/"

    with zipfile.ZipFile(zip_path) as archive:
        for folder, expected in (("import", build.IMPORT_PLACEHOLDER),
                                 ("export", build.EXPORT_PLACEHOLDER)):
            raw = archive.read(f"{root}{folder}/README.txt").decode("utf-8")
            assert "\r\n" in raw, f"{folder}/README.txt is not CRLF"
            assert raw.replace("\r\n", "\n") == expected


def test_a_sidecar_is_written_next_to_the_archive(portable):
    """A sha256 sidecar in sha256sum's own format.

    The 366 MB of binaries lands at ~147 MB deflated, which is worth checking,
    and the two-space form is what makes
    `sha256sum -c` accept the file unmodified instead of the user having to
    know which of the several conventions this one is.
    """
    _, dist = portable
    zip_path = _archive(VERSION)
    sidecar = dist / f"{zip_path.name}.sha256"

    assert sidecar.is_file()
    line = sidecar.read_text(encoding="utf-8")
    assert line.endswith("\n")
    digest, separator, name = line.rstrip("\n").partition("  ")
    assert separator == "  ", f"not sha256sum's two-space form: {line!r}"
    assert name == zip_path.name

    expected = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    assert digest == expected, "the sidecar does not describe this archive"


def test_the_entries_are_written_in_a_stable_order(portable):
    """Two archives of one tree list their entries the same way.

    The bytes are not reproducible -- PyInstaller embeds a build timestamp --
    so this is not a claim that the same tag produces the same file. It is that
    the *order* is decided by the tree rather than by the filesystem, which
    makes a diff of two archives about their contents.
    """
    source, _ = portable
    first = _archive(VERSION)
    with zipfile.ZipFile(first) as archive:
        before = archive.namelist()

    (source / "commcut.exe").write_text("changed", encoding="utf-8")
    second = _archive(VERSION)
    with zipfile.ZipFile(second) as archive:
        after = archive.namelist()

    assert before == after


# ---------------------------------------------------------------------------
# The post-build payload check
# ---------------------------------------------------------------------------

def _payload_files(root, relative):
    """Write `relative` (a build.PAYLOAD_FILES entry) under `root`."""
    _write(root / Path(relative), "payload")


@pytest.fixture
def payload_roots(tmp_path, monkeypatch):
    """A source tree and a payload root, so the two can be made to disagree.

    `_verify_payload` asks whether a file is in the payload *and* whether it was
    in the source tree the build ran from, which is why it needs two roots
    rather than one. Both are synthetic: the real PROJECT_ROOT is monkeypatched
    so a developer's checkout is never the thing being asserted about.
    """
    source = tmp_path / "source"
    payload = tmp_path / "payload"
    monkeypatch.setattr(build, "PROJECT_ROOT", str(source))
    return source, payload


def test_a_payload_carrying_every_required_file_passes(payload_roots):
    """The base case, with no icon anywhere.

    Both because it is the arrangement the build actually runs in today, and
    because the optional-file rule added below must not have quietly turned the
    icon into a requirement: a build with no artwork is a complete build.
    """
    _source, payload = payload_roots
    for relative in build.PAYLOAD_FILES:
        _payload_files(payload, relative)

    build._verify_payload(str(payload))


def test_a_missing_required_payload_file_stops_the_build(payload_roots):
    """A .ui the payload does not carry is the failure this check is for."""
    _source, payload = payload_roots
    for relative in build.PAYLOAD_FILES:
        if relative == "mainwindow.ui":
            continue
        _payload_files(payload, relative)

    with pytest.raises(build.BuildError):
        build._verify_payload(str(payload))


def test_the_icon_must_be_bundled_when_it_was_in_the_source_tree(payload_roots):
    """Present on disk, absent from the payload: refused.

    The direction that matters, and the one a one-directional assertion about
    "optional files" would miss. It is what a build made between adding the
    artwork to `assets/` and adding it to the spec looks like: the exe runs,
    every window opens, and every window shows Qt's default icon.
    """
    source, payload = payload_roots
    for relative in build.PAYLOAD_FILES:
        _payload_files(payload, relative)
    _payload_files(source, build.OPTIONAL_PAYLOAD_FILES[0])

    with pytest.raises(build.BuildError):
        build._verify_payload(str(payload))


def test_the_icon_must_not_be_claimed_when_it_was_not_in_the_source_tree(
        payload_roots):
    """Absent on disk, present in the payload: also refused.

    The other direction, and the one that keeps the optional list honest. A
    payload file with no source behind it is a stale build artefact, and the
    only reason to check is that "bundled iff present" is a claim about both.
    """
    source, payload = payload_roots
    for relative in build.PAYLOAD_FILES:
        _payload_files(payload, relative)
    _payload_files(payload, build.OPTIONAL_PAYLOAD_FILES[0])

    with pytest.raises(build.BuildError):
        build._verify_payload(str(payload))


def test_the_icon_is_accepted_in_both_places(payload_roots):
    """The arrangement a build of the finished artwork produces."""
    source, payload = payload_roots
    for relative in build.PAYLOAD_FILES:
        _payload_files(payload, relative)
    for relative in build.OPTIONAL_PAYLOAD_FILES:
        _payload_files(payload, relative)
        _payload_files(source, relative)

    build._verify_payload(str(payload))

