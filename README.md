<img width="720" height="303" alt="image" src="https://github.com/user-attachments/assets/16477bd8-32d0-46fa-b966-11becee62828" />

# CommCut, A Linear Television Filler Manager

A suite of tools to manage, rename, organize, and edit pre-made filler for the purposes of linear television emulation

- Maps onto existing filler organization scheme (or creates one based on given criteria)
- Rename Wizard (Throw existent individual clips into import folder, programmatically rename them, dynamically save them to mapped filler export folder)
- Editing Wizard designed specifically with cutting compilations of commercials into individual clips in mind

# Install

COMING SOON

# Usage

## Editor (initial alpha release)

### Main Menu

- Pre-check: deposit source video(s) into `import` folder
- Click "Editor" and select desired source video from list
- This will, eventually, open the Segment Scanner window

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

Four required tags, **Title**, **Type**, **Network**, and **Time Period**, and many optional tags. These are ultimately what will name and organize your filler library (and importantly, what will allow you to do advanced filler scheduling), **for a glossary of filler types** see my WIP guide [Filler and You](/docs/guides/filler_and_you.md#glossary).

- **Lock/Unlock**: Locks the currently entered tag in that row, carrying it over to the next segment, and every segment after that as long as it remains locked (**Usecase**: The entire source video is from 2010, so all clips have that year applied)

- **Title**: Required, the title of the clip, unique, and should be descriptive enough to tell what the clip is at a glance.
- **Type**: Filler Type, required, the category of this particular filler clip, again see the [Filler and You](/docs/guides/filler_and_you.md#glossary) glossary for the ones I personally use + their descriptions.
- **Network**: Required, the television network which this filler clip aired on/you want it to appear on. Promos and bumpers are network specific, but commercials are more generalized (appearing across multiple networks), for those I personally use the network "General".
- **Time Period**: Required, the time period ("80s" vs "90s") or era ("CN City" vs "Powerhouse") in which this filler aired. Personally, I use 5 year time periods (2000, 2005, 2010, etc) so that whatever my target year for a channel is, it can have filler from the surrounding +/- 5 years.
- **Year**: Optional, the year this filler clip aired, prime candidate for being locked.
- **Block**: Optional, the programming block this filler clip aired in (e.g. "Toonami", "Cartoon Cartoon Fridays"). Basically for anything that didn't air during a network's general programming.
- **Show**: Optional, the show this filler clip is tied to, I recommend you only use this if it is *intrinsically tied* to the show (i.e. a show Intro, a Be Right Back, etc, a Promo about a particular show should NOT receive this tag, as it would not air *with* the show).
- **Special**: Optional, this could be any number of organizational things, eg Holiday, Target Demographic (I use it on General Commercials for "Kids" vs "Everyone" vs "Mature"), etc, basically any special circumstance that doesn't apply elsewhere.
- **Length**: Optional, the length of the filler clip, should **ONLY** be used when differentiation between clips is needed, e.g. two of the same promos but one is a 60 Sec cut and the other is a 30 Sec cut.
- **Info**: Optional, denotes something about a file, eg Upscale, Remastered, Low Quality, Left Only Audio, etc.

#### Export

When you've staged the last segment, click "Finished - Export" at the bottom, and the clips will be named and organized in your export folder according to their tags using the schemes set in the settings (the defaults are what I personally use, although they can be changed).

**Example**: `Cartoon Network/Blocks/Toonami/Promo/Cartoon Network - Promo - 2000 - Toonami Worlds Finest (30 Sec Remastered).mp4`

# Roadmap

- Complete roadmap
- 

# Documentation

This file is for using the program. The engineering documentation is separate:

- [docs/README.md](docs/README.md) — the documentation index, one entry per document
- [docs/status.md](docs/status.md) — what is built today, what is next, known gaps
- [docs/packaging.md](docs/packaging.md) and [packaging/README.md](packaging/README.md) — the portable build
- [AGENTS.md](AGENTS.md) — orientation and working agreements for coding agents
