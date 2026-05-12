# Project Journal

## Purpose

This repo is a local workflow for building and publishing YouTube history videos, currently focused on the `cold_war_chronicles` channel.

The main automation lives in:

- [bin/assemble.py](/Users/jarbas/Documents/youtube-farmer/bin/assemble.py)
- [bin/publish.py](/Users/jarbas/Documents/youtube-farmer/bin/publish.py)
- [bin/README.md](/Users/jarbas/Documents/youtube-farmer/bin/README.md)
- [.env.template](/Users/jarbas/Documents/youtube-farmer/.env.template)

## High-Level Workflow

1. Work inside a specific video project directory under `cold_war_chronicles/...`
2. Run `assemble.py` from inside that video directory
3. Review/render/sample the video
4. Use `publish.py` to upload or sync metadata on YouTube
5. Use `--shorts` flows for vertical short-form exports and uploads when needed

Both scripts are designed to be run from inside the video project directory, not from the repo root.

## Project Layout

Typical video project:

```text
cold_war_chronicles/<video_slug>/
  manifest.json
  youtube.md
  youtube_short_1.md
  short_1.json
  assets/
    narration/
    images/
    sfx/
    music/
    outro/
    thumbnail.png
  output/
    assembled.mp4
```

Shared repo-level reusable assets are also used:

```text
assets/
  music/
  outro/
  intro/
```

## `assemble.py` Behavior

### Inputs

- `manifest.json`
- `short_*.json` when rendering Shorts
- `youtube.md`
- `.env` in the project directory, with fallback to `bin/.env`

### Manifest Support

The script supports:

1. Native normalized manifest shape:
   - `metadata`
   - `timeline`
   - each scene has `index`, `narration`, `visual_prompt`, `sfx_prompt`, `duration`

2. Current Cold War project shape:
   - `metadata`
   - `timeline`
   - scenes use fields like:
     - `timestamp_start`
     - `narration`
     - `visual_description`
     - `sfx_prompt`
     - `music_intensity`
     - `pause_after`
     - `transition`

3. Older repo shape using `scenes`

Important adaptation details:

- `visual_description` is mapped into `visual_prompt`
- `metadata.global_visual_style` is appended to the visual prompt
- scene duration is inferred from `timestamp_start` deltas when needed
- `metadata.voice_id` is honored if set to a real value instead of `TO_BE_SET`
- silent scenes are supported

### Asset Generation

Assets are generated into:

- `assets/narration/`
- `assets/images/`
- `assets/sfx/`
- `assets/music/`
- `assets/outro/`

File naming is mostly scene-based:

- `scene_{index:03d}_narration.mp3`
- `scene_{index:03d}_image.jpg`
- `scene_{index:03d}_sfx.mp3`

Other generated assets:

- `assets/music/background_music.mp3`
- `assets/music/background_music_shorts.mp3` if present for Shorts
- `assets/outro/like_subscribe_narration.mp3`
- `assets/intro/ai_disclosure_intro.mp4` if you generate the disclosure opener
- `assets/thumbnail.png`

### Generation Services

- Narration: ElevenLabs TTS
- Scene images: Fal.ai
- SFX: Freesound first, ElevenLabs fallback
- Background music: ElevenLabs music
- Thumbnail: Fal.ai using prompt extracted from `youtube.md`

### Music Hierarchy

Background music lookup order:

1. shared repo root:
   - `assets/music/background_music.mp3`
   - `assets/music/background_music.wav`
   - `assets/music/music.mp3`
   - `assets/music/music.wav`

2. local project:
   - `background_music.mp3`
   - `background_music.wav`
   - `music.mp3`
   - `music.wav`
   - `assets/music/background_music.mp3`
   - `assets/music/background_music.wav`
   - `assets/music/music.mp3`
   - `assets/music/music.wav`
   - `assets/background_music.mp3`
   - `assets/background_music.wav`

3. only if nothing exists, generate from `metadata.bg_music_prompt`

