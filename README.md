<img width="720" height="303" alt="image" src="https://github.com/user-attachments/assets/16477bd8-32d0-46fa-b966-11becee62828" />

# CommCut, A Linear Television Filler Manager

<img width="858" height="581" alt="image" src="https://github.com/user-attachments/assets/12cd3700-0407-430c-9546-2acc5f36d69a" />

### Less Clicks, Less Clacks, More Commercials

Are you interested in commercials, network bumpers, and promos from 20+ years ago? Do you wish you could create, organize, and utilize a large library of them for your own enjoyment?

Good, I too, am insane.

My hope for this repository, between CommCut and the [Filler and You guide (WIP)](docs/guides/filler_and_you.md), is to give people both the tooling and the knowledge to start at 

- `I downloaded "Cartoon Network Commercials (August 10, 2004) [dQw4w9WgXcQ].mp4", now what?`

and make it all the way to

- `Yeah, I run a mix of general commercials from 2005 (+/- 5 years, only child demographic targeted) and CN City era Cartoon Network promos as my filler on that channel`

I have spent countless hours figuring out an editing workflow and an organizational structure to make that dream a reality, and that was *before* I had even considered designing my own filler editor/library manager. Now, for the first time, *you* too can benefit from my struggles, for the low low price of downloading my junk and reading my ramblings!

So, **what is CommCut**? In short, it is **a suite of tools to manage, rename, organize, and edit filler for the purposes of linear television emulation.** I'm a highly ~~lazy~~ efficient person, so every single workflow in this program tries to automate as much as possible, and anything that can't be fully automated is offered for user review and/or accomplished in the least amount of clicks and typing as possible.

