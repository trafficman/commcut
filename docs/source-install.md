# Running from source on macOS and Linux

The source release: what is in it, how to install it, what it resolves and from
where, and the one thing about it that has not been verified.

Applies to: `install_deps.sh`, `run.sh`, `commcut.command`,
`packaging/source_release.py`, `shared/environment.py`, `shared/mpv.py`,
`shared/ffmpeg.py`, `shared/sources.py`, `requirements.txt`,
`.github/workflows/source-release.yml`.

Related: [packaging.md](packaging.md) (the Windows build, and the two roots),
[architecture.md](architecture.md) (the one-process-per-window model this
inherits), [testing.md](testing.md) (the harness, and why a green suite does not
prove a platform works).

## The source release

`.github/workflows/source-release.yml` builds
`dist/commcut-<version>-source-<os>-<arch>.tar.gz` on a tag push and attaches it,
with a `.sha256` sidecar, to the same draft release as the Windows zip. Each leg
of its matrix runs the test suite first, so a release is refused for a test
reason before an archive exists.

Two properties of that workflow are worth knowing before changing either:

- **Its concurrency group is character-identical to `release.yml`'s.** GitHub's
  concurrency is repo-wide across workflows, so the two serialize on one draft
  release. Without it both can fail `gh release view` and both try to create it,
  and the loser fails a run that did its job.
- **It builds on the platform it is releasing for.** The Windows job is the only
  path that has ever shipped and was left alone; the two new legs need none of
  its machinery and none of its 384 MB of Git LFS binaries.

### What is in it, and what is not

The manifest is an **allow-list** (`packaging/source_release.py:SOURCE_ENTRIES`),
and that is a safety property rather than a style preference. `import/`,
`export/` and `temp/` are *not* in `.gitignore` — for a source install the
project root **is** the install root, so they sit at the repo root holding
whatever was last exported. An archive built from "everything, minus what I
remembered to exclude" would ship a developer's clips into a public release.

Ships: the app (`main.py`, `mainwindow.py`, `shared/`, `editor/`, `scanner/`,
`settings/`, `importer/`, `assets/`), `requirements.txt`, the three scripts
below, and **every markdown file in the repository**.

Every markdown file, rather than a curated two, because
`tests/test_docs.py` requires that every relative markdown link in the tree
resolves to a `.md` file. "All of them" is therefore provably enough: no link
inside a source release can dangle. That is also why `experiments/README.md`
ships — `AGENTS.md` and three documents link into it — while the experiment
code beside it does not.

Does not ship: `bin/` (Windows binaries, which these platforms must not use),
`tests/`, `prototypes/`, the dead `core.py` and `picker/`, `.github/`, build
output, and `__pycache__`. `tests/test_source_release.py` plants a full set of
each of those in a scratch tree and requires that none of it arrives.

`bin/` is worth one sentence of its own: the absence is not a loss of the
override. `resolve_mpv_library` searches `bin/<os>/` *before* the system
prefixes, so `mkdir -p bin/mac` and dropping a `libmpv.2.dylib` in still wins,
and the archive does not have to ship an empty folder for that to work.

### Two details of the archive that are deliberate

**It is a `.tar.gz`, not a `.zip`.** The launchers must arrive executable. A zip
written by Python does not carry Unix permissions, and Finder's Archive Utility
does not restore them on extraction, so a `.zip` release would need a `chmod +x`
before it could run. `tar` preserves the mode by definition. The mode is also
written into each `TarInfo` explicitly rather than read from disk: a Windows
checkout has no execute bit and `core.fileMode` is off there, so a mode taken
from the filesystem would be whatever the builder's umask decided.

**Two builds of one tree are byte-identical.** `mtime`, `uid`, `gid`, `uname`,
`gname` and the gzip header are all fixed and entries are sorted, so the
`.sha256` sidecar is a real claim. The Windows build cannot do this — PyInstaller
embeds a build timestamp — and settles for a stable entry order instead.

A consequence worth stating plainly: **the macOS and Linux archives have the
same contents**, so `--target` exists to build either from anywhere, and the
only thing that varies between them is the filename.

## Installing it

Extract, then two commands:

