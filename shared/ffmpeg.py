"""Basic ffmpeg/ffprobe operations shared by the editor and scanner.

Paths are resolved per-OS via shared.environment.get_binary_path. This is
where core.py's ffmpeg helpers migrate to as core.py is retired.
"""

import os
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass

from shared.environment import get_binary_path
from shared.diagnostics import log
from shared.exporting import (
    ExportPlan,
    ExportPlanError,
    ExportSchemes,
    plan_export,
    preflight_export_plan,
    validate_export_parent,
)
from shared.segments import sidecar_path, SegmentModel


# Project root is the parent of the shared/ package directory.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def _export_dir():
    return os.path.join(_PROJECT_ROOT, "export")


@dataclass(frozen=True)
class ExportClipFailure:
    segment_index: int
    destination: str
    message: str


class ExportCancelled(RuntimeError):
    """A clip was stopped because the run was cancelled, not because it failed.

    Distinct from a plain RuntimeError so the executor can break out of the
    batch without recording a failure for a clip the user deliberately stopped.
    """


@dataclass(frozen=True)
class ExportExecutionResult:
    written_paths: tuple[str, ...]
    written_relative_paths: tuple[str, ...]
    failures: tuple[ExportClipFailure, ...]
    cancelled: bool

    @property
    def succeeded(self) -> int:
        return len(self.written_paths)

    @property
    def failed(self) -> int:
        return len(self.failures)


def _commit_temporary_output(temporary_path: str, destination: str) -> None:
    try:
        os.link(temporary_path, destination)
        return
    except FileExistsError:
        raise
    except OSError as link_error:
        if os.name != "nt":
            raise OSError(
                "Filesystem does not support atomic no-clobber export commits"
            ) from link_error
        os.rename(temporary_path, destination)


#: How often a running ffmpeg is polled for a cancel request. This is the
#: upper bound on how long a cancel takes to land mid-clip.
_CANCEL_POLL_SECONDS = 0.05


def _run_ffmpeg(command, should_cancel=None) -> tuple[int, str]:
    """Run one ffmpeg command to completion, honoring a cooperative cancel.

    stderr goes to a temporary file rather than a PIPE on purpose: nothing
    drains it while the encode runs, and a full pipe buffer stalls ffmpeg
    dead. It is read back only to report a failure.

    A cancel terminates the encoder instead of waiting it out. On Windows
    terminate is TerminateProcess, so it is immediate rather than graceful and
    needs no grace/kill escalation; the caller discards the uncommitted
    temporary file either way, so a half-written file is never observable.
    """
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=errors,
        )
        try:
            while process.poll() is None:
                if should_cancel is not None and should_cancel():
                    process.terminate()
                    process.wait()
                    raise ExportCancelled()
                time.sleep(_CANCEL_POLL_SECONDS)
            process.wait()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        errors.seek(0)
        return process.returncode, errors.read().decode("utf-8", "replace")


def _run_planned_clip(
    ffmpeg_path: str,
    source_path: str,
    clip,
    destination: str,
    export_root: str,
    relative_parent: tuple[str, ...],
    crf: int,
    should_cancel=None,
) -> str:
    parent = os.path.dirname(destination)
    validate_export_parent(export_root, relative_parent)
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=".commcut-export-",
        suffix=".mp4",
        dir=parent,
    )
    temporary_stat = os.fstat(descriptor)
    os.close(descriptor)
    try:
        temporary_identity = (temporary_stat.st_dev, temporary_stat.st_ino)
        if not stat.S_ISREG(temporary_stat.st_mode):
            raise RuntimeError("Temporary export path is not a regular file")
        command = [
            ffmpeg_path, "-y",
            "-ss", str(clip.start),
            "-i", source_path,
            "-t", str(clip.duration),
            "-c:v", "libx264", "-crf", str(crf), "-preset", "veryfast",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            temporary_path,
        ]
        current_stat = os.stat(temporary_path, follow_symlinks=False)
        if (
            not stat.S_ISREG(current_stat.st_mode)
            or (current_stat.st_dev, current_stat.st_ino) != temporary_identity
        ):
            raise RuntimeError("Temporary export path changed before ffmpeg execution")
        returncode, stderr = _run_ffmpeg(command, should_cancel)
        if returncode != 0:
            message = stderr.strip()
            raise RuntimeError(message or f"ffmpeg exited with code {returncode}")
        validate_export_parent(export_root, relative_parent)
        current_stat = os.stat(temporary_path, follow_symlinks=False)
        if (
            not stat.S_ISREG(current_stat.st_mode)
            or (current_stat.st_dev, current_stat.st_ino) != temporary_identity
        ):
            raise RuntimeError("Temporary export file changed during ffmpeg execution")
        _commit_temporary_output(temporary_path, destination)
        return destination
    finally:
        try:
            validate_export_parent(export_root, relative_parent)
            current_stat = os.stat(temporary_path, follow_symlinks=False)
            if (
                stat.S_ISREG(current_stat.st_mode)
                and (current_stat.st_dev, current_stat.st_ino) == temporary_identity
            ):
                os.remove(temporary_path)
        except (OSError, ExportPlanError):
            pass