**AI Disclaimer**: An LLM wrote 95% of this code, some of it more thoroughly reviewed by me (an admittedly amateur coder) than others. I designed and tested every module (which is preserved in the [closed Issues](https://github.com/trafficman/commcut/issues?q=is%3Aissue+state%3Aclosed)), made most of the UI myself, drew everything in mspaint (poorly), and wrote all of the public facing text aside from the install instructions and the engineering documentation.

## Common Use Cases

- You have compilations of commercials that you would like to quickly and easily cut up into individual clips
- You would like your created clips to be dynamically named and organized on export based on the tags you have set for them
- Similarly, you would like to organize an existing library of filler with an easy to use tagging wizard
- You would like to easily share your filler library with others, and be able to import and mesh other's libraries into yours

## Features

- **Video Editor**: Scan a compilation video for commercial boundaries, then verify and/or confirm those boundaries one at a time, from left to right.
- **Tagging**: Apply tags to each clip in your library, this allows them to be named and organized to your liking (the schemes are user customizable), plus trivializes sharing!
- **Importing**: Whether it's an existing, untagged library, or a library which has already been processed by CommCut, there's an easy to use wizard that walks you through the process of meshing that library into yours.
- **File/Folder Schemes**: Name and organize your filler library *your* way, with tag based schemes that are customizable in the settings, try to match your existing structure or use the provided defaults!
- **Sharing**: Easily create a zip file containing all or part of your library, send it to a friend, and they can drag and drop it into the main menu to import it into their own.

# Install

## Windows

Download `commcut-<version>-windows-x64.zip` from the
[releases page](https://github.com/trafficman/commcut/releases), extract it
anywhere, and run `commcut.exe`. No Python required, the zip carries it.

It is unsigned, so SmartScreen will throw a warning. Choose **More info → Run anyway** (I ain't paying $80/mo for code signing)

## macOS and Linux

Download `commcut-<version>-source-macos-<arch>.tar.gz` (or `-linux-`) from the
same page, then:

```bash
tar xzf commcut-<version>-source-macos-arm64.tar.gz
cd commcut-*
./install_deps.sh
./run.sh
```

`install_deps.sh` builds a Python environment in the folder you extracted to and
installs everything PySide6 needs. It then **tells you** if ffmpeg or the mpv
client library is missing — it will not install system packages for you, because
it will not ask for your password. It prints the exact command to run:

```bash
brew install ffmpeg mpv                  # macOS
sudo apt install ffmpeg libmpv2          # Debian, Ubuntu
sudo dnf install ffmpeg mpv              # Fedora, RHEL
```

Then re-run `./install_deps.sh` to confirm, and `./run.sh` to start. On macOS
you can double-click `commcut.command` instead of running `./run.sh`.

Your clips, settings and log all live in the folder you extracted to — that is
where commcut reads and writes, and it needs to stay writable.

Full detail, including the one thing about this platform that is not yet
verified, is in [docs/source-install.md](docs/source-install.md).

## Updating

There is no auto-updater, a new version is a new download from the
[releases page](https://github.com/trafficman/commcut/releases/latest)
(that link always points at the newest). Each release extracts into its
own versioned folder, so updating is side-by-side: the old version keeps
working until you delete it, and nothing is overwritten.

**Windows**

1. Download the newest `commcut-<version>-windows-x64.zip` and extract
   it **next to** your current folder, not into it.
2. If you customized anything in Settings, copy `settings.json` (and
   `vocabulary.json`, if you have vocabulary entries) from the old
   folder into the new one. If you have any in-progress imports, also copy/move
   the contents of the `/import/` folder over (along with `/export/` if you have
   not changed your export location from the default). 
3. Run the new `commcut.exe`.
4. Once it works, delete the old folder.

**macOS and Linux**

Same, except after extracting the new tarball, run `./install_deps.sh`
in the new folder (it rebuilds the Python environment there) before
`./run.sh`. ffmpeg, mpv and libmpv are system packages you already
installed, they are shared, no need to reinstall. Copy `settings.json`
and `vocabulary.json` over if you customized anything, `/import/` and 
`/export/` if they are currently in use, then delete the old folder.

To be told when there's a new version: in the upper right of the repo, 
use the **Watch** dropdown → **Custom** → **Releases**.

# Usage

## Editor

**Cut compilations of commercials into individual clips, tag them, export them, properly named and organized, to your library**

Video tutorial (**unmute**):


https://github.com/user-attachments/assets/a6e2e82e-040b-456e-8159-c6f3e7087310


(Real video tutorials coming soon)

**In Short**:

1. Calibrate the Segment Scanner by running test scans on a 2 minute preview of your source video first
2. Define the endpoint of the current clip with the "End Seg" button
3. Fill out tags as you see fit (mouse over their titles for a brief tooltip)
4. Once satisified with the current clip, press "Stage"
5. Repeat this process until you've processed every clip
6. Export your clips, conveniently named and organized according to their tags, by pressing "Finished - Export" at the bottom

**Extensive instructions also available**:

<details>

<summary>Expand for in depth Video Editor instructions</summary>

### Main Menu

- Click "Editor" and pick the source video from the file browser, or just **drag the video file onto the main menu** and drop it
- Either way the Segment Scanner window opens on that video, scanning it for clip boundaries before the editor proper

### Segment Scanner

**TL;DR**: Manually place boundaries in between clip segments -> Run "Test Scan" -> Compare scanned boundaries with yours -> Fiddle with settings until they match -> Finish

The basic idea is to manually mark the boundaries in a small sample of your source video, then tweak the Segment Scanner settings with the sliders, running Test Scans, until you catch as many of the real boundaries as possible with the fewest false positives (my advice: prefer it being too sensitive over not being sensitive enough, it's less work to remove a boundary than it is to add one)

- The Segment Scanner cuts the first 120 seconds out of your source video in order for you to calibrate the full scan
- The basic controls are as expected, play/pause, frame-by-frame, moving between keyframes
- Between the video and the controls are two scrub-able timelines (the vertical red lines are the playheads) labeled "Scanner Preview" and "User Marked"
- Using the controls, find the (usually, black frames) boundary between the first and second commercials, and click "Place Boundary"
- A grey marker will be placed under the red marker in the "User Marked" timeline
- Repeat until every legitimate boundary has been marked
- Click the "Test Scan" button, a black-detect segment scan with the current settings will be run
- The upper "Scanner Preview" timeline will populate with grey markers, compare them to your manually placed ones below
- Tweak settings until they maximally line up
- Once they do, press "Finished - Scan Full Source Video"

### Editor Proper

**TL;DR**: Define endpoint of current clip with "End Seg" -> Fill out tags -> Press Stage -> Repeat

The idea behind this video editor is that, it functions more like a linear wizard than an actual fully fledged editor. You focus on one single "Active Segment" (the segment with the white box around it) at a time, which logically represents a single filler clip. Your only real job is to define (or if the Segment Scanner got it right, confirm) the end point of this current clip, plus its tags, before you swiftly move on to the next clip, and, because there cannot be any overlapping segments, once you define the end point of one segment, you have *already* defined the start points of the next one.

Controls, from left to right, top down:

#### Timeline

- **Timeline**: Shows segments in alternating colors, playhead (red vertical line) can be moved with mouse, defaults to zooming in on current active sgement.
- **Timeline Zoom**: Magnifying glass icon button, toggle between zooming in on the active segment, and showing the entire timeline.

#### Timeline Controls

- **Skip Cur Seg**: Skip Current Segment, mark Active Segment to be skipped, will not be rendered come export time, doesn't need required tags etc (**Usecase**: Segment is a stub left over from the show, segment is too low quality due to tape issue).
- **Start Seg**: Start Segment, only usable inside the current Active Segment, clicking this will divide the active segment in half (into two new segments), the left half will be marked as ignored, while the right half will become the new Active Segment and inheret the original segment's tags (**Usecase**: Quicker way to ignore a bad segment that wasn't automatically scanned).
- **Timeline Navigation**: Standard controls, pause/play, frame and keyframe forward/backward.
- **End Seg**: End Segment, does two separate things, when used inside of the Active Segment it splits it in half, with the left side staying the active segment and the right side becoming the new next segment (**Usecase**: Scanner missed a boundary). When used *past* the Active Segment, it instead shifts the boundary to that point (**Usecase**: Scanner placed the boundary slightly too early and you need to shift it forward).
- **Add Next Seg**: Add Next Segment, merges the Active Segment and the segment immediately after, deleting the boundary between them (**Usecase**: Scanner missed a segment boundary).

#### Segment Controls

- **Active Left/Right**: Move the Active Segment selector left or right across the timeline, making the segment immediately to the left or right the new Active Segment (**Usecase**: You want to edit a past segment)
- **Undo**: Undo any unstaged changes and revert back to the current state of the `.cmct` file (**Usecase**: You made a mistake).
- **Stage**: Stage the changes made to the current Active Segment to the source video file's accompanying `.cmct` file, then automatically advances to the next segment (**Usecase**: You are finished with the current segment).

#### Tags

Four required tags, **Title**, **Type**, **Network**, and **Time Period**, and many optional tags. These are ultimately what will name and organize your filler library (and importantly, what will allow you to do advanced filler scheduling), **for a glossary of filler types** see my WIP guide [Filler and You](docs/guides/filler_and_you.md#glossary).

- **Lock/Unlock**: Locks the currently entered tag in that row, carrying it over to the next segment, and every segment after that as long as it remains locked (**Usecase**: The entire source video is from 2010, so all clips have that year applied)

- **Title**: Required, the title of the clip, unique, and should be descriptive enough to tell what the clip is at a glance.
- **Type**: Filler Type, required, the category of this particular filler clip, again see the [Filler and You](docs/guides/filler_and_you.md#glossary) glossary for the ones I personally use + their descriptions.
- **Network**: Required, the television network which this filler clip aired on/you want it to appear on. Promos and bumpers are network specific, but commercials are more generalized (appearing across multiple networks), for those I personally use the network "General".
- **Time Period**: Required, the time period ("80s" vs "90s") or era ("CN City" vs "Powerhouse") in which this filler aired. Personally, I use 5 year time periods (2000, 2005, 2010, etc) so that whatever my target year for a channel is, it can have filler from the surrounding +/- 5 years.
- **Year**: Optional, the year this filler clip aired, prime candidate for being locked.
- **Block**: Optional, the programming block this filler clip aired in (e.g. "Toonami", "Cartoon Cartoon Fridays"). Basically for anything that didn't air during a network's general programming.
- **Show**: Optional, the show this filler clip is tied to, I recommend you only use this if it is *intrinsically tied* to the show (i.e. a show Intro, a Be Right Back, etc, a Promo about a particular show should NOT receive this tag, as it would not air *with* the show).
- **Special**: Optional, this could be any number of organizational things, eg Holiday, Target Demographic (I use it on General Commercials for "Kids" vs "Everyone" vs "Mature"), etc, basically any special circumstance that doesn't apply elsewhere.
- **Length**: Optional, the length of the filler clip, should **ONLY** be used when differentiation between clips is needed, e.g. two of the same promos but one is a 60 Sec cut and the other is a 30 Sec cut.
- **Info**: Optional, denotes something about a file, eg Upscale, Remastered, Low Quality, Left Only Audio, etc.

#### Export

When you've staged the last segment, click "Finished - Export" at the bottom, and the clips will be named and organized in your export folder according to their tags using the schemes set in the settings (the defaults are what I personally use, although they can be changed in the settings).

**Example**: `Cartoon Network/Blocks/Toonami/Promo/Cartoon Network - Promo - 2000 - Toonami Worlds Finest (30 Sec Remastered).mp4`

</details>

## Library Manager

**Manage your filler clips, (soon) deal with duplicate clips, and perform library maintenance**

Pretty self explanatory, used to browse your filler library and manage its tags, both singularly and in bulk.

### Single Editing Mode

1. Expand folders in the library tree in order to navigate to your desired clip.
2. Click on the text of the clip to load it into the right pane.
3. Watch the clip, and change its tags in the tag form below as desired.
4. Click save, and your clip will be renamed and moved to its new location, according to its tags and your set schemes.

### Bulk Editing Mode

1. Click on one or more of the toggles in the library tree in order to enter Bulk Editing Mode.
2. The video player will disappear, and the tag form on the right will activate a "blended" mode, overlapping all of the toggled clip's tags.
3. In this blended mode, any namespaces that are the same tag across every single toggled clip, will display as that tag, while any with differing tags will display as `(multiple values)`
4. Any changed tags (aside from Title which cannot be bulk edited) in this blended mode will apply to **every** toggled clip once "Apply to X clips" is clicked.
5. To bulk remove a tag from all clips, simply erase it and then apply it (if the entries you wish to remove are multiple values, you must first type any character, and then delete it).

## Library Import

**Import both tagged and untagged libraries into your own**

Drop an existing untagged filler library folder, or a library which has already been tagged by CommCut, into `/import/`, and click the button on the main menu. Also supports Library Shares produced by CommCut itself, simply drag and drop the share `.zip` file onto the main menu.

### Untagged Libraries

Any clips in the `/import` folder which do not have an associated `.cnfo` files with tags will first pass through the Untagged Library Mesh, where you will apply tags either semi-automatically (via folder names) or manually. It may prove easier to do some amount of initial organization at a file level first, before importing, as folder names are a quick and easy way to automatically apply tags across large swathes of clips.

**NOTE**: The `/import/` folder is potentially destructive: Through use, CommCut will rename, reorganize, move, and delete files in this folder. If you wish to preserve the original state of your library, make sure you're working on a copy of it, and have the library source stored safely somewhere else.

1. Assuming the existing untagged library has some sort of organization scheme, the Untagged Library Mesh will show you each folder name, and ask you what namespace and tag should be applied to every clip contained within it (recursively, so it applies to clips in all subfolders as well). Each instance of that folder name will have the tag applied to it, so any duplicates will receive it as well. Folders that do not need a tag (e.g. technically namespace folders like "Blocks") can be skipped.
2. Once you've assigned a tag to every folder name, you will advance to the individual clip Tag Editor, where you will assign a title to each clip, and make any needed tag edits.
3. Title must be manually set for each clip, if the file names contain the title, this can be sped up: click and drag your cursor over the file name at the top in order to highlight it, then click the button to add the highlighted text as the title.
4. Apply any other required/desired tags as needed on a clip by clip basis.
5. Derive tags based on text contained in the file name by clicking "Manage Autofill Rules", it comes with year parsing as a default rule, but this can be temporarily removed if desired. Add new rules as you discover them in the library (e.g. if a filename has "30 Sec" in it, apply the tag `length:30 Sec`) and your tagging will get faster as you progress. Autofill Rules are per session, they reset to only the year one on every new import.
6. You may stop at any time, your progress through the clips is saved (you are actually writing `.cnfo` files to each clip, slowly turning it into a Tagged Library) and will resume where you left off the next time you open the importer. Once you have completed all of them, you will be given the opportunity to review the tags in the Tagged Library Mesh (next section). If you are confident you got all of them right and didn't make any mistakes, you can click "Import Now" to skip this process and go straight to moving/copying the imported library straight to your export directory.

### Tagged Libraries

If all of the clips in the `/import/` folder already have associated `.cnfo` files (either because you are importing an already tagged library, or you just tagged an untagged library yourself), you will be taken to the Tagged Library Mesh. Contrasting with the Untagged Library Mesh, which lets you apply new tags to clips, the purpose of the Tagged Library Mesh to match up the tags someone else uses in their library to the tags you use in your own. If the creator of the library uses `network:CN` or `network:Nick` while you use `network:Cartoon Network` or `network:Nickelodeon`, you will have the opportunity to translate, in bulk, from one to the other.

1. The Tagged Library Mesh will show you one tag at a time from the imported library, and ask what you want to change it to, if anything.
2. Type in a new value if desired and press "Use This Value", if the tag already matches your library press "Keep As It Is", and if you want to entirely remove the tag in question press "Remove This Tag".
3. Repeat until all tags have been addressed, then review the results, if everything looks correct, press "Import Now".
4. Choose your transfer method, whether you want to copy, move, or link the files from your import folder to your export (library) folder.
5. The newly imported clips will appear in your library, tagged, named, and organized to your liking according to your file and folder schemes.

## Library Sharing

**Share all or part of your filler library with others**

Package your filler into a convenient zip file, which the recipient can simply drag and drop onto their CommCut main menu in order to import it into their library.

1. If you wish to share your entire library, open the Library Sharing window and simply click "Share".
2. If you only want to share a portion of your library, first edit the included and/or excluded tag lists.
3. Included tags are a whitelist, only clips which contain these tags will be included in the share zip file.
4. Excluded tags are a blacklist, clips which contain these tags will not be included in the share zip file.
5. Once you are satisfied and click "Share", supply a descriptive name for the Share, and the zip file will be deposited in the "share" folder in the root of your CommCut directory, with all the clips and their associated `.cnfo` files included.
6. Upload this zip somewhere and share it with whoever you want, they will be able to use CommCut to import it and mesh it into their existing CommCut library.

# Roadmap

- ~~v0.1 Video Editor~~
- ~~v0.2 Library Importer~~
- ~~v0.3 Library Manager~~
- ~~v0.4 Library Sharing~~
- v0.5 Duplicate Scanner

# Documentation

This file is for using the program. The engineering documentation is separate:

- [docs/README.md](docs/README.md) — the documentation index, one entry per document
- [docs/status.md](docs/status.md) — what is built today, what is next, known gaps
- [docs/packaging.md](docs/packaging.md) and [packaging/README.md](packaging/README.md) — the portable build
- [AGENTS.md](AGENTS.md) — orientation and working agreements for coding agents
