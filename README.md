# CommCut

A suite of tools to manage, rename, organize, and edit pre-made filler for the purposes of linear television emulation

- Maps onto existing filler organization scheme (or creates one based on given criteria)
- Rename Wizard (Throw existent individual clips into import folder, programattically rename them, dynamically save them to mapped filler export folder)
- Editing Wizard designed specifically with cutting compilations of commercials into individual clips in mind

# Project Scope

## Export Folder Organization Scheme

Built within the framework of my personal filler organization scheme, each clip is given a tag for each of:
- Title
- Network
- Block (Optional)
- Filler Type
- Year (If unknown, fallback to Time Period)
- Time Period (eg, decade/5 year period or era)
- Show (Optional)
- Special (Optional, this could be any number of organizational things, eg Holiday, Target Demographic, etc, basically any special circumstance that doesn't apply elsewhere)
- Length (Optionally used, only if needed for file differentiation, ie some Promos have a 60 sec, a 30 sec, and a 15 sec version)
- Information (Optionally used, denotes something about a file, eg Upscale, Remastered, Low Quality, Left Only Audio, etc)

These tags are then used to organize and name the resulant files on export. Defaults will be what I personally use:

**Folder organization:** `<Network>/<Block (O)>/<Type>/<Time Period>/<Special (O)>`

**Naming Scheme:** `<Network> - <Type> - <Year or Time Period> - <Block or Special> <Title> (<Length (O)> <Information (O)>)`

**Example:** `Cartoon Network/Blocks/Toonami/Promo/Cartoon Network - Promo - 2000 - Toonami Worlds Finest (30 Sec Remastered).mp4`

## Library Sharing

Full library import and export

Given a copy of someone else's filler library, and their organization + file naming schemes, individual videos could theoretically be extracted, and renamed + reorganized to mesh perfectly into your (differently organized) library in a completely automated fashion, as all the tags of a particular file can be derived solely from its filename and file path.

## Rename Wizard

Rename and organize filler which has already been cut into individual clips:

- Drop video files which are already individual clips into an import folder, then run the Rename Wizard
- The first item will appear with both video playback (so you can watch the clip) and a list of text inputs where you can set each of the needed tags
- Each tag input will have a toggle lock button, allowing you to preserve what's been entered into the next video (making batch processing of similar video easier)
- When satisfied, press export, the video will be renamed using your entered tags according to the naming scheme, and similarly saved in the appropriate folder in your export directory
- The next video in the import folder will load and the process can be repeated

## Editing Wizard

Cut large compilations which contain multiple individual filler clips up, dynamically name and organize them according to set criteria: 

- Load a filler compilation video which contains multiple individual filler clips
- It runs ffmpeg's black screen detect, possibly also silence detect + some other heuristics (ie, ignore rapid concurrent detections or detect long spans without them) to cut the source video up into "Unedited" Segments
- It loads the first detected Unedited Segment into an editing interface, with video playback and a timeline (which will optionally show keyframes)
- Timeline buttons from left to right:
   - Add previous segment (expand the current timeline by adding the previously detected segment to the left)
   - Clip start (cuts the start of the clip at this point, discarding everything to the left)
   - Timeline navigation (keyframe + frame back/advance, play/pause button)
   - Clip end (cuts the end of the clip at this point, the next unedited segment will now begin after this point)
   - Add next segment (expand the current timeline by adding the next detected segment to the right)
- Below you will set the desired tags, each with a toggle lock to preserve what was entered into the next unedited segment
- When everything is satisfactory, press the Stage Changes button, and the next unedited segment will load up
- When the last segment is reached and staged, you are given the opportunity to review all edited segments
- If everything looks right, press Export, and each edited segment will be "Smart Cut" out using ffmpeg (Basically the same as the LosslessCut feature)
   - For each Edited Segment, at the start and end cut points, the innermost keyframe is found
   - A lossless stream copy (`-c`) is made between these two keyframes, leaving out the bit at the start and the bit at the end
   - A transcoded frame perfect cut is made between the desired starting cut point, and the first keyframe
   - Ditto for the bit of missing video at the end
   - The two transcoded bits are joined to the front and back of the stream copy, creating a composite clip
   - The resultant clip is as high quality as it can possibly be, with the interior keyframes being identical to the source
   - Only the first and last, partial keyframe segments, suffer quality loss from transcoding
   - If the Edited Segment only contains *one* keyframe, the entire thing is extracted via transcode
