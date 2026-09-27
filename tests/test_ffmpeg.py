"""Tests for plan-based ffmpeg execution and partial-failure reporting."""

import os
import subprocess
from types import SimpleNamespace

import pytest

from shared.exporting import ExportPlan, ExportPlanError, ExportSchemes, PlannedExportClip
from shared.ffmpeg import check_video_encoder, execute_export_plan, export_named_model
from shared.paths import DEFAULT_FOLDER_SCHEME
from shared.segments import SegmentModel


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def ffmpeg_has_the_required_encoder(monkeypatch):
    """Assume a working ffmpeg; the capability probe has its own tests below.

    check_video_encoder is a subprocess inquiry, and the fake Popen here models
    an encode -- a temp file for stderr, a payload written to the output path.
    Letting the probe reach it would test the harness rather than the export.
    """
    monkeypatch.setattr(
        "shared.ffmpeg.check_video_encoder", lambda *args, **kwargs: True)


@pytest.fixture
def source_path(tmp_path):
    path = tmp_path / "source.mp4"
    path.write_bytes(b"source")
    return str(path)


def make_plan(root, clips):
    return ExportPlan(
        export_root=str(root),
        clips=tuple(
            PlannedExportClip(
                segment_index=segment_index,
                start=float(start),
                duration=float(duration),
                relative_components=components,
            )
            for segment_index, start, duration, components in clips
        ),
    )


# ---------------------------------------------------------------------------
# The fake encoder
# ---------------------------------------------------------------------------

class Encoding:
    """What the stand-in encoder does for one clip.

    `poll_count` is how many times the process reports "still running" before
    it settles, which is what lets a test cancel a clip that is genuinely in
    progress rather than one that has already finished. `after` runs once the
    clip's output is written, for the tests that need to interfere mid-clip.
    """

    def __init__(self, returncode=0, stderr="", poll_count=0, after=None):
        self.returncode = returncode
        self.stderr = stderr
        self.poll_count = poll_count
        self.after = after

    def finish(self, command, errors):
        with open(command[-1], "wb") as output_file:
            output_file.write(b"encoded")
        errors.write(self.stderr.encode("utf-8"))
        if self.after is not None:
            self.after(command)
        return self.returncode


class FakeProcess:
    """The subprocess.Popen surface shared.ffmpeg drives, without an encoder."""

    def __init__(self, command, errors, encoding):
        self.command = command
        self.returncode = None
        self.terminated = False
        self.killed = False
        self._errors = errors
        self._encoding = encoding
        self._remaining_polls = encoding.poll_count

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        if self._remaining_polls > 0:
            self._remaining_polls -= 1
            return None
        self.returncode = self._encoding.finish(self.command, self._errors)
        return self.returncode

    def wait(self, timeout=None):
        while self.poll() is None:
            pass
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


def install_fake_ffmpeg(monkeypatch, *encodings):
    """Swap subprocess.Popen for a recording fake and return the processes.

    The last encoding repeats once the list runs out, so a test that only cares
    about the first clip can pass a single one.
    """
    queue = list(encodings) or [Encoding()]
    processes = []

    def fake_popen(command, stdout=None, stderr=None):
        encoding = queue.pop(0) if len(queue) > 1 else queue[0]
        process = FakeProcess(command, stderr, encoding)
        processes.append(process)
        return process

    monkeypatch.setattr("shared.ffmpeg.subprocess.Popen", fake_popen)
    # A clip that is cancelled mid-encode polls in a tight loop; the real
    # 50 ms interval is a wall-clock detail, not something to wait on.
    monkeypatch.setattr("shared.ffmpeg._CANCEL_POLL_SECONDS", 0)
    return processes


# ---------------------------------------------------------------------------
# Successful plan execution
# ---------------------------------------------------------------------------