Shorts mode music behavior:

- prefer `background_music_shorts.mp3` if it exists
- otherwise skip background music entirely
- ignore `metadata.bg_music_prompt`

### Outro Hierarchy

Outro narration lookup order:

1. shared repo root:
   - `assets/outro/like_subscribe_narration.mp3`

2. local project:
   - `assets/outro/like_subscribe_narration.mp3`
   - `outro/like_subscribe_narration.mp3`

3. only if nothing exists, generate from `OUTRO_NARRATION_TEXT`

### Outro Behavior

Current behavior:

- there is no separate visual outro scene anymore
- the final scene is extended
- the same last image stays on screen
- a silence gap is inserted
- then the CTA narration plays

Config:

- `ENABLE_OUTRO=true|false`
- `OUTRO_GAP_SECONDS=...`
- `OUTRO_NARRATION_TEXT=...`

### Thumbnail Behavior

The script reads `youtube.md` and looks for a `Fal.ai Prompt` section/line.

If found, it generates:

- `assets/thumbnail.png`

If `youtube.md` is missing or no prompt is found, thumbnail generation is skipped with a message.

### Rendering Behavior

- 1920x1080 output
- 1080x1920 output in Shorts mode
- Ken Burns zoom on images
- sample rendering supported with `--sample <length>:<offset>`
- sample affects rendering only, not asset generation
- output rendering is skipped if the target output file already exists
- thumbnail generation is also skipped if `assets/thumbnail.png` already exists
- Shorts mode skips the disclosure intro and CTA outro
- Shorts mode ignores per-scene `music_intensity`

### Mixing Behavior

Audio layers:

1. narration
2. SFX
3. background music

Notable controls:

- `SFX_VOLUME`
- `SFX_DUCKING_LEVEL`
- `DEFAULT_MUSIC_INTENSITY`

Behavior:

- SFX is scaled with `SFX_VOLUME`
- SFX is ducked while narration is active
- background music is scaled by per-scene `music_intensity`, or falls back to `DEFAULT_MUSIC_INTENSITY`
- background music is looped continuously across the timeline, not restarted per scene
- music is ducked during narration

Shorts mode mixing:

- narration and SFX still render normally
- background music uses the shorts-specific file if available
- `music_intensity` is ignored

### Silent Scenes

Intentional silent scenes are supported.

If a scene has no narration:

- no narration asset is required
- duration comes from manifest duration/inferred duration
- image/SFX/music can still render

### Known Rendering Fixes Already Made

These were previous pain points and have already been addressed:

- Python 3.9 type-hint compatibility issues
- ElevenLabs 401 vs 429 retry handling
- open-file exhaustion during long renders
- sampled render subclip boundary errors
- full-duration audio bed for scenes with pauses
- SFX ducking under narration
- smoother Ken Burns via supersampling and float crop coordinates

If zoom motion still feels “steppy,” revisit:

- `KEN_BURNS_SUPERSAMPLE`
- zoom amount
- possibly lowering zoom delta further

## `publish.py` Behavior

### Purpose

Uploads a rendered video and thumbnail to YouTube, or syncs metadata on an existing uploaded video.

### Inputs

- `youtube.md`
- rendered video in `output/`
- optional `assets/thumbnail.png`
- `.env` in project dir, fallback to `bin/.env`
- OAuth files in `bin/`

### `youtube.md` Parsing

Title selection priority:

1. `## Selected Title`
2. `## Final Title`
3. `## Title`
4. Proposed titles

For proposed titles, preferred order is:

1. `Narrative / Dramatic`
2. `SEO-Focused`
3. `Clickbait / High-Curiosity`

Description is built from:

- `The Hook`
- `Chapter Timestamps`
- `SEO Paragraph`

Tags are parsed from:

- `## Tags`

Tag handling is sanitized conservatively to avoid YouTube `invalidTags` API errors.

Shorts metadata rules:

- Shorts still read `youtube.md` by default
- title comes from the Shorts SEO title
- hidden YouTube tags come from the Shorts tags table
- visible hashtags come from the Shorts `Hashtags` block
- `publish.py --shorts` uploads the newest MP4 in `output/` unless `--file` is passed

### Publish Modes

1. Upload mode
   - uploads the video file
   - uploads thumbnail if present
   - writes `assets/youtube_upload.json`

2. Sync mode
   - `--sync-existing`
   - updates title/description/tags/status/thumbnail for an existing YouTube video
   - does not re-upload the video file

3. Shorts mode
   - `--shorts`
   - reads `youtube.md` by default
   - uses the Shorts SEO title
   - uploads the newest MP4 in `output/` unless `--file` is passed
   - uploads hidden Shorts tags separately from visible hashtags

Existing video id resolution:

1. `--video-id`
2. `assets/youtube_upload.json`

### OAuth / Channel Gotcha

Important:

- the upload goes to the YouTube channel associated with the authenticated OAuth token
- this can accidentally target a regular account instead of the intended Brand Account

This already happened once in this project.

Practical rule:

- if the wrong channel is selected, delete `bin/youtube_token.json`
- reauthenticate and make sure the Brand Account/channel is the active one

### Current Weakness in Publish Flow

The script does not yet print the authenticated channel name/id before upload.

That would be a good future safety improvement.

## Environment / Python Notes

The old environment used Python 3.9 and hit warnings from Google libraries.

Recommended now:

- use Python 3.10+
- preferably Python 3.11

The user confirmed moving to a newer Python worked.

## Git / Ignore Notes

Important ignore behavior already added:

- `.env`
- OAuth secrets/tokens
- output mp4s
- assembled video outputs

Generated output MP4s were checked and were not currently tracked when ignore rules were added.

## User Preferences / Decisions Made

- keep SFX generation as-is for now, even though library reuse could save credits
- keep multi-title support in `youtube.md` because of manual experimentation with YouTube title testing
- do not implement title A/B automation through API because YouTube API does not expose that in a useful way
- outro should use the same last image, not a separate visual outro card
- add a gap before the CTA narration
- prefer shared reusable assets at repo root where practical

## Likely Next Good Improvements

If continuing this project later, likely next candidates are:

- print authenticated YouTube channel before upload/sync
- document or expose `KEN_BURNS_SUPERSAMPLE` in `.env.template`
- optional SFX library mode to reduce ElevenLabs credits
- more explicit manifest schema docs for the current `timeline` shape
- optional text-only fallback if outro narration exists but no shared/local outro setup is complete

## Useful Commands

Assemble full video:

```bash
cd cold_war_chronicles/<video_slug>
python ../../bin/assemble.py
```

Assemble sample:

```bash
python ../../bin/assemble.py --sample 20:60
```

Publish dry-run:

```bash
python ../../bin/publish.py --dry-run
```

Sync existing YouTube metadata dry-run:

```bash
python ../../bin/publish.py --sync-existing --video-id VIDEO_ID --dry-run
```

## Important Files To Check First Next Session

- [bin/assemble.py](/Users/jarbas/Documents/youtube-farmer/bin/assemble.py)
- [bin/publish.py](/Users/jarbas/Documents/youtube-farmer/bin/publish.py)
- [bin/README.md](/Users/jarbas/Documents/youtube-farmer/bin/README.md)
- [.env.template](/Users/jarbas/Documents/youtube-farmer/.env.template)
- a representative project:
  - [cold_war_chronicles/able_archer_the_exercise_that_almost_started_ww3/manifest.json](/Users/jarbas/Documents/youtube-farmer/cold_war_chronicles/able_archer_the_exercise_that_almost_started_ww3/manifest.json)
  - [cold_war_chronicles/able_archer_the_exercise_that_almost_started_ww3/youtube.md](/Users/jarbas/Documents/youtube-farmer/cold_war_chronicles/able_archer_the_exercise_that_almost_started_ww3/youtube.md)
