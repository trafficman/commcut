# Testing

How the suite runs, what the shared harness gives you, which file covers which
area, and the Qt/import gotchas that decide whether a test can see what it is
trying to test.

Applies to: `tests/`, `tests/conftest.py`, `tests/editor_stub.py`.

## Running the suite

```powershell
python -m pytest              # everything, from the project root
python -m pytest tests/test_docs.py   # the documentation guard
```

There is no `pytest.ini`, `pyproject.toml`, or `setup.cfg`. Two things come from
`tests/conftest.py` instead: the project root is put on `sys.path` (so
`shared.*`, `editor.editor`, etc. import), and `COMMCUT_LOG` is pointed at the
temp directory so a test run never leaves a `commcut.log` in the source tree.

Widget tests set `QT_QPA_PLATFORM=offscreen` themselves
(`tests/editor_stub.py:ensure_qapp`, `tests/test_settings.py`); nothing needs to
be set by hand.

## The shared harness

The editor window itself needs libmpv and a real video, so tests bind the real
`MediaPlayer` methods onto a stub instead of re-implementing them.
`tests/editor_stub.py` supplies the `ui` namespace and the segment model, which
keeps the code under test the *shipped* code.

- `ensure_qapp()` — the offscreen `QApplication` the widget-backed tests need.
- `FakeBridge` — the `MpvBridge` surface the editor uses, without libmpv or a
  video. It **records every seek** and every frame/keyframe step, which is what
  lets a test assert the exact playhead *sequence* (the boundary peek is made of
  a sequence, not of a resting position).

Six test files bind the real `MediaPlayer` through `EditorStub`:
`test_boundary_preview.py`, `test_editor_locks.py`, `test_editor_required_tags.py`,
`test_end_boundary.py`, `test_timeline_zoom.py`, and `test_editor_export.py`.
`test_boundary_preview.py` additionally uses `FakeBridge`; `ensure_qapp` is
imported more widely (`test_main_window.py`, `test_picker.py`).

## Which file covers what

| File | Covers |
|---|---|
| `test_boundary_preview.py` | the boundary peek: the seek sequence, and every rule that cancels a pending one |
| `test_editor_locks.py` | tag-lock display, pinned-value semantics, locked-only segment carry-over |
| `test_editor_required_tags.py` | front-end enforcement of the four required fields, including refusal to write |
| `test_end_boundary.py` | `place_end_boundary` — insert vs. move, and the refusal guards |
| `test_timeline_zoom.py` | zoom state surviving a resize, and the editor toggle agreeing with the widget |
| `test_sources.py` | the import folder: what can be opened, what the picker offers, the "nothing to open" messages, and the containment/traversal defense incl. case-insensitive volumes |
| `test_source_handoff.py` | the chosen source surviving every window-to-window hand-off |
| `test_picker.py` | the picker window, offscreen |
| `test_main_window.py` | the main menu, the launcher, and the failure paths |
| `test_frozen_mode.py` | frozen roots, per-platform binary and libmpv resolution, table completeness, mpv `vo`, child-window argv, spec/`resource_path` agreement, and the sweep that every spawn site carries `no_console_kwargs()` |
| `test_mpv_player.py` | how the player is built: libmpv loaded before the import that needs it, the native handle, and the zero-handle refusal (no real player) |
| `test_scheme.py` | strict scheme parsing |
| `test_naming.py` | filename rendering, including the README pattern |
| `test_paths.py` | strict folder scheme compilation, rendering, and sanitation |
| `test_exporting.py` | export settings, destination planning, preflight, resume skips |
| `test_ffmpeg.py` | plan-based ffmpeg execution: progress, cancel, partial-failure reporting, the encoder probe, and the no-console flag reaching `Popen` (`Popen` mocked) |
| `test_editor_export.py` | the export worker thread, progress dialog, cancel, resume, close-mid-run, and the summary screen (window wiring with a substituted dialog, plus the shipped dialog itself) |
| `test_settings.py` | the Settings window: defaults, previews, atomic save, help panels |
| `test_docs.py` | the documentation guard (see below) |

## Gotchas that decide whether a test means anything

- **A resized widget must be visible.** Qt delivers `resizeEvent` to a
  *visible* widget only, so any test that resizes a widget to check zoom
  behavior has to `show()` it first. Otherwise the assertion passes for the
  wrong reason.
- **The shadowing trap needs a fresh interpreter.** A window folder that shadows
  its own package (see [packaging.md](packaging.md)) cannot be reproduced
  in-process once `scanner` is cached in `sys.modules` as the package — an
  in-process reproduction succeeds against broken code. `test_source_handoff.py`
  therefore runs each window's script the way `launch_command` launches it, in a
  **new interpreter**.
- **Frozen mode is simulated, not built.** `test_frozen_mode.py` monkeypatches
  `sys.frozen` / `sys._MEIPASS` / `sys.executable` rather than producing an
  executable.
