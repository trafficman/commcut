# Packaging and frozen mode

How the Windows build is assembled, and the rules that only bite once the app
is frozen: the two roots, the read-only payload layout, and per-platform library
loading.

Applies to: `packaging/commcut.spec`, `packaging/build.py`,
`packaging/source_release.py`, `packaging/README.md`, `shared/environment.py`,
`shared/version.py`, `shared/session.py`, `shared/splash.py`,
`assets/commcut_banner.png`, `main.py`, `.github/workflows/release.yml`,
`.github/workflows/source-release.yml`, `tests/test_frozen_mode.py`,
`tests/test_release_build.py`.

Related: [source-install.md](source-install.md) (how you run commcut on macOS or
Linux, which this build does not cover), [architecture.md](architecture.md) (the
same code unfrozen — one process, one visible window, diagnostics),
[testing.md](testing.md) (how frozen behavior is tested without building an exe).

## The distributable

`packaging/build.py` produces `dist/commcut-portable/`:

```
commcut.exe   46 MB  self-extracting (Python + PySide6 + app + the payload files)
bin/win/            ffmpeg.exe, ffprobe.exe, libmpv-2.dll -- NOT inside the exe
import/             drop finished clips in here to import them; a source
                    video is picked from anywhere with a file dialog
export/             named clips are written here
```

~412 MB total. It is a **portable smoke-test build**, not a release: no
installer, no shortcuts, no uninstaller, no signing. See
`packaging/README.md` for build instructions and the smoke-test checklist.

`python packaging/build.py` is the whole build; `--onedir` produces the faster
folder form, and `--check-only` runs just the pre-flight checks.

**`bin/win/` is deliberately not bundled into the exe.** A onefile build
re-extracts its whole payload on every launch, so bundling ~366 MB of binaries
would mean extracting a third of a gigabyte every time commcut started. Kept
beside the exe, the payload is only ~46 MB. `build.py` copies `bin/win/` in
beside the exe, which is what makes the layout below work.

The conclusion is unchanged from when each window was its own process, but the
reasoning moved: back then one editing session paid that extraction **four
times**, once per window. It now pays it once. That is a better argument for
the same layout, not a reason to revisit it — a 412 MB onefile is still a third
of a gigabyte on every start.

Every runtime path resolution in the app therefore has to work from two
different places — the source tree and the frozen install folder. The next four
sections are how that is arranged; each of them is load-bearing and none of them
fail from source.

## The two roots

Unfrozen there is one root and the distinction is academic. Frozen there are
two, and conflating them is the most common way this app breaks in a build:

| | Resolves to (frozen) | Holds |
|---|---|---|
| `resource_root()` | `sys._MEIPASS` | the read-only payload: the `.ui` files and the splash banner |
| `install_root()` | `dirname(sys.executable)` | `bin/<os>/`, `settings.json`, `import/`, `export/`, `temp/`, `commcut.log` |

The payload directory is **wiped on exit**, so it is never the place for
anything that has to survive. `install_root()` is what `settings.json` and
every export resolve against.

`_bin_dir()` used to derive from `__file__`, which frozen points into the
payload — it now branches on `sys.frozen` and resolves against
`install_root()` instead.

## `contents_directory="."` (onedir only)

PyInstaller 6 onedir splits its output: the exe lands in `dist/commcut/` but
data goes to `dist/commcut/_internal/`. Since `bin/<os>/` is resolved against
`dirname(sys.executable)`, the default layout would make the app look for
ffmpeg next to the exe and not find it. The spec sets `contents_directory="."`
so the payload sits beside the exe. Onefile has no contents directory and
ignores it. `packaging/build.py` asserts `_internal/` is absent.

## Read-only payload files keep their source subfolders

`resource_path()` takes a project-root-relative path and is called with the
same expression whether or not the app is frozen — `resource_path("settings",
"settingswindow.ui")`. Flattening the payload files into the payload root
would make that correct only in a packaged build and wrong from source, so the
spec mirrors the source layout instead: `mainwindow.ui` at the payload root and
`editor/`, `scanner/`, `settings/`, `assets/` beside it.
`tests/test_frozen_mode.py::test_source_and_payload_layouts_agree` reads the
spec's `datas` list and compares it against the code's view, so a file that
moves cannot be silently mis-bundled.

