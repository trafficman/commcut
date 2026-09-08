"""Basic ffmpeg/ffprobe operations shared by the editor and scanner.

Paths are resolved per-OS via shared.environment.get_binary_path. This is
where core.py's ffmpeg helpers migrate to as core.py is retired.
"""

import os
import subprocess

from shared.environment import get_binary_path


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
