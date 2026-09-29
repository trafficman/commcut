"""Does closing a window with a live mpv player hang the process?

The bug: ``create_mpv_player`` hands mpv a ``wid`` -- the native handle of a
child ``QFrame`` -- and nothing shut that player down before this app had one
process. Closing the window destroyed the handle, and the ``mpv.MPV`` finalizer
then joined libmpv's threads from inside the GUI thread, against a window that
no longer existed. The GUI thread blocked, so the app stopped responding.

This is the only harness in the tree that can catch a *missing* teardown,
because the bug needs a real libmpv and a real HWND; the suite stands in for the
player by design. It is built on the same watchdog idea as
``experiments/mpv_foreground``: each case runs in its own process under a hard
timeout, so a hang is a verdict rather than a wedged terminal.

Two cases, and the first one is the point:

    control    close the window WITHOUT shutting the player down (today's bug)
    shutdown   close the window after MpvBridge.shutdown(), which is the fix

``control`` must report HANG. Without it, a passing ``shutdown`` case proves
nothing -- it is the same argument that made Z_control_block necessary in the
foreground experiment.

Run it from a real desktop session:

    python experiments/mpv_teardown/run_matrix.py
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(HERE))
RESULTS = os.path.join(HERE, "results.json")

NO_WINDOW = 0x08000000

CASES = {
    "control": "close the shipped window with the player teardown disabled "
               "(the pre-fix state, which is what froze)",
    "fix": "close the shipped window with MpvBridge.shutdown() in place",
}


def _read(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return handle.read().strip() or None


def run_once(case, index, timeout, play_timeout, window="editor"):
    stem = os.path.join(HERE, f".run-{case}-{window}-{index}")
    phase_file = f"{stem}.phase"
    result_file = f"{stem}.json"
    for path in (phase_file, result_file):
        if os.path.exists(path):
            os.remove(path)

    environment = dict(os.environ)
    # There is no foreground window offscreen, and the video frame needs a real
    # HWND for mpv to embed into, so this harness cannot run headless.
    environment.pop("QT_QPA_PLATFORM", None)

    command = [
        sys.executable, os.path.join(HERE, "probe.py"),
        "--case", case, "--window", window,
        "--phase-file", phase_file,
        "--result-file", result_file,
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
            "detail": f"no result after {timeout}s: the GUI thread never came "
                      f"back from closing the window",
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
    if record.get("verdict") == "OK":
        for path in (phase_file, result_file):
            if os.path.exists(path):
                os.remove(path)
    else:
        record["artifacts"] = [p for p in (phase_file, result_file)
                               if os.path.exists(p)]
    return record


def summarize(runs):
    by_case = {}
    for run in runs:
        by_case.setdefault(run["case"], []).append(run)
    rows = []
    for case, case_runs in by_case.items():
        verdicts = [run["verdict"] for run in case_runs]
        totals = [run["total_seconds"] for run in case_runs
                  if run.get("total_seconds") is not None]
        rows.append({
            "case": case,
            "runs": len(case_runs),
            "ok": verdicts.count("OK"),
            "hang": verdicts.count("HANG"),
            "error": verdicts.count("ERROR") + verdicts.count("NO_FRAMES"),
            "median_s": round(statistics.median(totals), 3) if totals else None,
            "max_s": max(totals) if totals else None,
        })
    return rows


def print_table(rows):
    headers = ["case", "runs", "OK", "HANG", "ERR", "median s", "max s"]
    print("| " + " | ".join(headers) + " |")
    print("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        print("| " + " | ".join([
            row["case"], str(row["runs"]), str(row["ok"]), str(row["hang"]),
            str(row["error"]),
            "-" if row["median_s"] is None else str(row["median_s"]),
            "-" if row["max_s"] is None else str(row["max_s"]),
        ]) + "|")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default=",".join(sorted(CASES)))
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=40.0)
    parser.add_argument("--play-timeout", type=float, default=10.0)
    parser.add_argument("--out", default=RESULTS)
    parser.add_argument("--windows", default="editor,scanner")
    args = parser.parse_args()

    cases = [name.strip() for name in args.cases.split(",") if name.strip()]
    unknown = [name for name in cases if name not in CASES]
    if unknown:
        parser.error(f"unknown case(s): {', '.join(unknown)}")
    windows = [name.strip() for name in args.windows.split(",") if name.strip()]

    runs = []
    for window in windows:
        for case in cases:
            for index in range(args.reps):
                label = f"{window}/{case}"
                print(f"  {label} run {index + 1}/{args.reps} ...", flush=True)
                run = run_once(
                    case, index, args.timeout, args.play_timeout, window)
                run["label"] = label
                runs.append(run)
                print(f"    -> {run['verdict']}", flush=True)

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"runs": runs, "summary": summarize(runs)}, handle, indent=2)

    print()
    print_table(summarize(runs))
    print()
    print(f"results written to {args.out}")
    print("\nNOTE: a green row here means the process stayed responsive and the "
          "main menu took a *posted* click after the window closed. It does NOT "
          "mean the mouse worked. This harness has been unable to reproduce the "
          "reported input lockout from the inside -- see experiments/README.md "
          "for the four mechanisms it measured and ruled out. Confirm the freeze "
          "by hand, using --hold on a single case.")

    if "fix" in cases and "control" in cases:
        by_case = {}
        for run in runs:
            by_case.setdefault(run["case"], []).append(run)
        control, fixed = by_case.get("control", []), by_case.get("fix", [])
        control_ok = sum(1 for run in control if run["verdict"] == "OK")
        fixed_ok = sum(1 for run in fixed if run["verdict"] == "OK")
        if control_ok == len(control):
            print("\nINCONCLUSIVE: the control case completed normally, so a "
                  "passing fix case proves nothing on this machine. Either the "
                  "freeze needs something this harness does not do, or it is "
                  "not reproducible here -- do not read a green fix as a "
                  "confirmed fix.")
            return 1
        if fixed_ok != len(fixed):
            print("\nREGRESSION: the fix does not hold on every run.")
            return 1
        print("\nThe control fails and the fix does not: the teardown is what "
              "makes the difference.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