- **Walking the tree beats walking a list.**
  `test_every_ui_file_in_the_tree_is_listed_and_bundled` walks the source tree
  for `.ui` files and requires every one of them to be in both the list and the
  spec: iterating the list alone only checks files it already knows about, so a
  new window's `.ui` that was never bundled would pass and then fail only in a
  packaged build.
- **Documentation claims are testable.** `test_file_help_states_the_title_rule_the_compiler_enforces`
  checks the in-app help against `file_scheme_error`, so the help cannot drift
  into lying. The same idea guards these docs (below).
- **A stubbed thread is not enough to prove off-thread work.** The export tests
  substitute `QThread` and `ExportWorker` so the wiring runs synchronously and
  cannot flake. That combination cannot distinguish "runs elsewhere" from
  "returns fast", so `test_the_gui_thread_is_not_blocked_by_the_batch` uses a
  real `QThread` and a real blocking executor, and asserts a *queued* signal is
  delivered while the worker is still running. The queued connection is the
  point: a direct one fires without the event loop, so it would pass against the
  original bug.
- **A modal dialog is substituted, not the method that shows it.**
  `test_editor_export.py` replaces `ExportSummaryDialog` as a *class* and leaves
  `MediaPlayer._ask_export_summary` alone, so the shipped ordering still runs —
  in particular that the dialog is released before the editor closes, which is
  what stops the process from lingering with a hidden window still counted.
  Stubbing the method instead would have thrown that away, and the fake records
  `editor_enabled` at construction time because the answer comes back
  synchronously and the editor is live again by the time the test could look.
- **Do not let a `QThread` be collected in a test.** The editor holds the thread
  and the worker on `self` and nulls both from `thread.finished`; a test that
  drops the reference destroys a running thread, which aborts the interpreter
  rather than failing an assertion.
- **`FakePopen` replaces `subprocess.run`.** `shared/ffmpeg.py:_run_ffmpeg`
  drives ffmpeg through `Popen` so a cancel can terminate it, which means the
  mock seam in `test_ffmpeg.py` is a fake process class, not a fake
  `CompletedProcess`. `Encoding.poll_count` models a clip that is still running,
  which is what makes a mid-clip cancel reachable. `_CANCEL_POLL_SECONDS` is
  monkeypatched to 0 so the poll loop does not put 50 ms in the suite.
- **The suite mocks ffmpeg, so it cannot catch a broken `Popen`.**
  `tests/real_ffmpeg_check.py` (not named `test_*.py`, so pytest skips it) runs
  the export against the bundled binary: a clean batch, a cancel part-way
  through, the resume that follows, and a failing clip. It is the only thing
  that executes the terminate path against a live encoder. Run it by hand after
  touching `shared/ffmpeg.py`.
- **A green suite is not evidence that a platform works.** `FakeBridge` stands in
  for libmpv by design, so the suite passes on a machine with no libmpv at all,
  and every macOS/Linux test monkeypatches `platform.system` rather than running
  on that platform. The suite covers *resolution*; playback on macOS and Linux
  can only be confirmed by a person on that machine — see
  [source-install.md](source-install.md#what-has-not-been-verified).
- **Cross-platform behavior is asserted by table completeness, not by the host.**
  The per-OS tables in `shared/environment.py` are checked for holes (every
  platform has a video output, a libmpv filename, and — if it does not bundle —
  search prefixes) because that is what fails when someone adds a platform.
  Asserting `video_output() == MPV_VIDEO_OUTPUT[platform.system()]` instead
  indexes the same dict with the same key and cannot fail on any host, which is
  why it is not the test.
- **Where a stdlib behavior differs by platform, spy instead of provoking.**
  `ntpath.commonpath` folds case and `posixpath.commonpath` does not, so
  `test_sources.py`'s case-sensitivity tests assert which comparisons
  `_is_inside` *tried* rather than provoking a real refusal — the one that must
  not move cannot be reproduced on a Windows machine.

## The documentation guard

`tests/test_docs.py` exists so the split of `AGENTS.md` into `docs/` cannot rot:

- every `docs/**/*.md` in the tree is linked from both `AGENTS.md` and
  `docs/README.md`, so a new document cannot be orphaned and the two indexes
  cannot drift apart;
- every relative markdown link in `AGENTS.md`, the documents, and the two
  project readmes resolves on disk, and resolves to a `.md` file — that last rule
  is what keeps source files referenced as backticked paths;
- every `tests/test_*.py` path named in `AGENTS.md` or a doc still exists, so a
  renamed test file is caught in the same change that renames it;
- `AGENTS.md` stays under a line ceiling, because the reason it was split in the
  first place is that it had grown to ~1000 lines of force-loaded context.

Markdown links between docs are the *only* thing the link test resolves. Source
files are referenced as backticked repo-root paths, deliberately not as links, so
moving a file does not force a documentation edit. `tests/test_*.py` paths are the
one exception, because a test file is a claim about coverage that is cheap to
keep true.