**`assets/commcut_banner.png` is payload data of exactly the same kind.** It is the
logo `shared/splash.py` draws, resolved with `resource_path("assets", ...)` and
listed in the spec's `UI_DATAS`, so it is covered by the agreement test above as
well. It is listed separately from the `.ui` files in
`tests/test_frozen_mode.py:BANNER_FILES` so the tree walk that requires *every*
`.ui` to be listed does not become a claim about every image in the tree — a
screenshot in `docs/` is not something a build should be shipping. What that
separate list does buy is the guarantee that everything the app resolves through
`resource_path()` is in `PAYLOAD_FILES`, and `build.py:_verify_payload` checks the
same list after the build, so a missing banner is caught by the build rather than
by a user looking at a blank splash.

**Do not use `SCRIPT_DIR` for a resource.** It is
`dirname(os.path.abspath(__file__))`, which is only meaningful unfrozen; frozen
it points into the payload. Use `resource_path()`.

## There is one entry point, and it is not re-executed

`main.py` is the only entry point in the spec, and the only one on disk. Every
window is built in-process by `shared/session.py:Shell.open`, which imports the
window's `create(...)` builder lazily and shows it. There is no `--window` flag,
no per-window script, and nothing to dispatch.

This was not always true, and the reason is worth keeping because the frozen
build is where it used to bite. Windows used to be separate processes because
constructing an mpv player (direct3d) while another top-level window is
foreground was believed to deadlock; `shared/environment.launch_command(name,
*args)` returned `[sys.executable, "--window", name, *args]` and `main.py`
routed it to that window's `run(*args)`. That whole arrangement — the argv
forwarding, the `if __name__ == "__main__":` blocks, and the folder-shadowing
guard below — was there to make a re-executed binary find its own windows, and
all of it is gone with the process model. `experiments/mpv_foreground/` tested
the claim it rested on and found no hang in 120 runs; see
[architecture.md](architecture.md) and
[experiments/README.md](../experiments/README.md).

One consequence for the spec was favourable. Under onefile, PyInstaller
extracts the whole payload on **every** launch, so the old model extracted it
once per window — four extractions for one editing session. There is one now.

### The windows are named in the spec, not found by it

The second consequence broke the build, and it is the reason this section
exists.

**The three windows are in `hiddenimports` because nothing in the source imports
them in a way PyInstaller can see.** `shared/session.py:_BUILDERS` maps a name
to a `(module, builder)` pair and `Shell._resolve` loads it with
`importlib.import_module(module_name)` — where `module_name` is a variable.
modulegraph cannot follow that. Its `_Visitor` implements `visit_Import` and
`visit_ImportFrom` and aliases every other expression node to a no-op:

```python
visit_Call = visit_Expression     # line 899

def visit_Expression(self, node):
    # Expression node's cannot contain import statements or
    # other nodes that are relevant for us.
    pass
```

So a `Call` node is discarded, and the four window modules are absent from the
graph. Not "absent from the payload when the name is a variable" — absent
*either way*, because a string literal would not help.

**What made this invisible is that the windows used to be entry points.** Under
the process model the spec's `Analysis` listed all five scripts, so the windows
were bundled by construction and nothing had to import them. Deleting the
per-window entry points is exactly what broke the build, and the lazy import that
replaced them is the one shape of import the analysis cannot perform. Nothing in
`AGENTS.md` or [architecture.md](architecture.md) said the entry points were
load-bearing for packaging; they were load-bearing for both reasons at once.

The failure is the quiet kind. `mainwindow` is a normal import and
`mainwindow.ui` is in `datas`, so the exe starts and shows a menu with two
buttons on it. Both call `Shell.open_safely`, which catches the
`ModuleNotFoundError` and reports it as *"The settings window could not start"* —
so the app is a menu whose every button is dead, and it says so in a dialog
rather than in a traceback. `build.py`'s post-build checks are about the `.ui`
files and `bin/` and never look at the module set; `zip_portable` checks the
same six paths. **CI cannot catch it either**, because CI does not run the exe.

`tests/test_frozen_mode.py::test_every_window_the_shell_can_open_is_bundled`
parses the spec's `hiddenimports` and requires every module in `_BUILDERS` to be
in it. It is the module-side counterpart of
`test_source_and_payload_layouts_agree`, and it exists for the same reason: a
`.ui` file that moves cannot be silently mis-bundled, and neither can a window
that the shell can open but the spec has forgotten.

