"""Basic ffmpeg/ffprobe operations shared by the editor and scanner.

Paths are resolved per-OS via shared.environment.get_binary_path. This is
where core.py's ffmpeg helpers migrate to as core.py is retired.
"""

import os
import subprocess

from shared.environment import get_binary_path
from shared.segments import sidecar_path, SegmentModel


# Project root is the parent of the shared/ package directory.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def _export_dir():
    return os.path.join(_PROJECT_ROOT, "export")


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