```bash
tar xzf commcut-<version>-source-<os>-<arch>.tar.gz
cd commcut-<version>
./install_deps.sh
./run.sh
```

On macOS, `commcut.command` is the same thing as `./run.sh` for Finder: it runs
it, keeps the Terminal window on screen afterwards, and waits for a keypress. It
exists because an error a user cannot read is an error they will report as "it
didn't start".

Both scripts `cd` to their own folder first. Finder runs a double-clicked
`.command` with `$HOME` as its working directory, so without that `main.py` is
not found.

### What `install_deps.sh` does, and what it refuses to do

Two halves, on purpose.

**The Python half it owns.** It finds a Python new enough for the pinned
PySide6, creates `.venv/` in the install folder, and installs `requirements.txt`
into it. The virtual environment is not tidiness: PEP 668 makes `pip install`
into a Homebrew or Debian/Ubuntu system interpreter an *error* rather than a
warning, so without one the script would have nothing to do but explain that.

It accepts `python3.13` down to `python3.10`. `requirements.txt` says 3.11, and
that pin is real, but it is a **PyInstaller reproducibility** pin — no 3.11-only
syntax exists anywhere in the tree — so 3.10 is the actual floor and it is
PySide6's, not commcut's.

**The rest it only reports.** ffmpeg, ffprobe, libmpv and `libx264` come from a
package manager, and this script will not run one. It will not run `sudo`
either. Instead it calls the app's own resolvers — `get_binary_path`,
`resolve_mpv_library`, `shared.ffmpeg.check_video_encoder` — and prints what
they said, which already names every folder searched and every command that
would help. A second, shell-side implementation of the search order would be a
second answer to the same question, and it would be wrong the day someone added
a platform.

`./install_deps.sh --brew` is the one exception, and only on macOS, and only
when Homebrew is present: it runs `brew install python ffmpeg mpv` first. It is
opt-in because a script that mutates a package manager unasked is a surprise,
and because `brew install python` may install a different major than the pins
were resolved against.

The check skips the `libx264` probe when ffmpeg itself did not resolve. The
probe shells out to ffmpeg, so with no ffmpeg it cannot say anything useful, and
reporting it would send the user to fix an encoder they do not have a problem
with.

### What `run.sh` does

It execs `.venv/bin/python main.py` and nothing else. It will not create the
environment: a first run that quietly downloaded PySide6 would put a
four-minute wait and a pip error inside window-startup, with no terminal to read
either in. When the environment is missing it says so and names
`install_deps.sh`.

`exec` rather than a subshell, so commcut *is* the process and Ctrl-C reaches
the app rather than a wrapper holding it open.

### Where the app's data goes

Into the extracted folder, because `install_root()` is the project root when
unfrozen: `settings.json`, `vocabulary.json`, `import/`, `export/`, `temp/` and
`commcut.log`. That folder must be writable, and it is the whole reason a source
install works where a frozen `.app` would not — see the next section.

## What "not packaged" means here

`packaging/build.py` produces a **Windows** build. There is no macOS or Linux
*build*, and none is planned: frozen macOS would resolve `install_root()` to
`Contents/MacOS` inside a signed `.app` bundle, which is read-only and whose
signature breaks the first time the app writes a file into it. What those
platforms get is a **source release**, so the install root is the extracted
folder, which is writable, and that whole class of problem cannot arise.

What a source release does *not* get is a bundled ffmpeg. The binaries come from
the system instead. That inverts one policy the Windows build depends on, which
is the subject of the next section.

## Where the binaries come from

`shared/environment.get_binary_path` resolves ffmpeg and ffprobe, and the policy
is data rather than an if-chain:

| | Windows | macOS, Linux |
|---|---|---|
| Searched first | `bin/win/` | `bin/<os>/` (absent from a source release, so nothing) |
| Then | *nothing* | `/opt/homebrew/bin`, `/usr/local/bin`, `/usr/bin` (macOS); `/home/linuxbrew/.linuxbrew/bin`, `/usr/local/bin`, `/usr/bin` (Linux) |
| Missing | `FileNotFoundError` | `FileNotFoundError`, naming every location |
| A candidate must be | present | present **and executable** |

