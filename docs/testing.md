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
imported more widely (`test_main_window.py`).

**A modal dialog reached from a test has to be answered, not shown.** The menu's
Editor button opens a `QFileDialog`; the mesh wizard asks a `QMessageBox` before
committing a conflicting tag; the Settings close handler can raise one while a sync
is running. A real one under `QT_QPA_PLATFORM=offscreen` blocks on nobody and hangs
the run rather than failing. Each is stubbed in its own fixture — the dialog through
a module-level `choose_source_video` seam and a `QFileDialog` subclass, the boxes
through recorders — and `tests/test_settings.py`'s fixture drains any run left in
flight so teardown does not close a window that is mid-something.

**The stub's widgets must match the classes the `.ui` file declares.** The nine
suggestable tag fields are editable `QComboBox` and Title is a `QLineEdit`, both
built in `EditorStub.__init__` rather than loaded from `editorwindow.ui`. That is
not tidiness: a stubbed `QLineEdit` everywhere would let the locks, required-tag
and export suites stay green after the shipped window switched to combos, which
is exactly the conversion those suites exist to catch — and the required-field
outline is the quietest version of it, because a QSS selector that matches
nothing is not an error, it just removes the warning.

For the same reason `test_editor_vocabulary.py` loads the real `.ui` through the
app's own `UiLoader` and asserts the classes and properties there. The stub
cannot see the form; only that test can.

Each stub instance points its tag vocabulary at its own `mkdtemp` folder, so a
suite run cannot write to the real `install_root()` and no two editors share
state.

## Which file covers what

| File | Covers |
|---|---|
| `test_boundary_preview.py` | the boundary peek: the seek sequence, and every rule that cancels a pending one |
| `test_records.py` | the clip record: the XML format, its reader, and the atomic publish |
| `test_vocabulary.py` | `vocabulary.json`: the shipped defaults, the unusable-file fallbacks, the dedup rule, the atomic write, and `prune_to` |
| `test_catalog.py` | the library walk: what counts as a clip, what is ignored, what is reported (by code as well as by sentence), progress, cancel, and the record-over-filename guard. Also the vocabulary sync: union, prune, the empty-library and cancelled-write rules, and idempotence |
| `test_mesh.py` | the Mesh Wizard's model: that a fresh session is empty even when every folder name matches exactly, one answer per folder name, the most-open-path-first sequencing, conflicts, derived tags, the alias table, the report, and the learned rules — including that a literal already in the table is refused and that a rule may not target `title` |
| `test_mesh_window.py` | the wizard window: the coloured path bar, both questions, Assign disabled until both are filled, the conflict asked before it is committed, the vocabulary sync summarised on screen, the hand-off to the queue, and the close guard |
| `test_queue.py` | the Library Mesh Tag Editor: resume (including that a partial record is not "done"), the folder answers and the learned rules reaching the form, Add Title, Next writing a record `build_catalog` reads back, Skip - Delete, the report's three cases, and the player |
| `test_importing.py` | the importer backend: records becoming candidates, an imported clip landing where export would put it, per-clip skipping, skip-if-identical against a library built by really importing, the three transfer modes, the space preflight, cancel and resume, untagged discovery, and `match_value` |
| `test_tag_form.py` | the shared tag form: what the fields read back, the dropdown ordering, the empty-list placeholder, the required-field outline, and that the editor's `.ui` does not draw its own grid |
| `test_queue.py` | the Library Mesh Tag Editor: resume (including that a partial record is not "done"), the folder answers and the learned rules reaching the form, Add Title, Next writing a record `build_catalog` reads back, Skip - Delete, the report's three cases, and the player |
| `test_editor_vocabulary.py` | the tag dropdowns: what they offer, the most-recently-used ordering, what counts as "used", and that a refresh cannot eat a value being typed |
| `test_editor_locks.py` | tag-lock display, pinned-value semantics, locked-only segment carry-over |
| `test_editor_required_tags.py` | front-end enforcement of the four required fields, including refusal to write |
| `test_end_boundary.py` | `place_end_boundary` — insert vs. move, and the refusal guards |
| `test_timeline_zoom.py` | zoom state surviving a resize, and the editor toggle agreeing with the widget |
| `test_sources.py` | which videos may be opened: a real video from any folder is accepted, a missing file / a folder / a non-video is refused in words, and the writable-folder rule behind the `.cmct` written beside the source — including the probe itself |
| `test_source_handoff.py` | the chosen source surviving every window-to-window hand-off, and both builders requiring it |
| `test_main_window.py` | the main menu, the native file dialog (filter, native-ness, cancel), the launcher, and the failure paths |
| `test_frozen_mode.py` | frozen roots, per-platform binary and libmpv resolution, table completeness, mpv `vo`, child-window argv, spec/`resource_path` agreement, and the sweep that every spawn site carries `no_console_kwargs()` |
| `test_mpv_player.py` | how the player is built: libmpv loaded before the import that needs it, the native handle, and the zero-handle refusal (no real player) |
| `test_scheme.py` | strict scheme parsing |
| `test_naming.py` | filename rendering, including the README pattern |
| `test_paths.py` | strict folder scheme compilation, rendering, and sanitation |
| `test_exporting.py` | export settings, destination planning, preflight, resume skips, the export folder, and the two destination helpers the importer shares |
| `test_ffmpeg.py` | plan-based ffmpeg execution: progress, cancel, partial-failure reporting, the encoder probe, and the no-console flag reaching `Popen` (`Popen` mocked) |
| `test_editor_export.py` | the export worker thread, progress dialog, cancel, resume, close-mid-run, and the summary screen (window wiring with a substituted dialog, plus the shipped dialog itself) |
| `test_settings.py` | the Settings window: defaults, previews, atomic save, help panels, and the vocabulary sync button (thread, worker, progress dialog, and result dialog substituted) |
| `test_docs.py` | the documentation guard (see below) |

