"""Run the probe matrix and turn hangs into verdicts.

Each case is run ``--reps`` times, each run in its own process with a hard
timeout, because the failure under test is a process that never returns. A run
the timeout kills is the HANG verdict and the probe's phase file names the step
it died in; a run that finishes without a result file is an ERROR carrying the
probe's stderr.

    python experiments/mpv_foreground/run_matrix.py
    python experiments/mpv_foreground/run_matrix.py --cases A_baseline --reps 3

Writes results.json next to itself and prints a summary table.
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE = os.path.join(HERE, "probe.py")
RESULTS = os.path.join(HERE, "results.json")

sys.path.insert(0, HERE)
from probe import CASES  # noqa: E402  (path set above)

NO_WINDOW = 0x08000000


def _read(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return handle.read().strip() or None


def run_once(case, index, timeout, play_timeout):
    stem = os.path.join(HERE, f".run-{case}-{index}")
    phase_file = f"{stem}.phase"
    result_file = f"{stem}.json"
    for path in (phase_file, result_file):
        if os.path.exists(path):
            os.remove(path)

    environment = dict(os.environ)
    environment.pop("QT_QPA_PLATFORM", None)

    command = [
        sys.executable, PROBE, "--case", case,
        "--phase-file", phase_file, "--result-file", result_file,
        "--play-timeout", str(play_timeout),
    ]

    started = time.monotonic()
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout,
            env=environment, creationflags=NO_WINDOW)
        elapsed = round(time.monotonic() - started, 3)
    except subprocess.TimeoutExpired:
        return _finish({
            "case": case, "repetition": index, "verdict": "HANG",
            "detail": f"no result after {timeout}s",
            "phase": _read(phase_file), "total_seconds": timeout,
        }, phase_file, result_file)

    payload = _read(result_file)
    if payload is None:
        return _finish({
            "case": case, "repetition": index, "verdict": "ERROR",
            "returncode": completed.returncode,
            "detail": (completed.stderr or completed.stdout or "")[-1500:],
            "phase": _read(phase_file), "total_seconds": elapsed,
        }, phase_file, result_file)

    try:
        payload = json.loads(payload)
    except ValueError:
        return _finish({
            "case": case, "repetition": index, "verdict": "ERROR",
            "returncode": completed.returncode,
            "detail": f"unreadable result file: {payload[-1500:]}",
            "phase": _read(phase_file), "total_seconds": elapsed,
        }, phase_file, result_file)

    payload["repetition"] = index
    payload["elapsed"] = elapsed
    payload["returncode"] = completed.returncode
    return _finish(payload, phase_file, result_file)


def _finish(record, phase_file, result_file):
    """Clear the scratch files, except when the run did not pass.

    A verdict the probe agrees with needs nothing kept. A non-OK verdict may
    have lost a race with process teardown, and the exit code plus the files are
    the only evidence of what the child actually did, so they are left in place
    and named on stdout.
    """
    if record.get("verdict") != "OK":
        record["artifacts"] = [path for path in (phase_file, result_file)
                               if os.path.exists(path)]
        return record
    for path in (phase_file, result_file):
        if os.path.exists(path):
            os.remove(path)
    return record


def summarize(runs):
    by_case = {}
    for run in runs:
        by_case.setdefault(run["case"], []).append(run)

    rows = []
    for case, case_runs in by_case.items():
        verdicts = [run["verdict"] for run in case_runs]
        constructs = [
            value for run in case_runs
            for value in run.get("construct_seconds") or []
        ]
        frames = [
            run["first_frame_seconds"] for run in case_runs
            if run.get("first_frame_seconds") is not None
            and run["verdict"] == "OK"
        ]
        confirmed = [run.get("foreground_confirmed") for run in case_runs]
        rows.append({
            "case": case,
            "runs": len(case_runs),
            "ok": verdicts.count("OK"),
            "hang": verdicts.count("HANG"),
            "vo_failed": verdicts.count("VO_FAILED"),
            "no_frames": verdicts.count("NO_FRAMES"),
            "error": verdicts.count("ERROR"),
            "construct_median_s": round(statistics.median(constructs), 3)
            if constructs else None,
            "construct_max_s": max(constructs) if constructs else None,
            "first_frame_median_s": round(statistics.median(frames), 3)
            if frames else None,
            "mpv_version": next(
                (run.get("mpv_version") for run in case_runs
                 if run.get("mpv_version")), None),
            "players": max(
                (run.get("players") or 0 for run in case_runs), default=0),
            "foreground_confirmed": (
                "yes" if all(confirmed) else
                "no" if not any(confirmed) else "partial"),
        })
    return rows


def print_table(rows):
    headers = [
        "case", "players", "runs", "OK", "HANG", "VO_FAIL", "NO_FRM", "ERR",
        "construct med/max s", "1st frame med s", "fg confirmed",
    ]
    print("| " + " | ".join(headers) + " |")
    print("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        construct = ("-" if row["construct_median_s"] is None else
                     f"{row['construct_median_s']} / "
                     f"{row['construct_max_s']}")
        frame = "-" if row["first_frame_median_s"] is None \
            else str(row["first_frame_median_s"])
        print("| " + " | ".join([
            row["case"], str(row["players"]), str(row["runs"]), str(row["ok"]),
            str(row["hang"]), str(row["vo_failed"]), str(row["no_frames"]),
            str(row["error"]), construct, frame, row["foreground_confirmed"],
        ]) + "|")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default=",".join(sorted(CASES)))
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--play-timeout", type=float, default=10.0)
    parser.add_argument(
        "--out", default=RESULTS,
        help="where to write the run records; a second run should not "
             "overwrite the first")
    args = parser.parse_args()

    cases = [name.strip() for name in args.cases.split(",") if name.strip()]
    unknown = [name for name in cases if name not in CASES]
    if unknown:
        parser.error(f"unknown case(s): {', '.join(unknown)}")

    runs = []
    for case in cases:
        for index in range(args.reps):
            print(f"  {case} run {index + 1}/{args.reps} ...", flush=True)
            run = run_once(case, index, args.timeout, args.play_timeout)
            runs.append(run)
            print(f"    -> {run['verdict']}", flush=True)

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"runs": runs, "summary": summarize(runs)}, handle, indent=2)

    print()
    print_table(summarize(runs))
    print()
    print(f"results written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