def execute_export_plan(
    source_path: str,
    plan: ExportPlan,
    crf: int = 18,
    ffmpeg_path: str | None = None,
    on_progress=None,
    should_cancel=None,
) -> ExportExecutionResult:
    """Execute a preflighted named-export plan with structured partial results.

    `on_progress(clips_done, total, current_relative_path)` is called before
    each clip and once more as (total, total, "") once the batch is done, so a
    caller can drive a clip-count progress bar without polling. It must not
    raise: nothing here is prepared for a progress callback to fail.

    `should_cancel()` is a zero-arg predicate checked before every clip and
    while each one runs. Setting it stops the batch where it stands: the
    current clip is terminated, its uncommitted temporary file is discarded,
    and it is recorded as cancelled rather than failed. Clips already
    committed stay committed.
    """
    if not isinstance(plan, ExportPlan):
        raise TypeError("plan must be an ExportPlan")
    source_path = os.path.abspath(source_path)
    if not os.path.isfile(source_path):
        raise FileNotFoundError(f"Source video does not exist: {source_path}")

    preflight_export_plan(plan)
    if ffmpeg_path is None:
        ffmpeg_path = get_binary_path("ffmpeg")

    parent_directories = {
        os.path.dirname(os.path.join(plan.export_root, *clip.relative_components))
        for clip in plan.clips
    }
    try:
        for directory in parent_directories:
            os.makedirs(directory, exist_ok=True)
    except OSError:
        raise

    preflight_export_plan(plan)
    written: list[str] = []
    written_relative: list[str] = []
    failures: list[ExportClipFailure] = []
    cancelled = False
    total = len(plan.clips)

    for clips_done, clip in enumerate(plan.clips):
        if should_cancel is not None and should_cancel():
            cancelled = True
            break
        if on_progress is not None:
            on_progress(clips_done, total, clip.relative_path)
        validate_export_parent(
            plan.export_root,
            clip.relative_components[:-1],
        )
        destination = os.path.join(plan.export_root, *clip.relative_components)
        try:
            written.append(
                _run_planned_clip(
                    ffmpeg_path,
                    source_path,
                    clip,
                    destination,
                    plan.export_root,
                    clip.relative_components[:-1],
                    crf,
                    should_cancel=should_cancel,
                )
            )
            written_relative.append(clip.relative_path)
        except ExportCancelled:
            cancelled = True
            break
        except (OSError, RuntimeError) as error:
            failures.append(
                ExportClipFailure(
                    segment_index=clip.segment_index,
                    destination=clip.relative_path,
                    message=str(error),
                )
            )

    # A cancelled batch did not finish, so it does not claim a full bar on its
    # way out.
    if on_progress is not None and not cancelled:
        on_progress(total, total, "")

    return ExportExecutionResult(
        written_paths=tuple(written),
        written_relative_paths=tuple(written_relative),
        failures=tuple(failures),
        cancelled=cancelled,
    )


def export_named_model(
    source_path: str,
    model: SegmentModel,
    schemes: ExportSchemes,
    out_dir: str,
    crf: int = 18,
    ffmpeg_path: str | None = None,
    skip_destinations=(),
    on_progress=None,
    should_cancel=None,
):
    """Plan and execute one complete named export from an in-memory model.

    `skip_destinations` names planned destinations to leave out, which is how a
    cancelled export resumes without re-cutting what it already wrote.
    """
    plan = plan_export(
        model,
        schemes,
        out_dir,
        skip_destinations=skip_destinations,
    )
    result = execute_export_plan(
        source_path,
        plan,
        crf=crf,
        ffmpeg_path=ffmpeg_path,
        on_progress=on_progress,
        should_cancel=should_cancel,
    )
    return plan, result


def export_segment_clips(source_path, out_dir=None, crf=18):
    """Transcode each non-ignored segment of a source video into its own file.

    Reads the .cmct sidecar for `source_path` to recover the segment
    boundaries (start/end times and ignored flags), then re-encodes every
    non-ignored segment to a separate mp4 using frame-accurate seeking plus a
    full libx264 + aac transcode (i.e. NOT a stream copy).

    Outputs land in `out_dir` (default: <project_root>/export) as 1.mp4, 2.mp4,
    ... in segment order; ignored segments are skipped and are not counted in
    the numbering. Returns the list of output paths written (in order).
    """
    source_path = os.path.abspath(source_path)
    ffmpeg_path = get_binary_path("ffmpeg")
    if out_dir is None:
        out_dir = _export_dir()
    os.makedirs(out_dir, exist_ok=True)

    model = SegmentModel.load(sidecar_path(source_path))
    written = []
    out_index = 0
    for i in range(model.segment_count()):
        seg = model.segments[i]
        if seg.get("ignored"):
            continue
        out_index += 1
        start = model.start(i)
        duration = model.end(i) - start
        if duration <= 0:
            continue
        out_path = os.path.join(out_dir, f"{out_index}.mp4")
        # -ss before -i performs an accurate seek (decodes to the exact frame)
        # without needing a full from-start decode, then -t limits the take.
        cmd = [
            ffmpeg_path, "-y",
            "-ss", str(start),
            "-i", source_path,
            "-t", str(duration),
            "-c:v", "libx264", "-crf", str(crf), "-preset", "veryfast",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            out_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log(f"FFmpeg export error for segment {out_index} "
                f"(start={start:.3f}s dur={duration:.3f}s):\n{result.stderr}")
            continue
        written.append(out_path)
    return written


def clip_to_temp(input_path, duration, output_dir="temp"):
    """Copy the first `duration` seconds of a video into the temp folder.

    Uses a stream copy (-c copy) so it's fast and lossless. Returns the
    output path, or None on failure.
    """
    ffmpeg_path = get_binary_path("ffmpeg")

    os.makedirs(output_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(input_path))[0]
    output_path = os.path.join(output_dir, f"{base}_clip{int(duration)}s.mp4")

    cmd = [
        ffmpeg_path,
        "-y",
        "-i", input_path,
        "-t", str(duration),
        "-c", "copy",
        output_path,
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return output_path
    except subprocess.CalledProcessError as e:
        log(f"FFmpeg clip error for {input_path}:\n{e.stderr}")
        return None