def test_execute_export_plan_creates_nested_mp4(
    tmp_path,
    source_path,
    monkeypatch,
):
    root = tmp_path / "library"
    plan = make_plan(
        root,
        [(0, 0.0, 4.5, ("Network", "Promo", "2000s", "Clip.mp4"))],
    )
    processes = install_fake_ffmpeg(monkeypatch)

    result = execute_export_plan(source_path, plan, ffmpeg_path="ffmpeg")

    destination = root / "Network" / "Promo" / "2000s" / "Clip.mp4"
    assert result.succeeded == 1
    assert result.failed == 0
    assert result.written_paths == (str(destination),)
    assert result.written_relative_paths == ("Network/Promo/2000s/Clip.mp4",)
    assert destination.read_bytes() == b"encoded"
    command = processes[0].command
    assert command[1:5] == ["-y", "-ss", "0.0", "-i"]
    assert command[-1].startswith(str(root))
    assert os.path.basename(command[-1]).startswith(".commcut-export-")


def test_export_named_model_plans_and_executes_in_one_call(
    tmp_path,
    source_path,
    monkeypatch,
):
    root = tmp_path / "library"
    model = SegmentModel(
        duration=2.0,
        segments=[{
            "start": 0.0,
            "ignored": False,
            "tags": {
                "title": "Named Clip",
                "network": "Network",
                "filler_type": "Promo",
                "time_period": "2000s",
            },
        }],
    )
    schemes = ExportSchemes(
        file_scheme="{title}",
        folder_scheme=DEFAULT_FOLDER_SCHEME,
    )
    install_fake_ffmpeg(monkeypatch)

    plan, result = export_named_model(
        source_path,
        model,
        schemes,
        str(root),
        ffmpeg_path="ffmpeg",
    )

    assert len(plan.clips) == 1
    assert result.written_paths == (
        str(root / "Network" / "Promo" / "2000s" / "Named Clip.mp4"),
    )


# ---------------------------------------------------------------------------
# Preflight and failure behavior
# ---------------------------------------------------------------------------

def test_execute_export_plan_refuses_existing_destination(
    tmp_path,
    source_path,
    monkeypatch,
):
    root = tmp_path / "library"
    destination = root / "Network" / "Clip.mp4"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"existing")
    plan = make_plan(root, [(0, 0.0, 2.0, ("Network", "Clip.mp4"))])
    processes = install_fake_ffmpeg(monkeypatch)

    with pytest.raises(ExportPlanError, match="already exists"):
        execute_export_plan(source_path, plan, ffmpeg_path="ffmpeg")
    assert not processes


def test_execute_export_plan_reports_partial_failures_and_cleans_temp_files(
    tmp_path,
    source_path,
    monkeypatch,
):
    root = tmp_path / "library"
    plan = make_plan(
        root,
        [
            (0, 0.0, 2.0, ("Network", "First.mp4")),
            (1, 2.0, 2.0, ("Network", "Second.mp4")),
        ],
    )
    install_fake_ffmpeg(
        monkeypatch,
        Encoding(),
        Encoding(returncode=1, stderr="encoder failed"),
    )

    result = execute_export_plan(source_path, plan, ffmpeg_path="ffmpeg")

    assert result.succeeded == 1
    assert result.failed == 1
    assert result.failures[0].segment_index == 1
    assert result.failures[0].message == "encoder failed"
    assert (root / "Network" / "First.mp4").exists()
    assert not (root / "Network" / "Second.mp4").exists()
    assert not list((root / "Network").glob(".commcut-export-*.mp4"))


def test_execute_export_plan_never_overwrites_racing_destination(
    tmp_path,
    source_path,
    monkeypatch,
):
    root = tmp_path / "library"
    plan = make_plan(root, [(0, 0.0, 2.0, ("Network", "Clip.mp4"))])
    destination = root / "Network" / "Clip.mp4"

    def race(command):
        with open(destination, "wb") as raced_destination:
            raced_destination.write(b"other export")

    install_fake_ffmpeg(monkeypatch, Encoding(after=race))

    result = execute_export_plan(source_path, plan, ffmpeg_path="ffmpeg")

    assert result.succeeded == 0
    assert result.failed == 1
    assert destination.read_bytes() == b"other export"
    assert not list((root / "Network").glob(".commcut-export-*.mp4"))


