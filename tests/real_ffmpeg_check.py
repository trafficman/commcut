"""Manual end-to-end check of the real Popen export path.

The suite mocks ffmpeg, so the parts of the export that can only break against
a real process -- `subprocess.Popen`, stderr routed to a temporary file, a
`terminate()` landing on a live encoder -- are never executed by
`python -m pytest`. This script runs them: a clean batch, a cancel part-way
through, the resume that follows it, and a clip that fails.

It is deliberately not named `test_*.py`, so pytest does not collect it. It
needs the bundled ffmpeg (`bin/<os>/`), writes real video, and takes a few
seconds. Run it by hand after touching `shared/ffmpeg.py`:

    py -3.11 tests/real_ffmpeg_check.py
"""

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.environment import get_binary_path
from shared.exporting import (
    ExportPlan,
    ExportPlanError,
    ExportSchemes,
    PlannedExportClip,
    plan_export,
)
from shared.ffmpeg import execute_export_plan
from shared.segments import SegmentModel

FFMPEG = get_binary_path("ffmpeg")


def make_source(path, seconds=6):
    subprocess.run(
        [
            FFMPEG, "-y", "-f", "lavfi", "-i",
            f"testsrc=duration={seconds}:size=320x240:rate=10",
            "-c:v", "libx264", "-crf", "30", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            path,
        ],
        check=True,
        capture_output=True,
    )
    return path


def make_broken_source(path):
    """A file that exists but is not decodable, so ffmpeg exits non-zero."""
    with open(path, "wb") as handle:
        handle.write(b"not a video")
    return path


