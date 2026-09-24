# Filler and You

## Intro

***"What is filler? Why do I need it?" or, how I learned to stop worrying and love commercials.***

In the before times, back when iPads and Netflix were considered science fiction, there was no choosing what to watch, and there was no binging a show until it's suddenly 11:30pm and you realize you haven't eaten anything all day. Every half hour of programming had 6-8 minutes of interruptions where someone was trying to sell you something, and this is what we refer to as "filler". In plain English, it's anything that aired on television that wasn't a show, so all the commercials, station bumpers ("You're watching Cartoon Network!"), etc.

**"Well why does that matter?"** you might ask yourself, "I can watch whatever I want whenever I want now…". Yes, this is true, but if you're reading this it probably means, for one reason or another, you have personal interest in recreating the broadcast/cable television experience, which might extend all the way to a desire to create your own custom full 24 hour schedules. The problem you will quickly run into, is those 6-8 minutes mentioned in the previous paragraph: because networks would only air shows with room for their commercials (with a few exceptions of non-ad supported networks like HBO) every TV show produced in the last… well, ever, has a runtime in the 25 minute range. If you want to fit shows to a 30 minute schedule (ie a new show starting on every half hour mark), you have 5ish minutes of dead air to deal with.

**Enter, filler:** content to "fill" the time gaps created by the ghosts of commercials past. This could be anything you want as long as it's short, some people use music videos or youtube clips that they enjoy, but the most common solution is to use actual real commercials.

In this guide I will be going over my personal workflow when it comes to filler acquisition, editing, organization, and how best to use ErsatzTV (ETV) smart collections in order to recreate the experience of real television in the most customizable and flexible way possible. This is by no means the definitive workflow for accomplishing this, only what I've found works best for me so far, so feel free to modify it to better fit your needs.

## Acquisition

***"Where can I download mass quantities of commercials?", and other things uttered by the deranged.***

The two biggest sources of filler are going to be Youtube, and archive.org, but I'll mostly focus on Youtube as it tends to be easier to work with: there's a rather large community of people buying old VHS tapes and DVRs at thrift stores or garage sales, digitizing their contents, cutting out the actual shows (leaving only the commercials), and then uploading the resulting videos as effectively large compilations. Just search Youtube for whatever era and/or channel you're interested in + commercials/bumpers/what-have-you ("90s commercials", "80s MTV commercials", "Nickelodeon bumpers", etc), and you'll get a long list of such videos. It is also possible to find channels and playlists dedicated to uploads of individual commercials, it's up to you if you want to get large compilations of commercials that aired together in the time period you're interested in, or if you want to focus more on these individual uploads that may be easier to deal with but will likely be more varied in content.

The next question is, how do you download these? There's a myriad of ways to download Youtube videos, from online tools to command line level yt-dlp. I personally run a TubeArchivist instance and utilize that to grab filler content with all of its metadata so I can easily process it at a later date, but feel free to use whatever tool you're most comfortable with.

In the next section, I will detail how I go about editing the large compilation videos down into individual usable files. If you decide to only deal with individual uploads, you can skip this section and move on to Organization.

## Editing

***"How do I turn this single, large, convenient, compilation of commercials into 80, small, inconvenient files?"***

My secret: **a tool called LosslessCut.** 

Pretty much every other editing tool requires transcoding the entire file, which not only takes a lot of time but results in a potential loss of quality in the output file. A tool called ffmpeg can create lossless (no quality loss), basically instant cuts in video files, but it's limited to only cutting along predetermined points in the video that appear every few seconds (in short: videos are compressed by only storing data between "keyframes", and these types of cuts can only be made at these keyframe borders). This results in very imprecise cuts in output videos, adding a few seconds of extra unwanted footage to the beginning and end of every clip, which is extremely detrimental when the commercial itself might only be 15 seconds long.

In comes LosslessCut, with their experimental "smart cut" feature: it uses ffmpeg to create a losslessly cut clip at the closest keyframes inside the borders of your desired commercial, then separately creates two small transcodes at the beginning and end of the clip in order for it to start and stop on the exact frame you desire, before finally combining the three parts into one whole video file. **In simple terms,** only the first few seconds, and the last few seconds will suffer any amount of quality loss, while the bulk of your cut out commercial will be at the exact quality of the source video!

It also has a pretty simple and easy to use interface, with tools to detect the black frames or silence that occur between commercials, allowing you to automate the cutting process (at least partially, it's rarely perfect and will still require manual review and in many cases manual editing). For those interested, I will go into detail about my workflow using this program to convert large compilations of commercials into individual files, if you already have an editor of choice that you prefer, feel free to skip on to the next section.

[Placeholder: LosslessCut tutorial, probably want to structure it around screenshots]


## Organization

***No, "commercial (2).mp4" through "commercial (156).mp4" is not a good naming scheme.***

This is a big one, and arguably the most important to get right if you really want to unlock the full potential of ETV's smart collections feature (which will be covered in the next section). Filler content in ETV will only ever be as good as the organization of the underlying folder structure: every folder name in a file's path will be a searchable tag

## Collections

***"Organization 2: Electric Boogaloo", or, "Reciting the ancient incantations to summon both Cartoon Network promos and 20 year old Chuck Norris' Total Gym commercials."***

## Glossary

**Back To:**

    A type of bumper that goes between the end of a commercial break and the start of the next segment of a specific show, typically in the form of "Now, we're back to <show>!".

**Be Right Back:**

	A type of bumper that goes between the end of one segment of a specific show and the commercial break, typically in the form of "We'll be right back to <show>!".

**Blocks:**

	A section of programming with unique filler not otherwise used in the general network programming. May contain its own bumpers, promo material, intros, etc.

**Bumper/Ident:**

	An element that acts as a transition to or from commercial breaks. Idents were originally born from legal requirements for traditional broadcast radio (and later, television) to identify themselves periodically ("You are watching CBS"). Adopted by cable networks and evolved into the modern (and often more elaborate) bumper as a form of brand recognition and to raise viewer retention. Typically 30 seconds or less, almost never longer than 60.
	
**Commercial:**

	Non-network specific advertisements, can play on multiple channels/networks.

**Ending:**

	Distinct from outro. Show specific ending (credits, etc) that's not technically filler, and is usually baked right into the episode. However, some releases (mainly DVD rips) may cut them out and provide them as separate files in order to save on storage space. Most common in anime.

**Interstitial:**

	Network specific promo material used to pad out larger time segments, my personal rule of thumb tends to be anything longer than 60 seconds.

**Intro:**

	Network (or often, block) specific bumpers that air between commercials and the start of a specific show, Toonami intros being a prominent example.

**Opening:**

	Distinct from intro. Show specific opening that's not technically filler, and is usually baked right into the episode. However, some releases (mainly DVD rips) may cut them out and provide them as separate files in order to save on storage space. Most common in anime.

**Outro:**

	Network (or often, block) specific bumpers that air between the end of a specific show or (more commonly) block and commercials.Most often used to signal the end of a block (Adult Swim sign offs as an example).

**Promo:**

	Network specific promotional material, basically, commercials for shows or other events airing on that network.

**Up Next:**

	A type of bumper that announces the shows airing after the current/previous one, often in a "Now, Then, Later" format telling you the next three scheduled shows.