## Gotchas that decide whether a test means anything

- **A resized widget must be visible.** Qt delivers `resizeEvent` to a
  *visible* widget only, so any test that resizes a widget to check zoom
  behavior has to `show()` it first. Otherwise the assertion passes for the
  wrong reason.
- **An unpatched `QMessageBox` hangs the run, it does not fail it.** `warning()`
  and `question()` block in a nested event loop waiting for a click, so a test
  that forgets to stub one does not fail — the whole run stops, with no output
  after the last passing test and nothing to suggest why. `Shell.open_safely`
  reports failures with `QMessageBox.warning`, so any test that calls it has to
  stub that out (`monkeypatch.setattr(QMessageBox, "warning",
  staticmethod(lambda *a: None))`), exactly as it would stub a subprocess.
- **A synthetic click is not evidence that input works.** `QTest.mouseClick`
  posts its event straight to the widget, so it bypasses the OS input path and
  reports a window as responsive when a person finds it frozen. It is good for
  "does this button open that window" and useless for "does this app respond to
  the mouse". The latter needs a person; see
  [experiments/README.md](../experiments/README.md#mpv_teardown).
- **Closing a window needs its deferred delete delivered.** `WA_DeleteOnClose`
  destroys the C++ object through a `DeferredDelete` event, which
  `processEvents()` does not reliably deliver. A test that closes a window and
  only pumps the loop will sometimes assert against a stack that has not been
  updated yet — and then pass on the next run. `tests/test_session.py`
  `sendPostedEvents(None, QEvent.DeferredDelete)` explicitly, which is what makes
  it deterministic.
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
- **A stubbed thread cannot prove a thread stops.** The mesh and queue suites run
  their workers through `FakeThread` — `start()` emits `started` and `finished`
  by hand on the GUI thread — plus a worker whose `moveToThread` is a no-op, and
  `deleteLater()` that sets a flag. That is what makes them fast and
  deterministic, and it is exactly why a worker thread which *never terminates*
  passed every one of them: a faked thread has no `exec()` loop left spinning, so
  there is nothing to keep alive, and no C++ object left to destroy, so there is
  nothing to delete. Both windows really had that bug and shipped.

  So each of those files also has a `real_thread_*` fixture that keeps the real
  `QThread`, the real worker and the real event loop, and stubs only what needs
  hardware or a subprocess (mpv, ffprobe). Those tests pump the loop until the
  thread stops and assert `isRunning()` went false, and that the window's
  `closeEvent` guard came off. Two details make a regression legible instead of
  fatal:

  - the fixture stubs `deleteLater` on the thread, because with the bug present
    the teardown deletes a *running* `QThread` and aborts the process — so
    without that stub the regression is a dead test run with no report instead of
    a failed assertion;
  - teardown calls `quit()`/`wait()` on anything still running, so a failing test
    cannot leave a spinning thread for Qt to destroy at interpreter shutdown.

  Assert the *effect* (`isRunning()` false), never the wiring — `thread.quit`
  being connected is an implementation detail, and the previous bug was invisible
  precisely because nothing checked the effect.
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
