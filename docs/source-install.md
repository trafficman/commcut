# Running from source on macOS and Linux

How to run commcut on a platform that is not the packaged Windows build, what
it resolves and from where, and the three things that are known not to have been
verified.

Applies to: `shared/environment.py`, `shared/mpv.py`, `shared/ffmpeg.py`,
`shared/sources.py`, `requirements.txt`.

Related: [packaging.md](packaging.md) (the Windows build, and the two roots),
[architecture.md](architecture.md) (the one-process-per-window model this
inherits), [testing.md](testing.md) (the harness, and why a green suite does not
prove a platform works).

## What "not packaged" means here

`packaging/build.py` produces a **Windows** build. There is no macOS or Linux
build, and none is planned: frozen macOS would resolve `install_root()` to
`Contents/MacOS` inside a signed `.app` bundle, which is read-only and whose
signature breaks the first time the app writes a file into it. macOS and Linux
are **source installs** — clone, install dependencies, run `python main.py` — so
the install root is the clone, which is writable, and that whole class of
problem cannot arise.

What a source install does *not* get is a bundled ffmpeg. `bin/mac/` and
`bin/linux/` ship empty, and the binaries come from the system instead. That
inverts one policy the Windows build depends on, which is the subject of the
next section.

## Where the binaries come from

`shared/environment.get_binary_path` resolves ffmpeg and ffprobe, and the policy
is data rather than an if-chain:

| | Windows | macOS, Linux |
|---|---|---|
| Searched first | `bin/win/` | `bin/<os>/` (empty, so nothing) |
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

### Installing them

```bash
brew install python@3.11 ffmpeg mpv      # macOS
brew install ffmpeg mpv                  # Linux, with Linuxbrew
apt install ffmpeg libmpv2                # Debian / Ubuntu
pip install -r requirements.txt
python main.py
```

`import/`, `export/`, `temp/`, `settings.json` and `commcut.log` all land in the
clone, because `install_root()` is the project root when unfrozen.

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
3. The `lib/` subdirectory of each system prefix.

```bash
ls /opt/homebrew/lib/libmpv*
export COMMCUT_MPV_LIB=/opt/homebrew/lib/libmpv.2.dylib
```

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

**None of this has been run on macOS or Linux hardware.** The code is
cross-platform and the suite covers the resolution logic on any host, but the
following are assumptions:

- **mpv embedding.** `vo=gpu` with a `wid` — an `NSView*` on macOS, a window on
  Linux. `WA_NativeWindow` is required on every platform, since `winId()` is
  what produces the handle. If the video area is black on macOS, the first
  thing to try is `vo=libmpv` (`MPV_VIDEO_OUTPUT` in `shared/environment.py`).
- **Wayland on Linux.** `wid` embedding is an X11-shaped mechanism. A Linux
  session on Wayland is the open question, and it is the main reason Linux is
  designed-for rather than supported.
- **Anything requiring a real player.** `tests/editor_stub.py`'s `FakeBridge`
  stands in for libmpv by design, so the suite passes on a machine with no
  libmpv at all. **A green suite is not evidence that a platform works.** The
  suite covers *resolution*; only a human on that platform can confirm
  playback, and the macOS `libx264` and libmpv checks above only cover what
  code can check.

### Checking it, if you have the machine

1. Does a loadable libmpv exist? `ls /opt/homebrew/lib/libmpv*`, then
   `python -c "import ctypes; ctypes.CDLL('/opt/homebrew/lib/libmpv.2.dylib')"`.
   This is the first thing to check and the one most likely to need
   `COMMCUT_MPV_LIB`.
2. Does the video render in the editor? A black area means the embedding
   assumption above is wrong.
3. Scan and export a compilation end to end. Export failing while scan works
   means `libx264`, not mpv.
4. Is the clone writable? `import/`, `export/`, `settings.json` and
   `commcut.log` all land there.
