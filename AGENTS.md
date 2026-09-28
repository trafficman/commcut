# AGENTS.md

Orientation for AI coding agents working on the **commcut** project: what it is,
where things live, the rules that silently break a build, and an index into
`docs/`. The detail lives in `docs/` — start at
[docs/README.md](docs/README.md) and read the one document that owns the area you
are changing. Keep this file short; it is loaded into every session.

## What this project is

**commcut** is a Python/PySide6 desktop app for managing a personal library of
"filler" clips (commercials, promos, bumps) ripped from television. The full
vision, described in [README.md](README.md), has three pieces:

1. **Library organization** — a folder/naming scheme driven by per-clip tags
   (Title, Network, Block, Filler Type, Year, Time Period, Show, Special,
   Length, Information).
2. **Rename Wizard** — batch-rename already-cut clips. Not built.
3. **Editing Wizard** — load a compilation video, auto-detect boundaries, let
   the user review/adjust, and cut each segment out as a separate file. This is
   the current focus.

The tree holds the Editing Wizard (`editor/`), its Segment Scanner pre-process
(`scanner/`), the source picker that fronts them, the Settings window, and the
`shared/` library they build on. [docs/status.md](docs/status.md) says exactly
what exists today and what is next — read it before building something that may
already exist.

## Commands

Run from the project root.