Windows refusing to fall through is the point. A packaged build that silently
used a system ffmpeg would be running a binary nobody tested, which is a far
worse failure than a missing file — and the Windows build *does* bundle a known
one, so there is nothing to gain. macOS and Linux have the opposite situation:
the user supplied the ffmpeg, and "whatever the user installed" is the intent.
`_BUNDLED_BINARY_PLATFORMS` is the switch that says which side of that a
platform is on.

macOS needs no architecture handling anywhere. Homebrew installs to
`/opt/homebrew` on Apple Silicon and `/usr/local` on Intel, and both are listed,
so one artifact works on both.

Linux does need one, and it is about **libraries** rather than binaries — see
`libmpv` below.

## libmpv is not mpv

**The one that trips people up.** `brew install mpv` gives you the *player*.
python-mpv needs the **client library** — `libmpv.dylib` — which is a separate
thing to install, and having the player is no evidence you have it. Plenty of
machines install mpv successfully and then cannot start commcut.

`shared/environment.resolve_mpv_library` searches, in order:

1. **`COMMCUT_MPV_LIB`**, if set — must be an absolute path that exists. A value
   that does not exist is an error, not a reason to keep searching: an override
   that silently does nothing is worse than none. Point it at a framework binary
   (`…/mpv.framework/mpv`) if that is what your build ships; `ctypes` loads those
   too.
2. `bin/<os>/`, for the platform's candidate filenames.
3. `system_lib_dirs()`: the `lib/` subdirectory of each system prefix, **plus,
   on Linux, `/usr/lib/<multiarch>`**.

```bash
ls /opt/homebrew/lib/libmpv*
export COMMCUT_MPV_LIB=/opt/homebrew/lib/libmpv.2.dylib
```

### The multiarch directory, and why it is in the list

A distro package does not put libmpv in `/usr/lib`. `apt install libmpv2`
installs `/usr/lib/x86_64-linux-gnu/libmpv.so.2`, which is not any of
`/home/linuxbrew/.linuxbrew/lib`, `/usr/local/lib` or `/usr/lib` — so a
prefix-only search misses **exactly the case the error message in
`shared/environment.py` tells the user to create**. The command and the search
disagreed, and the search was the one that lost.

The triplet is read from `sysconfig.get_config_var('MULTIARCH')` rather than
written out, so an aarch64 host names its own, and it is appended only on Linux
— the multiarch layout is a dpkg convention, and adding it to macOS would put a
path that never exists into the error message's search list.

### Why it is loaded *and named*, not just found

On Windows, `ctypes.util.find_library` — which python-mpv calls at *import* time
— scans `%PATH%`, so prepending `bin/win` is enough, and
`os.add_dll_directory` is registered as well (its handle is held in a module
global, because dropping it unregisters the directory and surfaces much later as
a bare `OSError`).

**On macOS `find_library` ignores `PATH` entirely.** It searches a fixed list of
system directories and returns a path only if that exact file exists. A source
install's libmpv is not there, so the `PATH` prepend buys nothing.

**And loading the library is not sufficient on its own.** This is the part worth
knowing if you are debugging this. python-mpv 1.0.8's POSIX branch is:

```python
sofile = ctypes.util.find_library('mpv')
if sofile is None:
    raise OSError("Cannot find libmpv in the usual places. ...")
backend = CDLL(sofile)
```

It raises when the *lookup* is empty. It never checks whether libmpv is already
mapped, so a library commcut found and loaded is still one python-mpv refuses.
`DYLD_LIBRARY_PATH` does not help either, because `find_library` checks for a
file at a fixed list of paths rather than asking the dynamic loader. The error
tells you to read the `ctypes.util.find_library` documentation, which is a
s unhelpful place to be sent from a GUI.

`shared/environment.mpv_import_context` therefore does all three things, and
`create_mpv_player` wraps its `import mpv` in it:

1. resolves the library by absolute path (`resolve_mpv_library`),
2. maps it with `ctypes.CDLL(..., RTLD_GLOBAL)` (`load_mpv_library`), and
3. **answers `ctypes.util.find_library` for exactly the names python-mpv asks
   for** — `mpv`, and the platform's spellings — so its own lookup returns the
   path this module validated. Every other name still goes to the real function,
   and the override is removed when the `with` block exits.