def test_execute_export_plan_rejects_missing_source(tmp_path):
    plan = make_plan(
        tmp_path / "library",
        [(0, 0.0, 1.0, ("Network", "Clip.mp4"))],
    )

    with pytest.raises(FileNotFoundError, match="Source video"):
        execute_export_plan(str(tmp_path / "missing.mp4"), plan, ffmpeg_path="ffmpeg")


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------

def test_progress_reports_each_clip_and_closes_the_batch(
    tmp_path,
    source_path,
    monkeypatch,
):
    """The bar is driven entirely by these calls: one per clip, plus a
    completion tick. A progress bar that only ticks per batch would sit
    through every clip of a long export looking frozen."""
    root = tmp_path / "library"
    plan = make_plan(
        root,
        [
            (0, 0.0, 2.0, ("Network", "First.mp4")),
            (1, 2.0, 2.0, ("Network", "Second.mp4")),
        ],
    )
    install_fake_ffmpeg(monkeypatch)
    seen = []

    execute_export_plan(
        source_path,
        plan,
        ffmpeg_path="ffmpeg",
        on_progress=lambda done, total, current: seen.append((done, total, current)),
    )

    assert seen == [
        (0, 2, "Network/First.mp4"),
        (1, 2, "Network/Second.mp4"),
        (2, 2, ""),
    ]


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

def test_cancel_before_the_first_clip_runs_nothing(
    tmp_path,
    source_path,
    monkeypatch,
):
    root = tmp_path / "library"
    plan = make_plan(
        root,
        [
            (0, 0.0, 2.0, ("Network", "First.mp4")),
            (1, 2.0, 2.0, ("Network", "Second.mp4")),
        ],
    )
    processes = install_fake_ffmpeg(monkeypatch)
    seen = []

    result = execute_export_plan(
        source_path,
        plan,
        ffmpeg_path="ffmpeg",
        on_progress=lambda done, total, current: seen.append((done, total, current)),
        should_cancel=lambda: True,
    )

    assert processes == []
    assert result.cancelled is True
    assert result.succeeded == 0
    assert result.failures == ()
    # A batch that never ran must not claim a full bar on its way out.
    assert seen == []


def test_cancel_mid_clip_terminates_it_and_is_not_a_failure(
    tmp_path,
    source_path,
    monkeypatch,
):
    """A clip the user deliberately stopped is not an error, and must leave
    nothing behind: no destination, no temporary file."""
    root = tmp_path / "library"
    plan = make_plan(
        root,
        [
            (0, 0.0, 2.0, ("Network", "First.mp4")),
            (1, 2.0, 2.0, ("Network", "Second.mp4")),
        ],
    )
    processes = install_fake_ffmpeg(
        monkeypatch,
        Encoding(poll_count=5),
        Encoding(),
    )
    calls = []

    def should_cancel():
        calls.append(1)
        # Let the pre-clip check pass so the cancel lands inside the encode.
        return len(calls) > 1

    result = execute_export_plan(
        source_path,
        plan,
        ffmpeg_path="ffmpeg",
        should_cancel=should_cancel,
    )

    assert result.cancelled is True
    assert len(processes) == 1
    assert processes[0].terminated is True
    assert result.failures == ()
    assert result.written_paths == ()
    assert not list((root / "Network").glob("*.mp4"))
    assert not list((root / "Network").glob(".commcut-export-*.mp4"))


def test_cancel_keeps_the_clips_that_were_already_written(
    tmp_path,
    source_path,
    monkeypatch,
):
    """The resume path depends on this: a cancelled run reports exactly what
    it committed, so the next run knows what to skip."""
    root = tmp_path / "library"
    plan = make_plan(
        root,
        [
            (0, 0.0, 2.0, ("Network", "First.mp4")),
            (1, 2.0, 2.0, ("Network", "Second.mp4")),
        ],
    )
    install_fake_ffmpeg(monkeypatch, Encoding(), Encoding(poll_count=5))
    calls = []

    result = execute_export_plan(
        source_path,
        plan,
        ffmpeg_path="ffmpeg",
        should_cancel=lambda: (calls.append(1), len(calls) > 1)[1],
    )

    assert result.cancelled is True
    assert result.written_relative_paths == ("Network/First.mp4",)
    assert (root / "Network" / "First.mp4").exists()
    assert not (root / "Network" / "Second.mp4").exists()