| Task | Command |
|---|---|
| Run the app | `python main.py` |
| Run one window on its own | `python editor/editor.py <video>`, `python scanner/scanner.py <video>` (each window is its own process; `<video>` is optional) |
| Tests | `python -m pytest` — one file: `python -m pytest tests/test_paths.py` |
| Build the portable app | `python packaging/build.py` — Windows only; see [packaging/README.md](packaging/README.md) |
| Cut a release | set `VERSION` in `shared/version.py`, then `git tag v<version> && git push origin v<version>` — CI builds it and attaches a draft release; see [docs/packaging.md](docs/packaging.md#releases) |
| Run on macOS or Linux | Same, from a clone, with ffmpeg/mpv installed — see [docs/source-install.md](docs/source-install.md) |

There is no pytest config: `tests/conftest.py` puts the project root on
`sys.path` and redirects the log out of the source tree. Widget tests set
`QT_QPA_PLATFORM=offscreen` themselves.

## Layout

```
commcut/
├── main.py                  # entry point: argv dispatcher (--window <name>) + main menu
├── mainwindow.py/.ui        # main menu (Editor, Settings)
├── bin/<os>/                # bundled ffmpeg, ffprobe, libmpv (Windows only)
├── import/                  # the only videos that can be opened; user drops them in
├── export/                  # named clips are written here
├── temp/                    # scratch (the scanner's 2-minute preview)
├── commcut.log              # beside the exe; override with COMMCUT_LOG
├── packaging/               # PyInstaller spec + build script (docs/packaging.md)
├── .github/workflows/       # a pushed tag builds the Windows release zip
├── picker/                  # source video picker — the front door of the wizard
├── scanner/                 # Segment Scanner: detect boundaries, hand off
├── editor/                  # Editing Wizard: segments, tags, export
├── settings/                # file + folder scheme editors
├── shared/                  # the library all four windows build on
├── tests/                   # pytest suite (docs/testing.md)
├── docs/                    # the documents indexed below
├── experiments/             # code answering a question the docs could not;
│                            # never shipped, never bundled
└── prototypes/              # historical; the active code is editor/ and scanner/
```

`shared/` in one line each: `environment` (roots, binaries, `launch_command`),
`version` (the release number `packaging/build.py` checks a tag against),
`diagnostics` (log/excepthook/fatal), `mpv` (MpvBridge, `BoundaryPreview`),
`timeline` (editor timeline), `segments` (`SegmentModel`, `.cmct`), `sources`
(the `import/` policy), `ffmpeg` (preview clip + named export), `scheme`/
`naming`/`paths` (the two schemes), `exporting` (the export planner),
`ui_loader` (promoted widgets). Per-module detail:
[docs/architecture.md](docs/architecture.md).

## Documentation

| Document | Read it when you need to know... |
|---|---|
| [docs/README.md](docs/README.md) | the doc index and the conventions this set follows |
| [docs/architecture.md](docs/architecture.md) | the annotated layout, the one-process-per-window model, the main menu, the source hand-off, the shared library, the MpvBridge pattern, the splash flow, diagnostics |
| [docs/segment-model.md](docs/segment-model.md) | the `.cmct` format, `SegmentModel`, the editor state machine and its buttons, the boundary peek, tag locks, End Seg, required record fields |
| [docs/scanner.md](docs/scanner.md) | the scanner: preview clip, marker timelines, `blackdetect`, Test Scan / Finished, the hand-off to the editor |
| [docs/naming-and-organization.md](docs/naming-and-organization.md) | file naming scheme, folder organization scheme, the parser, sanitation and path safety, the export pipeline, the Settings scheme UI |
| [docs/packaging.md](docs/packaging.md) | the portable Windows build, everything that only breaks when frozen, and the tag-driven release pipeline |
| [docs/source-install.md](docs/source-install.md) | running from source on macOS or Linux: where the binaries come from, the `COMMCUT_MPV_LIB` override, and what is unverified |
| [docs/testing.md](docs/testing.md) | how to run the suite, the widget/`FakeBridge` harness, which test file covers what, the Qt/import gotchas |
| [docs/status.md](docs/status.md) | what is built, what is next, known gaps |
| [docs/design_legacy.md](docs/design_legacy.md) | the original scope/design notes |
| [docs/guides/filler_and_you.md](docs/guides/filler_and_you.md) | user-facing guide to acquiring and organizing filler |

## Non-negotiable invariants

Each of these fails silently in a packaged build or in a way the user cannot
diagnose. The linked document has the full reasoning.

1. **Resources resolve through `resource_path()`.** Never `SCRIPT_DIR` or
   `__file__` — frozen they point into the payload, which is wiped on exit.
   → [docs/packaging.md](docs/packaging.md)
2. **Frozen has two roots.** `resource_root()` is the read-only payload;
   `install_root()` is beside the exe and holds `bin/<os>/`, `settings.json`,
   `import/`, `export/`, `temp/`, `commcut.log`. Nothing that must survive goes
   in the payload. Unfrozen they are the same folder, so a source install on
   macOS or Linux writes to the clone. → [docs/packaging.md](docs/packaging.md)
3. **`launch_command(name, *args)` is the only way to open a window**, and its
   `*args` are forwarded verbatim into that window's `run(*args)`. A window's
   `__main__` must therefore `sys.exit(run(*sys.argv[1:]))`; dropping the
   argument opens a *different* video. → [docs/packaging.md](docs/packaging.md)
4. **A window folder can shadow its own package.** `scanner/` has no
   `__init__.py`, so a sibling import must be guarded on `__package__`, and the
   test for it must run in a **fresh interpreter**. →
   [docs/packaging.md](docs/packaging.md)
5. **Binaries resolve by absolute path, per platform, and libmpv is loaded
   and named before `import mpv`.** `get_binary_path` searches `bin/<os>/` and
   then, only on platforms that do not bundle (`_BUNDLED_BINARY_PLATFORMS`), the
   absolute prefixes in `_SYSTEM_BIN_PREFIXES`. A bundling platform must
   **refuse** rather than fall through to a system copy. On Windows the `PATH`
   prepend and the `os.add_dll_directory` handle stay load-bearing, and the
   handle must stay alive (a dropped one surfaces later as a bare `OSError` from
   ctypes). On macOS `find_library` ignores `PATH` and **raises** rather than
   falling back to an already-loaded image, so `mpv_import_context` loads the
   resolved library and answers the lookup for it.
   → [docs/packaging.md](docs/packaging.md),
   [docs/source-install.md](docs/source-install.md)
6. **Close the splash before constructing any mpv-backed widget.** A splash is a
   top-level window, and constructing a player while another one is foreground
   has deadlocked mpv's renderers.
   → [docs/architecture.md](docs/architecture.md)
7. **Never construct a second mpv player while another top-level window is
   foreground.** Every window is its own process for exactly this reason; the
   main menu stays open in the background rather than hosting a child. The
   deadlock was found on Windows with direct3d and is not proven absent on
   other platforms, so the rule is unconditional.
8. **The source video path is an argument, not shared state.** It travels
   `main menu → picker → scanner → editor` through argv; a window never
   re-resolves a default of its own. → [docs/architecture.md](docs/architecture.md)
9. **One owner per rule.** Required tags live in
   `shared/exporting.py:missing_required_tags`; the filename and folder
   grammars live in `shared/naming.py` and `shared/paths.py`. Never add a second
   implementation of a rule that already has one. →
   [docs/naming-and-organization.md](docs/naming-and-organization.md)
10. **Editing writes are all-or-nothing.** Stage and Export refuse rather than
    persist an incomplete record, and ignored segments are exempt from the
    required tags. A run that did not finish — cancelled, or left with failed
    clips — keeps the clips it committed, and the only destinations it may later
    skip are the ones that same session's own run wrote. →
    [docs/segment-model.md](docs/segment-model.md)
11. **Every child process carries `**no_console_kwargs()`.** A `console=False`
    build has no console, so Windows gives each ffmpeg, ffprobe, and window
    launch a *visible* console window — and it is invisible from a source run,
    where the child joins the developer's terminal. The rule's one owner is
    `shared/environment.py:no_console_kwargs`; `tests/test_frozen_mode.py` sweeps
    the app's modules and fails without it. → [docs/packaging.md](docs/packaging.md)

## Working agreements

- **Run the suite** (`python -m pytest`) before reporting a task done, and say
  what you ran.
- **A bug fix ships with a test** that drives the shipped code. For editor
  behavior that means the real `MediaPlayer` through
  `tests/editor_stub.py`, not a re-implementation —
  [docs/testing.md](docs/testing.md) has the harness and the gotchas.
- **Update the owning document in the same change** as any behavior it
  describes. If a doc and the code disagree, one of the two is a bug; find out
  which before editing either.
- **Do not add comments to code** unless asked. The code documents itself and
  the docs carry the rationale.
- **Do not commit** unless asked.

## Documentation conventions

- **Markdown links between documents; backticked repo-root paths for source
  files.** `tests/test_docs.py` resolves the links (so a moved or renamed
  document breaks a test) and deliberately does not resolve source paths, so
  moving a file does not force a documentation edit. `tests/test_*.py` is the
  one exception: it is a coverage claim, and it is checked.
- **Each document opens with a purpose line and an `Applies to:` list** of the
  source paths it describes, so you can tell whether it is the right one before
  reading it.
- **`AGENTS.md` stays under 240 lines.** The ceiling is enforced by
  `tests/test_docs.py`; new detail goes in `docs/`, not here.