`DYLD_LIBRARY_PATH` is also set, for libmpv's transitive dylibs. That is
secondary, and it works because a PyInstaller app is not SIP-protected. It is
skipped on Linux, where `LD_LIBRARY_PATH` semantics for runtime `dlopen` are
murkier and the load is the part that matters.

The context is entered from `create_mpv_player`, not from `setup_environment`:
the main menu never loads mpv, and entering it there would put libmpv in every
process, including the one with no player.

## The ffmpeg must have libx264

commcut hardcodes `libx264` for every exported clip. Homebrew's ffmpeg has it;
many minimal builds do not. When it is missing, scanning and preview still work
and **only export fails** — a raw "Unknown encoder" line, once per clip, after
the batch has started. `shared/ffmpeg.check_video_encoder` probes
`ffmpeg -encoders` once and refuses the batch up front with a named problem
instead.

If export is the only thing broken, check this first:

```bash
ffmpeg -hide_banner -encoders | grep libx264
```

## Case-insensitive filesystems

`shared/sources._is_inside` is the traversal defense on the import folder, and
its containment comparison is a string comparison — which is case-sensitive,
while NTFS and APFS are case-insensitive by default. The same command-line
argument would therefore be accepted on Windows and refused on a Mac, for a file
the OS would plainly open, and the error would tell the user to put the video
somewhere it already is.

The volume is asked directly (`os.path.samefile(folder, folder.casefold())`),
and only when it says it is case-insensitive is the comparison retried folded.
That direction is the one that could turn a consistency fix into a traversal
hole — on a case-*sensitive* filesystem, folding unconditionally would accept
paths that really are outside the folder — so on a volume that reports otherwise
no fold happens at all.

`shared/paths.py` needed nothing here: `normalized_validation_key` already
applies `unicodedata.normalize("NFC", ...)` and then `casefold()`, so the
case-insensitive APFS default and HFS+ NFD-vs-NFC do not affect export
collision checks.

## What has not been verified

**Playback.** That is the whole of it, and it is worth being exact about what
the CI run does and does not settle.

`.github/workflows/source-release.yml` runs on macOS and Linux and it does
establish three things on those platforms:

- **the suite passes there.** `FakeBridge` stands in for libmpv by design, so
  this is not evidence that a player exists — it is evidence that the code, the
  pinned PySide6 wheel and the pinned python-mpv all work on that platform's
  interpreter, which until now was an assumption.
- **libmpv resolves.** The job installs the system packages and then runs
  `./install_deps.sh` against the extracted archive, which calls the app's own
  `resolve_mpv_library`. If the search could not find libmpv on that platform,
  the installer would exit non-zero and the release would fail. That is the
  multiarch fix above, checked on the platform it exists for.
- **the three launchers run.** The archive is extracted and `install_deps.sh` is
  executed inside it, so a quoting bug or a wrong relative path fails the
  release instead of the user.

What none of that establishes is that a video **renders**:

- **mpv embedding.** `vo=gpu` with a `wid` — an `NSView*` on macOS, a window on
  Linux. `WA_NativeWindow` is required on every platform, since `winId()` is
  what produces the handle. If the video area is black on macOS, the first
  thing to try is `vo=libmpv` (`MPV_VIDEO_OUTPUT` in `shared/environment.py`).
- **Wayland on Linux.** `wid` embedding is an X11-shaped mechanism. A Linux
  session on Wayland is the open question, and it is the main reason Linux is
  designed-for rather than supported.

So: **a green CI run is not evidence that a platform works**, and neither is a
green local suite. Only a person on that machine can confirm playback. The
verification checklist below is the remaining step, and it is short.

### Checking it, if you have the machine

1. Extract, `./install_deps.sh`, `./run.sh`. The installer should end with
   `Everything is installed.` If it does not, it has already named what is
   missing and where it looked — read that first.
2. Does the video render in the editor? Open the scanner, which plays a
   two-minute preview. A black area means the embedding assumption above is
   wrong, and `commcut.log` will show what mpv said.
3. Scan and export a compilation end to end. Export failing while scan works
   means `libx264`, not mpv.
4. Anything at all went wrong: `commcut.log` in the same folder. `COMMCUT_LOG`
   redirects it.
