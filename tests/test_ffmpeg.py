"""Tests for plan-based ffmpeg execution and partial-failure reporting."""

import os
from types import SimpleNamespace

import pytest

from shared.exporting import ExportPlan, ExportPlanError, ExportSchemes, PlannedExportClip
from shared.ffmpeg import execute_export_plan, export_named_model
from shared.paths import DEFAULT_FOLDER_SCHEME
from shared.segments import SegmentModel


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

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
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        with open(command[-1], "wb") as output_file:
            output_file.write(b"encoded")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("shared.ffmpeg.subprocess.run", fake_run)

    result = execute_export_plan(source_path, plan, ffmpeg_path="ffmpeg")

    destination = root / "Network" / "Promo" / "2000s" / "Clip.mp4"
    assert result.succeeded == 1
    assert result.failed == 0
    assert result.written_paths == (str(destination),)
    assert destination.read_bytes() == b"encoded"
    assert commands[0][1:5] == ["-y", "-ss", "0.0", "-i"]
    assert commands[0][-1].startswith(str(root))
    assert os.path.basename(commands[0][-1]).startswith(".commcut-export-")


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

    def fake_run(command, **kwargs):
        with open(command[-1], "wb") as output_file:
            output_file.write(b"encoded")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("shared.ffmpeg.subprocess.run", fake_run)

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
    calls = []

    monkeypatch.setattr(
        "shared.ffmpeg.subprocess.run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with pytest.raises(ExportPlanError, match="already exists"):
        execute_export_plan(source_path, plan, ffmpeg_path="ffmpeg")
    assert not calls


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
    calls = 0

    def fake_run(command, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            return SimpleNamespace(returncode=1, stderr="encoder failed")
        with open(command[-1], "wb") as output_file:
            output_file.write(b"encoded")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("shared.ffmpeg.subprocess.run", fake_run)

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

    def racing_run(command, **kwargs):
        with open(command[-1], "wb") as output_file:
            output_file.write(b"encoded")
        with open(destination, "wb") as raced_destination:
            raced_destination.write(b"other export")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("shared.ffmpeg.subprocess.run", racing_run)

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