# ---------------------------------------------------------------------------
# Committing to a filesystem with no hardlinks
# ---------------------------------------------------------------------------

@pytest.fixture
def no_hardlinks(monkeypatch):
    """Simulate exFAT, a FUSE mount, or a network share: no os.link."""
    def refuse(*args, **kwargs):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(os, "link", refuse)


def test_export_lands_when_the_filesystem_has_no_hardlinks(
    tmp_path, source_path, monkeypatch, no_hardlinks
):
    """A clip library on a USB stick is the ordinary case for this.

    Refusing the export because the filesystem has no hardlinks would make the
    whole pipeline depend on a filesystem feature removable media does not
    have, so the commit has to degrade rather than stop.
    """
    root = tmp_path / "library"
    plan = make_plan(root, [(0, 0.0, 2.0, ("Network", "First.mp4"))])
    install_fake_ffmpeg(monkeypatch, Encoding())

    result = execute_export_plan(
        source_path, plan, ffmpeg_path="ffmpeg")

    destination = root / "Network" / "First.mp4"
    assert result.written_relative_paths == ("Network/First.mp4",)
    assert destination.read_bytes() == b"encoded"
    assert not list((root / "Network").glob(".commcut-export-*.mp4"))


def test_the_copy_fallback_still_never_clobbers(
    tmp_path, source_path, monkeypatch, no_hardlinks
):
    """The no-clobber guarantee is the point of the commit, not an optimization.

    The preflight already refuses a destination that exists, so the interesting
    case is the same one the hardlink path has to survive: something creates the
    destination while the clip is encoding. os.link cannot be the thing that
    enforces this on a filesystem that has none, so O_EXCL has to.
    """
    root = tmp_path / "library"
    destination = root / "Network" / "Clip.mp4"
    plan = make_plan(root, [(0, 0.0, 2.0, ("Network", "Clip.mp4"))])

    def race(command):
        with open(destination, "wb") as raced:
            raced.write(b"other export")

    install_fake_ffmpeg(monkeypatch, Encoding(after=race))

    result = execute_export_plan(source_path, plan, ffmpeg_path="ffmpeg")

    assert result.succeeded == 0
    assert result.failed == 1
    assert destination.read_bytes() == b"other export"
    assert not list(destination.parent.glob(".commcut-export-*.mp4"))