Two properties of the fix are worth keeping in mind. The four directories have
no `__init__.py` and are PEP 420 namespace portions, and `hiddenimports`
resolves a module inside one correctly — the build log prints `Analyzing hidden
import 'scanner.scanner'` and the module lands in the PYZ. And because the
hidden import is a real module rather than a name, its own imports are followed
normally: `scanner.marker_timeline` arrives with it, with no entry of its own.

**A folder named after an installed library cannot be imported.** `packaging/`
has no `__init__.py`, and the `packaging` that PyInstaller depends on is a real
package in site-packages. A regular package beats a namespace portion
*wherever* it is on `sys.path`, so `import packaging.build` binds to the
dependency and then reports no `build` inside it. This is permanent rather than
order-dependent, because PyInstaller means the installed one is always there.
`packaging/build.py` is therefore run as a script, and
`tests/test_release_build.py` loads it with
`importlib.util.spec_from_file_location`. PyInstaller's own `import packaging`
is unaffected and gets the dependency, which is what it wants anyway.

`scanner/` is a namespace portion for a different reason — it has no
`__init__.py` — and CPython ranks a regular module found **anywhere** on
`sys.path` above one. That used to break `from scanner.marker_timeline import
...` whenever the scanner was launched as a *script*, because `scanner.py` sat at
`sys.path[0]` and shadowed the package of the same name. Nothing launches a
window as a script any more, so the guard is gone. Do not reintroduce it by
adding a `__main__` block to a window; if a sibling import ever shadows a
package again, the fix is to stop running that module as a top-level script, not
to branch on `__package__`.

## Library loading, per platform

python-mpv resolves libmpv **for itself at import time**, via
`ctypes.util.find_library`, and how that lookup works is not the same on every
platform. This is why `create_mpv_player` loads the library itself before
importing python-mpv, and why that import is deferred at all.

**On Windows**, `find_library` scans `%PATH%` for each candidate name and
returns the first absolute hit, which it then loads with
`LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR`. So prepending `bin/<os>` to `%PATH%` is
**load-bearing**. `os.add_dll_directory` is registered too, and its handle is
kept in a module global — a dropped handle unregisters the directory and
surfaces much later as a bare `OSError` from ctypes with nothing pointing at
the cause.

**On macOS, `find_library` ignores `%PATH%` entirely.** It searches a fixed
list of system directories and returns a path only if that exact file exists.
A source install's libmpv is not there, so the `PATH` prepend buys nothing.

**And mapping the library first is not enough.** This is the non-obvious part.
python-mpv 1.0.8's POSIX branch asks `ctypes.util.find_library('mpv')` and
**raises if that returns `None`** — it never checks whether libmpv is already
loaded. `DYLD_LIBRARY_PATH` does not help either, because `find_library` looks
for a *file* at a fixed list of paths rather than asking the dynamic loader. So a
libmpv commcut can find and map is still one python-mpv refuses to load, and the
error tells the user to read the `ctypes.util.find_library` documentation.

`environment.mpv_import_context` therefore does both: it resolves libmpv itself
(`resolve_mpv_library` — `COMMCUT_MPV_LIB` override, then `bin/<os>/`, then the
system prefixes), maps it with `ctypes.CDLL(..., RTLD_GLOBAL)`, and **answers
`find_library` for exactly the names python-mpv asks for** (`_MPV_LOOKUP_NAMES`),
leaving every other name to the real function and removing the override on the
way out. On Windows that is redundant with the `PATH` prepend underneath, and is
applied anyway so the library bound is the one this module validated.

`create_mpv_player` enters the context around the `import` — not
`setup_environment`, so starting commcut and reaching the main menu never loads
libmpv. That property is now weaker than it was, and the difference is worth
being exact about: the menu is still the first thing to run and still loads no
mpv, but the first player window the user opens loads libmpv **into the menu's
process**, because there is only one process. Under the old model the menu could
never load mpv at all. `shared/session.py` is what preserves the first half — its
builders are imported lazily inside `Shell.open`, so importing a window module
at module level would drag libmpv in before the menu ever appears.

[source-install.md](source-install.md) owns the macOS and Linux end of this:
the search order, the `COMMCUT_MPV_LIB` override, and why `DYLD_LIBRARY_PATH` is
set but `LD_LIBRARY_PATH` is not.

