"""Basic ffmpeg/ffprobe operations shared by the editor and scanner.

Paths are resolved per-OS via shared.environment.get_binary_path. This is
where core.py's ffmpeg helpers migrate to as core.py is retired.
"""

import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass

from shared.environment import get_binary_path
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


@dataclass(frozen=True)
class ExportExecutionResult:
    written_paths: tuple[str, ...]
    failures: tuple[ExportClipFailure, ...]

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


def _run_planned_clip(
    ffmpeg_path: str,
    source_path: str,
    clip,
    destination: str,
    export_root: str,
    relative_parent: tuple[str, ...],
    crf: int,
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
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            message = (result.stderr or "").strip()
            raise RuntimeError(message or f"ffmpeg exited with code {result.returncode}")
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
) -> ExportExecutionResult:
    """Execute a preflighted named-export plan with structured partial results."""
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
    failures: list[ExportClipFailure] = []

    for clip in plan.clips:
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
                )
            )
        except (OSError, RuntimeError) as error:
            failures.append(
                ExportClipFailure(
                    segment_index=clip.segment_index,
                    destination=clip.relative_path,
                    message=str(error),
                )
            )

    return ExportExecutionResult(
        written_paths=tuple(written),
        failures=tuple(failures),
    )


def export_named_model(
    source_path: str,
    model: SegmentModel,
    schemes: ExportSchemes,
    out_dir: str,
    crf: int = 18,
    ffmpeg_path: str | None = None,
):
    """Plan and execute one complete named export from an in-memory model."""
    plan = plan_export(model, schemes, out_dir)
    result = execute_export_plan(
        source_path,
        plan,
        crf=crf,
        ffmpeg_path=ffmpeg_path,
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
            print(f"FFmpeg export error for segment {out_index} "
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
        print(f"FFmpeg clip error:\n{e.stderr}")
        return None