def test_a_failed_copy_leaves_nothing_behind(
    tmp_path, source_path, monkeypatch, no_hardlinks
):
    """A half-copied destination is worse than no destination: the export
    would look complete and the next run would skip it as already written."""
    root = tmp_path / "library"
    plan = make_plan(root, [(0, 0.0, 2.0, ("Network", "First.mp4"))])
    install_fake_ffmpeg(monkeypatch, Encoding())

    def fail_mid_copy(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("shared.ffmpeg.shutil.copyfileobj", fail_mid_copy)

    result = execute_export_plan(source_path, plan, ffmpeg_path="ffmpeg")

    assert not (root / "Network" / "First.mp4").exists()
    assert result.written_paths == ()
    assert result.failures, result
    assert not list((root / "Network").glob(".commcut-export-*.mp4"))


def test_the_export_directory_follows_the_install_root(tmp_path, monkeypatch):
    """Not a project root derived from __file__: frozen, that is PyInstaller's
    extraction folder, which is deleted on exit along with every clip in it."""
    from shared.ffmpeg import _export_dir

    monkeypatch.setattr("shared.ffmpeg.install_root", lambda: str(tmp_path))

    assert _export_dir() == os.path.join(str(tmp_path), "export")


# ---------------------------------------------------------------------------
# The encoder commcut hardcodes
# ---------------------------------------------------------------------------

class _Probe:
    """Stand-in for the `ffmpeg -encoders` inquiry."""

    def __init__(self, stdout, raises=None):
        self.stdout = stdout
        self.raises = raises
        self.calls = 0

    def __call__(self, command, **kwargs):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(returncode=0, stdout=self.stdout, stderr="")


@pytest.fixture
def fresh_encoder_probe(monkeypatch):
    """The answer is cached per process, so no test inherits another's."""
    monkeypatch.setattr("shared.ffmpeg._video_encoder_available", False)


def test_an_ffmpeg_with_the_encoder_is_accepted(monkeypatch, fresh_encoder_probe):
    probe = _Probe(" V....D libx264   H.264 encoder")
    monkeypatch.setattr("shared.ffmpeg.subprocess.run", probe)

    assert check_video_encoder("ffmpeg") is True
    assert probe.calls == 1


def test_a_missing_encoder_is_reported_not_raised(
    monkeypatch, fresh_encoder_probe
):
    """Homebrew's ffmpeg has libx264; plenty of minimal builds do not, and on
    those only export fails -- so the answer has to be a plain False that the
    caller turns into a named problem."""
    monkeypatch.setattr(
        "shared.ffmpeg.subprocess.run", _Probe(" V....D mpeg4 MPEG-4 part 2"))

    assert check_video_encoder("ffmpeg") is False


def test_a_probe_that_cannot_run_is_not_treated_as_a_missing_encoder(
    monkeypatch, fresh_encoder_probe
):
    """Refusing every export because an inquiry failed would be worse than
    letting ffmpeg have its say."""
    monkeypatch.setattr(
        "shared.ffmpeg.subprocess.run",
        _Probe("", raises=OSError("no such file")))

    assert check_video_encoder("ffmpeg") is False


def test_a_confirmed_encoder_is_not_probed_again(
    monkeypatch, fresh_encoder_probe
):
    probe = _Probe(" V....D libx264   H.264 encoder")
    monkeypatch.setattr("shared.ffmpeg.subprocess.run", probe)

    assert check_video_encoder("ffmpeg") is True
    assert check_video_encoder("ffmpeg") is True
    assert probe.calls == 1


def test_export_refuses_before_writing_anything_when_the_encoder_is_missing(
    tmp_path, source_path, monkeypatch
):
    """One named problem, up front -- rather than one raw "Unknown encoder"
    failure per clip after the batch has already started."""
    monkeypatch.setattr("shared.ffmpeg.check_video_encoder", lambda *a: False)
    root = tmp_path / "library"
    plan = make_plan(
        root,
        [(0, 0.0, 2.0, ("Network", "First.mp4")),
         (1, 2.0, 2.0, ("Network", "Second.mp4"))],
    )
    install_fake_ffmpeg(monkeypatch, Encoding())

    with pytest.raises(RuntimeError) as error:
        execute_export_plan(source_path, plan, ffmpeg_path="ffmpeg")

    assert "libx264" in str(error.value)
    assert not root.exists()


# ---------------------------------------------------------------------------
# The scanner's preview clip
# ---------------------------------------------------------------------------

def _fake_ffmpeg(monkeypatch, tmp_path):
    """Stub get_binary_path + subprocess so clip_to_temp runs without ffmpeg."""
    commands = []
    monkeypatch.setattr(
        "shared.ffmpeg.get_binary_path", lambda name: "ffmpeg")

    def fake_run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("shared.ffmpeg.subprocess.run", fake_run)
    return commands


def test_preview_clip_is_named_after_the_source(tmp_path, monkeypatch):
    """Two sources must not share one preview clip, so the name follows the
    file the user picked rather than a fixed test.mp4."""
    from shared.ffmpeg import clip_to_temp

    _fake_ffmpeg(monkeypatch, tmp_path)
    output_dir = str(tmp_path / "temp")

    result = clip_to_temp(str(tmp_path / "import" / "Saturday Morning.mkv"),
                          120, output_dir=output_dir)

    assert os.path.basename(result) == "Saturday Morning_clip120s.mp4"


def test_two_sources_get_different_preview_clips(tmp_path, monkeypatch):
    from shared.ffmpeg import clip_to_temp

    _fake_ffmpeg(monkeypatch, tmp_path)
    output_dir = str(tmp_path / "temp")

    first = clip_to_temp(str(tmp_path / "one.mp4"), 120, output_dir=output_dir)
    second = clip_to_temp(str(tmp_path / "two.mp4"), 120, output_dir=output_dir)

    assert first != second
