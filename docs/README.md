# Documentation

`AGENTS.md` holds the short orientation an agent needs before it starts
(what the project is, where things live, the invariants). This directory holds
the detail, one concern per document, so an agent can load exactly the part it
needs.

Start at [AGENTS.md](../AGENTS.md) if you are an agent; start at the table below
if you are looking for one specific thing.

## The documents

| Document | Read it when you need to know... |
|---|---|
| [architecture.md](architecture.md) | how the app is put together: the annotated repository layout, the one-process-per-window model, the main menu, how the source video travels between windows, the shared library and each module's role, the MpvBridge pattern, the splash flow, and diagnostics. |
| [segment-model.md](segment-model.md) | anything about segments and the editor's Active Segment: the `.cmct` format, `SegmentModel` operations, the state machine and its buttons, the boundary peek, tag locks, what End Seg does in each case, and the required-record-field rule. |
| [scanner.md](scanner.md) | the Segment Scanner: the 2-minute preview, the two marker timelines, `blackdetect` detection and the slider mapping, Test Scan vs. Finished, and the hand-off to the editor. |
| [naming-and-organization.md](naming-and-organization.md) | how a tagged segment becomes a path: file naming scheme syntax, folder organization scheme syntax, the shared parser, sanitation and path safety, the export pipeline, and the Settings window that edits both schemes. |
| [packaging.md](packaging.md) | the portable Windows build and everything that only breaks when frozen: the two roots, the `.ui` payload layout, child windows as re-executions, Windows DLL loading, and what the build checks. |
| [source-install.md](source-install.md) | running commcut from source on macOS or Linux instead of the packaged build: where ffmpeg, ffprobe and libmpv come from, the `COMMCUT_MPV_LIB` override, and the assumptions that have not been verified. |
| [testing.md](testing.md) | how to run the suite, the shared widget/`FakeBridge` harness, which test file covers which area, and the Qt and import gotchas that make a test meaningful. |
| [status.md](status.md) | what exists today, what is next, and the known gaps — read this before building something that may already exist. |

Background material, not engineering documentation:

| Document | What it is |
|---|---|
| [design_legacy.md](design_legacy.md) | the original scope/design notes |
| [guides/filler_and_you.md](guides/filler_and_you.md) | the user-facing guide to acquiring and organizing filler |
| [../README.md](../README.md) | the project spec, and the source of truth for scope |
| [../packaging/README.md](../packaging/README.md) | build instructions and the smoke-test checklist |

## Conventions

- **One concern per document.** Each document opens with a one-line purpose and
  an `Applies to:` list of the source paths it describes, so you can tell
  whether it is the right one before reading it.
- **Documents are edited in the same change as the code they describe.** A
  behavioural change that invalidates a claim makes that claim wrong; fix it in
  the same commit rather than leaving it to rot.
- **Link between documents with markdown links; refer to source files as
  backticked repo-root paths.** `tests/test_docs.py` resolves the first kind, so
  a renamed or moved document breaks a test instead of silently misleading an
  agent. Source paths are deliberately *not* links — the one exception is
  `tests/test_*.py`, which is a claim about coverage cheap enough to keep true.
- **`AGENTS.md` stays short.** It is force-loaded into every session. The line
  ceiling in `tests/test_docs.py` enforces that, and detail belongs here.
- **Prefer a code reference over a prose restatement.** Where a rule has an
  owner in the code — `missing_required_tags`, the filename compiler, the folder
  compiler, `launch_command` — the document should point at it rather than
  describe a second copy that can drift.