`mpv`'s `vo` comes from `environment.video_output()` (per-OS), not a literal
`'direct3d'`. The `WA_NativeWindow` attribute on the video frame is required on
**every** platform, not just the one whose driver motivated it: `winId()` is what
produces the handle mpv embeds into, and without it `winId()` returns 0.

## Windowed-build diagnostics

The build is `console=False`, so there is no console; `shared/diagnostics.py`
is the only reporting channel and is documented in
[architecture.md](architecture.md#no-console-diagnostics).

The spec sets `disable_windowed_traceback=True` for the same reason: the
default makes the windowed bootloader pop a **modal** traceback dialog that the
process waits on, so an undismissable error looks exactly like a hang.

## No console windows

A windowed build has no console of its own, and on Windows that changes what a
child process gets. A **console** program started by a process that has no
console is handed a new, *visible* console window. ffmpeg and ffprobe are
console programs, so without a flag every one of their calls flashes a console
over the app — the scanner's preview clip, the editor's duration and keyframe
probes, `blackdetect` on Test Scan, the encoder check and every exported clip.

This is invisible from a source run: the developer is in a terminal, the child
joins that terminal's console, and no window ever appears. It only happens in
the packaged build, which is what makes it a packaging rule.

`no_console_kwargs()` in `shared/environment.py` is the single owner. It
returns `{"creationflags": subprocess.CREATE_NO_WINDOW}` on Windows and `{}`
elsewhere, and every `subprocess.run` / `Popen` in the app splats it in:

```python
subprocess.run(command, capture_output=True, text=True, **no_console_kwargs())
```

The flag gives the child a console with **no window**, so captured pipes and the
temporary stderr file in `_run_ffmpeg` keep working unchanged. It is a no-op on
a child that is not a console application. Every child the app starts now is an
ffmpeg or ffprobe call: the window launches that used to carry it are gone with
the process model, which also removes the "was that console from the launch or
from the window's own probing?" question.

The flag is unconditional rather than applied only when the current process
happens to have no console: every call site already captures its child's
output, so there is nothing to lose by never joining the parent console, and
"does this process have a console" is a ctypes question with a subtle answer
that call sites should not each have to get right.

`tests/test_frozen_mode.py` sweeps the app's modules with `ast` and fails if any
`subprocess.run` / `Popen` lacks the flag, because the failure mode is silent on
every machine a developer has. `core.py` is excluded from the sweep: nothing
imports it, PyInstaller never sees it, and it is kept only as history. Wiring it
back into a window would reintroduce the pop-ups without failing the sweep.

## Resolving the binaries at runtime

`get_binary_path(name)` returns the absolute path of a binary, and it is the
only way any call site gets one — a bare name resolves through a mutated
`PATH` and picks up whatever happens to be installed. The order is data
(`shared/environment.py`), not an if-chain:

1. `bin/<os>/<name><ext>`, always. On Windows this is where the bundled binary
   is, so it wins.
2. Only on a platform **not** in `_BUNDLED_BINARY_PLATFORMS`, the absolute
   prefixes in `_SYSTEM_BIN_PREFIXES` — a candidate has to be an executable file
   to count. macOS and Linux are *source* installs, so this step is their only
   step; see [source-install.md](source-install.md).
3. Otherwise `FileNotFoundError`, naming every location searched.

A bundling platform must **refuse** rather than fall through. A packaged build
that silently used a system ffmpeg would be running a binary nobody tested,
which is a far worse failure than a missing file — and it has a known one
bundled, so there is nothing to gain. macOS and Linux are the mirror image: the
user supplied the ffmpeg, so "whatever the user installed" is the intent.

The libmpv load is separate from this, and happens at `import mpv` rather than at
`get_binary_path` time. See "Library loading, per platform" above.

## What the build checks

Pre-flight, `build.py` refuses to build when the host is not Windows, or when a
bundled binary is a Git LFS pointer file rather than a real binary. The Windows
gate stays because there is no macOS or Linux *build*: those platforms ship as
source releases — see [source-install.md](source-install.md) — and a frozen
macOS build would put `install_root()` inside a signed `.app` bundle, which is
read-only. `packaging/source_release.py` has no such gate, because a source
archive's contents are platform-independent; it refuses only on the *name*, and
`--target` overrides even that so the archive can be built and inspected from a
Windows checkout.

Post-build, it asserts `_internal/` is absent, and that `prototypes/` and
`tests/` were not bundled. `docs/` is not bundled either — the spec's `datas`
list names only the read-only payload files, so documentation never reaches the
payload. The payload layout itself cannot be checked on disk for a onefile build
(it unpacks at run time), so `tests/test_frozen_mode.py` asserts it instead.

`--zip` adds one more check, and it is the only one that looks at a *shipping*
artifact. `zip_portable` requires the exe, all three `bin/win/` binaries, and
both placeholder `README.txt` files to be present before it writes anything, and
refuses with the missing paths named. That is load-bearing for onefile, where
nothing else inspects the result: an archive missing its binaries installs
cleanly and then fails the first time it tries to cut a clip, with no useful
error. It holds for `--onedir` too, so one check covers both.

## Releases

`.github/workflows/release.yml` builds this artifact on a tag push and attaches
it to a **draft** GitHub release. Windows only; `check_platform()` is what stops
a build for anything else, and `build.py --version` is the only caller that
passes a version at all.

```
git tag v0.1.0 && git push origin v0.1.0
```

Draft, not published, because the exe has not run on a real machine yet and a
GitHub release cannot be re-cut for a tag that is already published. The assets
are downloadable from a draft, so smoke testing needs nothing changed. Workflow
inputs (`workflow_dispatch` with a `tag`) re-cut a release for a tag that
already exists, and a run that finds a release already there is a green no-op
rather than a failure — re-running a workflow is a normal thing to do.

**The tag is not trusted to be the version.** `shared/version.py` holds
`VERSION`, and `build.py --version` refuses anything that disagrees with it, so
a mistagged release fails before a 400 MB build happens instead of producing a
release page that contradicts the code inside it. A `v` prefix is stripped, so
`v0.1.0` and `0.1.0` are the same release; the constant stores the bare number
and the prefix is a tag convention only. The check is the reason the constant
exists in the codebase rather than living in the workflow.

**The archive is `commcut-<version>-windows-x64.zip`** plus a `.sha256`
sidecar, holding a single `commcut-<version>/` folder rather than a flat tree —
so extracting does not scatter the app across the folder the user extracted
into, and two releases extracted side by side do not merge each other's
`settings.json` and `import/`. Without `--version` it is
`commcut-portable-windows-x64.zip`, which is the local smoke-test form and does
not require editing the source. `tests/test_release_build.py` covers the layout,
the required entries, the sidecar, and the version rule.

Deflate is doing real work here: the ~412 MB folder lands at roughly 190 MB,
comfortably under the 2 GiB per-asset limit, and zipping it takes about twenty
seconds. The bytes are not reproducible — PyInstaller embeds a build timestamp —
but the *entry order* is, so two archives of one tree diff on their contents
rather than on their ordering.

Two things in the workflow are load-bearing. **`lfs: true`**: `bin/win/` is Git
LFS, ~384 MB, and without it the checkout has pointer text where the binaries
go — caught loudly by `check_binaries` rather than shipped. **`ref:`**: on a
manual re-run `github.ref` is the branch the workflow ran from, so without
`ref: ${{ inputs.tag || github.ref }}` the re-cut would build a different commit
than the tag it is releasing. The suite runs first and gates the build; it needs
none of the LFS binaries, since ffmpeg is stubbed and the mpv import is
deferred and mocked.

What CI does **not** do is run the built exe. It cannot: `build.py`'s layout
check and the workflow's sha256 comparison say the archive is complete and
internally consistent, not that the player renders. That is a person on a
Windows machine, which is the same reason `tests/real_ffmpeg_check.py` is run by
hand. The runner is `windows-2022` and pinned, deliberately: `windows-latest`
moves, and a different Visual Studio image means a different bundled
`vcruntime140.dll` in the exe.

The exe is **unsigned**, so SmartScreen shows "Windows protected your PC" and
the user has to choose *More info → Run anyway*. Every release until signing is
added will do this.

### The source releases, and why they are a second workflow

The same tag push also runs `.github/workflows/source-release.yml`, which builds
`commcut-<version>-source-<os>-<arch>.tar.gz` for macOS and Linux and attaches
it, with its sidecar, to the **same** draft release. It is a separate workflow
rather than a third leg on this one, for two reasons: this job is the only path
that has ever shipped and should not be restructured to accommodate two
platforms that need none of its machinery, and the preconditions are opposites —
this one needs Git LFS and PyInstaller, the other needs neither.

Its load-bearing detail is that **both workflows attach idempotently rather than
under a lock**. That is worth spelling out, because the obvious alternative is
wrong in a way that only shows up under load:

A GitHub `concurrency.group` is not a mutex. It keeps at most one *running* and
one *pending* job per group, and when a new arrival is queued, the existing
pending job "will be canceled and replaced". GitHub's own reference says group
names "must be unique across workflows to avoid canceling in-progress jobs or
runs from other workflows".

So sharing one group across these two workflows would not have ordered them onto
a single draft — it would have made them evict each other. With three builds
contending, a late-arriving leg could cancel the Windows job while it was merely
waiting, and a Linux leg that hung on a missing PySide6 wheel would have held
the group and cost the Windows release too.

What is actually true is that the three builds need no coordination at all:
separate runners, separate working copies, separate output. The only shared thing
is the release, and the only race is `gh release create` — which the attach step
resolves with `|| gh release upload`, so whichever workflow loses the create race
attaches instead of failing. Uploading different filenames concurrently to one
release is safe.

`packaging/source_release.py` is the builder, and it is loaded by path for the
same reason this file is: `packaging/` is a namespace portion and the real
`packaging` package is installed alongside it. It reuses `normalize_version`,
`check_requested_version` and `write_sha256_sidecar` from `build.py` rather than
reimplementing them, so the tag-must-match rule above has one owner.

Its manifest is an allow-list, its modes and timestamps are fixed so two builds
are byte-identical, and it prints an `artifact:` line the workflow reads back
instead of reconstructing the filename. All of that is
[source-install.md](source-install.md)'s subject, along with what the macOS and
Linux CI legs do and do not prove.

### What running the suite on macOS and Linux actually found

Worth recording, because these are the first non-Windows runs of anything and
they earned their keep immediately. All four macOS failures were real, and three
of them were things a green Windows suite could not have found:

- **`shared/exporting.py:_is_link_or_reparse_point` caught only
  `FileNotFoundError`.** POSIX answers `NotADirectoryError` (ENOTDIR) when
  `lstat` is given a path whose *ancestor* is a file — which is exactly what
  happens when an export destination's parent directory has been replaced by a
  file. Windows answers `FileNotFoundError` for the same path, so the preflight
  leaked a raw ENOTDIR out of `plan_export` instead of naming the problem the
  very next line already knew how to name. The regression test injects the POSIX
  behaviour so all three legs hold it down.
- **`shared/records.py:record_error_reason` classified a read failure as a
  corrupt record.** `load_record` reports every `OSError` as a `RecordError`, and
  the classifier matches on the message — so "Record could not be read: [Errno
  13] Permission denied" matched no branch and fell through to
  `REASON_INVALID`. `is_record_problem` then reported `True`, which tells a user
  to fix their tags over a file commcut could not open.
  `shared/catalog.py:REASON_UNREADABLE` had been effectively unreachable for
  anything except a *missing* record, because `load_record` re-raises only
  `FileNotFoundError` unwrapped. This one is the most interesting of the four,
  because the test guarding it used `chmod 000`, which does not stop the owner
  reading on Windows — so it had skipped on every platform the suite had ever
  run on, and the bug was waiting rather than absent.
- **`tests/test_frozen_mode.py` referenced `subprocess.CREATE_NO_WINDOW`
  directly**, a constant that exists only on Windows, so the two tests failed on
  macOS from inside the code under test. They now supply the constant as well as
  faking the platform, which means those legs verify the Windows branch rather
  than skipping it.
- **PySide6 needs system libraries that are not pip-installable.** `import
  PySide6.QtGui` `dlopen()`s `libEGL` at load time, so on a bare runner it fails
  before any platform plugin is chosen and `QT_QPA_PLATFORM=offscreen` does not
  help — which is why 16 test files errored during *collection* rather than one
  test failing. The Linux leg installs them.

The pattern is worth stating, because it is the argument for the whole workflow
rather than a detail of it: **three of these were refusals or guards that were
silently Windows-only.** Not test gaps — code whose behaviour depended on which
`OSError` the host raises, or on a constant that only exists on one platform. A
suite that has only ever run on one platform has not been tested on the others,
whatever it asserts.

