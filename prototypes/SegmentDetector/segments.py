"""Segment data model and .cmct sidecar persistence.

A .cmct file is a JSON sidecar stored alongside the source video. It records
the video as a contiguous tiling of segments defined by transition points: each
segment is the span from its own "start" to the next segment's "start" (or the
video duration for the last one). Because segments are derived from a single
ordered list of start points, they can never overlap and can never leave a gap.

Format:
{
  "source": "compilation.mp4",
  "duration": 120.0,
  "segments": [
    {"start": 0.0,   "ignored": false, "tags": {}},
    {"start": 42.5,  "ignored": true,  "tags": {}},
    {"start": 78.2,  "ignored": false, "tags": {}}
  ]
}
"""

import json
import os
import subprocess


def sidecar_path(video_path):
    """Return the .cmct sidecar path for a video (swap the extension)."""
    base, _ = os.path.splitext(video_path)
    return base + ".cmct"


def probe_duration(path):
    """Duration in seconds via ffprobe, or None on failure."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            path,
        ],
        capture_output=True,
        text=True,
    )
    try:
        return float(result.stdout.strip())
    except (ValueError, AttributeError):
        return None


class SegmentModel:
    """Holds the parsed .cmct data and handles persistence."""

    def __init__(self, source=None, duration=0.0, segments=None):
        self.source = source
        self.duration = duration
        self.segments = segments if segments is not None else []

    # --- serialization ---

    def to_dict(self):
        return {
            "source": self.source,
            "duration": self.duration,
            "segments": self.segments,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            source=data.get("source"),
            duration=data.get("duration", 0.0),
            segments=data.get("segments", []),
        )

    def save(self, path):
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load(cls, path):
        with open(path, "r") as f:
            return cls.from_dict(json.load(f))

    # --- factory ---

    @classmethod
    def placeholder(cls, source, duration):
        """Single segment spanning the whole video, not ignored, empty tags."""
        return cls(
            source=source,
            duration=duration,
            segments=[{"start": 0.0, "ignored": False, "tags": {}}],
        )

    # --- derived views ---

    def segment_count(self):
        return len(self.segments)

    def start(self, i):
        return self.segments[i]["start"]

    def end(self, i):
        return self.segments[i + 1]["start"] if i + 1 < len(self.segments) else self.duration

    # --- editing operations ---

    def end_segment(self, active_index, position):
        """Split the active segment at position.

        The active segment (left part) keeps the active index. A new segment
        is inserted to its right, inheriting the active segment's metadata.
        Returns True if a split was made, False if position was at a boundary.
        """
        seg = self.segments[active_index]
        start = seg["start"]
        end = self.end(active_index)
        position = max(start, min(position, end))
        if position <= start or position >= end:
            return False
        new_seg = {
            "start": position,
            "ignored": seg["ignored"],
            "tags": dict(seg["tags"]),
        }
        self.segments.insert(active_index + 1, new_seg)
        return True

    def merge_next(self, active_index):
        """Remove the boundary after the active segment, merging it with the next.

        The active segment absorbs the next one's span. The next segment's
        metadata is discarded. Returns True if a merge happened.
        """
        if active_index + 1 >= len(self.segments):
            return False
        del self.segments[active_index + 1]
        return True
