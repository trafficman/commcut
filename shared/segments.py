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


# Outcome codes returned by SegmentModel.place_end_boundary().
END_BOUNDARY_INSERTED = "inserted"
END_BOUNDARY_MOVED = "moved"
END_BOUNDARY_NO_CHANGE = "no_change"
END_BOUNDARY_BLOCKED = "blocked"


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

    def end_segment(self, active_index, position, tags=None):
        """Split the active segment at position.

        The active segment (left part) keeps the active index. A new segment
        is inserted to its right. That new segment's tags default to a copy of
        the active segment's tags; pass `tags` to override them (the editor
        passes its locked tag values so only locked tags carry over).

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
            "tags": dict(seg["tags"]) if tags is None else dict(tags),
        }
        self.segments.insert(active_index + 1, new_seg)
        return True

    def place_end_boundary(self, active_index, position, tags=None) -> str:
        """Place the active segment's end transition point at position.

        A segment's end and the next segment's start are the same stored
        value, so "put my end boundary here" has two possible answers: insert
        a boundary (the playhead is inside the active segment, so a new
        segment is created) or move the existing one (the playhead is already
        past the end, but still within the following segment).

        Exactly one transition point is ever affected. A position beyond the
        following segment is refused rather than clamped, so this can never
        quietly eat several segments -- absorbing one is what merge_next is
        for. Moving the boundary necessarily resizes both neighbours, since
        they share that value; no tags or ignored flags are touched.

        A position at or before the active segment's start is left alone for
        now; only the forward direction is handled.

        Returns END_BOUNDARY_INSERTED, END_BOUNDARY_MOVED, or
        END_BOUNDARY_NO_CHANGE (the boundary already sits there, or the
        position is at a video edge with nothing to move), or
        END_BOUNDARY_BLOCKED (the position would collapse a neighbour to zero
        length, or would cross a second boundary).
        """
        if not 0 <= active_index < len(self.segments):
            return END_BOUNDARY_NO_CHANGE
        start = self.start(active_index)
        end = self.end(active_index)

        if start < position < end:
            if self.end_segment(active_index, position, tags):
                return END_BOUNDARY_INSERTED
            return END_BOUNDARY_NO_CHANGE

        if position > end:
            next_index = active_index + 1
            if next_index >= len(self.segments):
                # Last segment: its end is the video duration, and there is no
                # following segment to give the time to.
                return END_BOUNDARY_NO_CHANGE
            if position < self.end(next_index):
                self.segments[next_index]["start"] = position
                return END_BOUNDARY_MOVED
            return END_BOUNDARY_BLOCKED

        return END_BOUNDARY_NO_CHANGE

    def merge_next(self, active_index):
        """Remove the boundary after the active segment, merging it with the next.

        The active segment absorbs the next one's span. The next segment's
        metadata is discarded. Returns True if a merge happened.
        """
        if active_index + 1 >= len(self.segments):
            return False
        del self.segments[active_index + 1]
        return True

    def start_segment(self, active_index, position, tags=None):
        """Split the active segment at position, activating the right part.

        The left part (behind the cut) is marked ignored. A new segment is
        inserted to the right and becomes the active segment. Its tags default
        to a copy of the active segment's tags; pass `tags` to override them
        (the editor passes its locked tag values so only locked tags carry
        over). Returns True if a split was made.
        """
        seg = self.segments[active_index]
        start = seg["start"]
        end = self.end(active_index)
        position = max(start, min(position, end))
        if position <= start or position >= end:
            return False
        # Left part (current active) becomes ignored.
        seg["ignored"] = True
        # Right part becomes the new active segment.
        new_seg = {
            "start": position,
            "ignored": False,
            "tags": dict(seg["tags"]) if tags is None else dict(tags),
        }
        self.segments.insert(active_index + 1, new_seg)
        return True
