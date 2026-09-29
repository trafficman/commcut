# Experiments

Code that answers a question the documentation could not. Nothing here ships:
`packaging/build.py` and the suite both treat this folder as not-the-app, and
`tests/test_frozen_mode.py` lists it in `EXCLUDED_FOLDERS` for the same reason
it lists `prototypes/`.

Each experiment is a directory with the runner, and a section below recording
what it found, when, and on what. **A result belongs here as much as the code
does** — an experiment whose finding is not written down has only cost time.

---

## `mpv_foreground/`

### The question

The app opens every window as a separate process because mpv's video output is
believed to deadlock when a player is constructed while another top-level window
is foreground. `AGENTS.md` carries that as invariant 7, and `mainwindow.py`,
`shared/environment.launch_command`, `main.py`'s `--window` dispatcher, and
`packaging/commcut.spec`'s decision to keep `bin/win/` outside the exe are all
downstream of it.

The rule is stated with more confidence than the evidence behind it supports.
There is no recorded reproduction, no stack, no frequency, and no trigger
condition anywhere in the tree. The nearest thing to a record is the workaround
in `editor/editor.py:1195` and `scanner/scanner.py:385`, which close a splash
screen before building a player — and a splash is a top-level window *in the
same process*. So the codebase's own fix for the hazard is a focus change, not
a process boundary, and invariant 7's word "second" does not survive that
reading.

This experiment asks the question directly: **does it happen, and what actually
triggers it?**

### Method

Two files. `probe.py` runs exactly one case in one process and writes a JSON
verdict. `run_matrix.py` runs each case N times, each in its own process under a
hard timeout, because the failure under test is a process that never returns.
A run the timeout kills is the `HANG` verdict, and the step name the probe wrote
before it blocked says where it died.

Two design points make the result mean something:

- **The harness has to be able to fail.** `Z_control_block` is a negative
  control: it blocks deliberately at the construction step. It reports `HANG` at
  phase `construct#1`, which is exactly what a real deadlock at that point
  would look like. Without it, a table of zeroes is indistinguishable from an
  instrument that never fires.
- **The foreground has to be real.** `raise_()`/`activateWindow()` are subject
  to Windows' foreground lock, and run from a console the console usually keeps
  it — so a case can silently stop testing the claim. The probe compares
  `GetForegroundWindow()` against the intended HWND and reports
  `foreground_confirmed` per run. The first version of this harness did not, and
  reported "no foreground" for cases it was not really running.

A player is only counted as working when its VO came up (`vo-configured`) *and*
the playhead advanced (`time-pos > 0`), so a VO that initialises and presents
nothing cannot pass.

The cases with no video-output override call the shipped
`shared.mpv.create_mpv_player` unchanged, so a pass describes the
configuration the app actually runs.

### Cases

| Case | Driver | Foreground window at construction | Live players |
|---|---|---|---|
| `A_baseline` | shipped (`direct3d`) | none | 1 |
| `B_d3d_foreground` | shipped (`direct3d`) | a plain window | 1 |
| `C_gpu_win_foreground` | `vo=gpu`, `gpu-context=win` | a plain window | 1 |
| `D_gpu_d3d11_foreground` | `vo=gpu`, `gpu-context=d3d11` | a plain window | 1 |
| `E_d3d_three_players` | shipped (`direct3d`) | a plain window | 3, all playing |
| `F_d3d_splash` | shipped (`direct3d`) | a frameless `QSplashScreen` | 1 |
| `Z_control_block` | shipped | a plain window | blocks on purpose |

`B` is the pivotal one: one player, one other window genuinely foreground, the
shipped driver. `F` is the scenario the codebase documents as deadlocking.
`E` is invariant 7 read literally — a second and third player, all presenting.

### Running it

    python experiments/mpv_foreground/run_matrix.py
    python experiments/mpv_foreground/run_matrix.py --cases B_d3d_foreground --reps 20
    python experiments/mpv_foreground/probe.py --case F_d3d_splash

Needs a real interactive desktop session and a real `libmpv`. With
`QT_QPA_PLATFORM=offscreen` there is no foreground window and the claim cannot
be posed; the harness clears that variable for the child, and the probe reports
`foreground_hwnd=None` if it ever runs offscreen. **Do not run the suite at the
same time** — the foreground assertions are sensitive to other windows taking
focus.