def main():
    work = tempfile.mkdtemp(prefix="commcut-real-")
    source = make_source(os.path.join(work, "source.mp4"))
    library = os.path.join(work, "export")
    failures = []

    def check(label, condition, detail=""):
        print(f"{'ok  ' if condition else 'FAIL'} {label}{(' -- ' + detail) if detail else ''}")
        if not condition:
            failures.append(label)

    # One model and one scheme for the whole script, so the destinations the
    # planner produces are the ones the executor writes and the resume checks
    # match. Driving the real planner rather than a hand-built plan is the
    # point: this is the same handoff the editor makes.
    schemes = ExportSchemes("{title}", "{network}/{filler_type}/{time_period}")
    model = SegmentModel(
        "source.mp4", 6.0,
        [
            {"start": index * 2.0, "ignored": False, "tags": {
                "title": f"Clip {index + 1}", "network": "Network",
                "filler_type": "Promo", "time_period": "2000s",
            }}
            for index in range(3)
        ],
    )
    plan = plan_export(model, schemes, library)
    paths = [clip.relative_path for clip in plan.clips]
    clip_files = [os.path.join(library, *clip.relative_components) for clip in plan.clips]

    # 1. A clean batch, driven only by the progress callback.
    seen = []
    result = execute_export_plan(
        source,
        plan,
        ffmpeg_path=FFMPEG,
        on_progress=lambda done, total, current: seen.append((done, total, current)),
    )
    check("clean export writes every clip", result.succeeded == 3, f"succeeded={result.succeeded}")
    check("clean export reports no failures", result.failed == 0)
    check("clean export is not cancelled", result.cancelled is False)
    check("every clip is a real file", all(os.path.getsize(p) > 0 for p in clip_files))
    check("progress fired per clip plus completion",
          seen == [(0, 3, paths[0]), (1, 3, paths[1]), (2, 3, paths[2]), (3, 3, "")],
          str(seen))
    check("written_relative_paths matches", result.written_relative_paths == tuple(paths))

    # 2. A resume against the destinations check 1 actually wrote.
    try:
        plan_export(model, schemes, library)
        check("a plain replan refuses the written destinations", False, "no error raised")
    except ExportPlanError as error:
        check("a plain replan refuses the written destinations", "already exists" in str(error))

    try:
        plan_export(
            model, schemes, library,
            skip_destinations=result.written_relative_paths,
        )
        check("resuming everything says they were written", False, "no error raised")
    except ExportPlanError as error:
        check("resuming everything says they were written", "already written" in str(error))

    # 3. A mid-batch cancel against a real encoder: clip 1 is written, clip 2 is
    #    terminated in flight, clip 3 is never started. That is the state the
    #    resume path has to cope with.
    cancel_library = os.path.join(work, "cancel")
    cancel_model = SegmentModel(
        "source.mp4", 6.0,
        [
            {"start": index * 2.0, "ignored": False, "tags": {
                "title": f"Long {index + 1}", "network": "Network",
                "filler_type": "Promo", "time_period": "2000s",
            }}
            for index in range(3)
        ],
    )
    cancel_plan = plan_export(cancel_model, schemes, cancel_library)
    started = {"done": 0}

    def should_cancel():
        return started["done"] >= 1

    cancelled = execute_export_plan(
        source,
        cancel_plan,
        ffmpeg_path=FFMPEG,
        on_progress=lambda done, total, current: started.__setitem__("done", done),
        should_cancel=should_cancel,
    )
    written = set(cancelled.written_relative_paths)
    on_disk = set()
    for dirpath, _dirnames, filenames in os.walk(cancel_library):
        for name in filenames:
            on_disk.add(name)
    check("cancel is recorded as cancelled", cancelled.cancelled is True)
    check("a cancelled clip is not a failure", cancelled.failures == ())
    check("only the completed clip was written", len(written) == 1, str(written))
    check("the run reports exactly what is on disk",
          {name for name in on_disk} == {os.path.basename(p) for p in written},
          str(sorted(on_disk)))
    check("no temp file survived the cancel",
          not any(name.startswith(".commcut-export-") for name in on_disk),
          str(sorted(on_disk)))

    # 4. Resuming that cancelled run: the one written clip is skipped and the
    #    two that never ran are cut.
    resumed = plan_export(
        cancel_model,
        schemes,
        cancel_library,
        skip_destinations=cancelled.written_relative_paths,
    )
    check("resume skips the written clip and plans the rest",
          len(resumed.clips) == 2 and len(resumed.skipped) == 1)
    finished = execute_export_plan(source, resumed, ffmpeg_path=FFMPEG)
    check("the resumed run writes exactly the two it planned",
          finished.succeeded == 2 and finished.failed == 0)
    check("all three clips now exist on disk",
          len([n for n in os.listdir(os.path.join(cancel_library, "Network", "Promo", "2000s"))]) == 3)

    # 4. A failing clip still surfaces ffmpeg's own stderr, now via a temp file.
    bad_plan = ExportPlan(
        export_root=os.path.join(work, "bad"),
        clips=(
            PlannedExportClip(
                segment_index=0, start=0.0, duration=2.0,
                relative_components=("Network", "Bad.mp4"),
            ),
        ),
    )
    broken = make_broken_source(os.path.join(work, "broken.mp4"))
    bad = execute_export_plan(broken, bad_plan, ffmpeg_path=FFMPEG)
    check("a failing clip is reported, not raised", bad.failed == 1 and bad.succeeded == 0)
    check("the failure message is ffmpeg's own stderr",
          bool(bad.failures) and "Invalid data" in bad.failures[0].message,
          bad.failures[0].message if bad.failures else "none")
    bad_leftovers = [
        name for name in os.listdir(os.path.join(work, "bad", "Network"))
    ] if os.path.isdir(os.path.join(work, "bad", "Network")) else []
    check("a failing clip leaves no temp file", bad_leftovers == [], str(bad_leftovers))

    # 5. A missing source is refused before any ffmpeg runs.
    try:
        execute_export_plan(
            os.path.join(work, "nope.mp4"), bad_plan, ffmpeg_path=FFMPEG
        )
    except FileNotFoundError:
        check("a missing source is refused outright", True)
    else:
        check("a missing source is refused outright", False, "no exception")

    shutil.rmtree(work, ignore_errors=True)
    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {failures}")
        return 1
    print("all real-ffmpeg checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