### Result

Windows, mpv `v0.41.0-39-ga58dd8ac4`, foreground confirmed on every run.
Two independent full matrices of 10 runs per case, run 2 shown; run 1 agreed
(`results.json`, `results_confirmation.json`):

| case | players | runs | OK | HANG | VO_FAIL | NO_FRM | ERR | construct med/max s | 1st frame med s |
|---|---|---|---|---|---|---|---|---|---|
| A_baseline | 1 | 10 | 10 | 0 | 0 | 0 | 0 | 0.061 / 0.096 | 0.159 |
| B_d3d_foreground | 1 | 10 | 10 | 0 | 0 | 0 | 0 | 0.058 / 0.064 | 0.178 |
| C_gpu_win_foreground | 1 | 10 | 10 | 0 | 0 | 0 | 0 | 0.073 / 0.087 | 0.398 |
| D_gpu_d3d11_foreground | 1 | 10 | 10 | 0 | 0 | 0 | 0 | 0.072 / 0.080 | 0.392 |
| E_d3d_three_players | 3 | 10 | 10 | 0 | 0 | 0 | 0 | 0.007 / 0.144 | 0.294 |
| F_d3d_splash | 1 | 10 | 10 | 0 | 0 | 0 | 0 | 0.051 / 0.067 | 0.168 |

**The stated trigger does not reproduce.** 120 runs across the two matrices,
no hang, on the shipped `direct3d` driver, with the foreground window verified
rather than assumed — including the splash case the codebase documents as
deadlocking, and three concurrent presenting players in one process.

Two incidental findings worth keeping:

- **Only the first player in a process pays device setup.** In `E`, construct
  times are `[~0.10, ~0.006, ~0.006]` seconds. The second and third players are
  an order of magnitude cheaper because the process already has its device. So
  the cost argument for one-process-per-window is weaker than the correctness
  one, and the correctness one is what this fails to support.
- **`vo=gpu` is slower to first frame here, not faster**: ~0.39 s against
  ~0.17 s for `direct3d`. Switching drivers is therefore not free, and
  `shared/environment.MPV_VIDEO_OUTPUT` should not be changed on the strength of
  a deadlock claim that did not reproduce.

### What this does not establish

- **One machine, one GPU, one driver, one mpv build.** The original observation
  was made somewhere, at some time, on some driver; nothing in the tree records
  which. This shows the stated trigger does not fire *here*, with mpv 0.41.0-39
  and this GPU. It does not prove it is absent everywhere, and a user on
  different hardware could still hit whatever was originally seen.
- **It is not the real app.** These are bare `QMainWindow`s, not the editor or
  scanner. Whether the real windows survive one event loop is a separate
  question this does not answer, and was answered instead by doing the
  refactor and walking the journey by hand.
- **One unresolved intermittent.** In one earlier matrix run, 4 of 10
  `E_d3d_three_players` runs were reported `ERROR` even though the probe's own
  stdout showed `"verdict": "OK"` and the result file was absent — a harness
  race, not an mpv failure, since the probe had already succeeded. It did not
  reproduce in the isolated re-run or in two subsequent full matrices. The
  harness now records the child's return code and keeps the scratch files for
  any non-OK verdict. Unresolved.
- **Crash isolation is a separate cost.** A hard fault inside `libmpv-2.dll`
  used to take down one window; single-process it takes down the app. No
  experiment informs that; it is a product decision.
- **Packaging does not follow automatically.** One process means one payload
  extraction, but a ~412 MB onefile extracting on every start is worse than a
  ~46 MB one extracting four times. The pairing that follows is single-process
  **plus onedir**, or keeping `bin/win/` beside the exe — see
  [packaging.md](../docs/packaging.md).

### What was done with it

The process model was removed on the strength of this result. `launch_command`,
the `--window` dispatcher, the per-window `run()` entry points and the
`scanner/` folder-shadowing guard are all gone; `shared/session.py` now owns one
`QApplication` and a window stack, and the trade is written down as an accepted
cost in [docs/status.md](../docs/status.md) rather than presented as a solved
problem. If a hang ever does appear, this harness is the thing to re-run first:
it is the only version of the claim in this tree that produces evidence rather
than an assertion.
