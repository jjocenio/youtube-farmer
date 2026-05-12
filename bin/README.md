# Video Assembly Script

`assemble.py` generates narration, images, and sound effects, then assembles a final video with MoviePy.

The script is stored in `bin/`, but it must be run from inside a video project directory because it reads:

- `.env` from the project directory, with fallback to `bin/.env`
- `manifest.json` by default, or any input JSON file you pass with `--input`
- optional background music files in the project folder

## Requirements

- Python 3.10+
- FFmpeg available on your system path
- API access for:
  - ElevenLabs
  - fal.ai
  - Freesound

## Create A Python Environment

From the repository root:

```bash
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

If your machine exposes a different Python 3.10+ binary, use that instead.

## Install Dependencies

```bash
pip install moviepy httpx pydantic tqdm numpy
```

## Configure Environment Variables

Use the tracked template at the repo root:

```bash
cp .env.template /path/to/your/video-project/.env
```

Then fill in the real values in that `.env`.

Load order:

- `/path/to/your/video-project/.env`
- `bin/.env` as a fallback

Minimum required variables:

```env
ELEVENLABS_API_KEY=...
ELEVENLABS_VOICE_ID=...
FAL_KEY=...
FREESOUND_API_KEY=...
FREESOUND_CLIENT_ID=...
```

Useful optional variables:

```env
ELEVENLABS_MODEL_ID=eleven_multilingual_v2
ELEVENLABS_OUTPUT_FORMAT=mp3_44100_128
ELEVENLABS_STABILITY=0.35
ELEVENLABS_SIMILARITY_BOOST=0.75
ELEVENLABS_STYLE=0.0
ELEVENLABS_MAX_CONCURRENCY=2
ELEVENLABS_MUSIC_MODEL_ID=music_v1
ELEVENLABS_MUSIC_OUTPUT_FORMAT=mp3_44100_128
ELEVENLABS_SFX_MODEL_ID=eleven_text_to_sound_v2
ELEVENLABS_SFX_OUTPUT_FORMAT=mp3_44100_128
ELEVENLABS_SFX_PROMPT_INFLUENCE=0.5
FAL_MODEL=fal-ai/flux/dev
DEFAULT_MUSIC_INTENSITY=0.25
SFX_VOLUME=0.22
SFX_DUCKING_LEVEL=0.65
ENABLE_OUTRO=true
OUTRO_NARRATION_TEXT=If you enjoyed this video, please like, subscribe, and let me know what Cold War story we should cover next.
OUTRO_GAP_SECONDS=1.0
```

## Expected Project Layout

Example:

```text
my-video-project/
  .env
  manifest.json
  youtube.md
```

During execution, the script creates:

```text
assets/
  narration/
  images/
  sfx/
  music/
  outro/
output/
```

Current SFX behavior:

- the pipeline reads `scene.sfx` when present
- it searches Freesound first using `query` and `filters`
- if Freesound returns no results, it falls back to `fallback_prompt`
- legacy `sfx_prompt` still works as a fallback

## Shorts Mode

You can render a 9:16 short from a JSON input file instead of `manifest.json`:

```bash
cd /path/to/your/video-project
python /Users/jarbas/Documents/youtube-farmer/bin/assemble.py --input short_1.json --shorts
```

Shorts mode behavior:

- renders at `1080x1920`
- reads the input file you pass with `--input`
- prefers `background_music_shorts.mp3` if it exists
- if `background_music_shorts.mp3` is missing, it skips background music instead of generating a new track
- skips the AI disclosure intro
- skips the CTA outro
- ignores per-scene `music_intensity`
- renders scene images in portrait mode so short-form text stays in frame

## Manifest Format

Preferred format:

```json
{
  "metadata": {
    "output_filename": "assembled.mp4",
    "fps": 24
  },
  "timeline": [
    {
      "index": 1,
      "narration": "Opening narration text.",
      "visual_prompt": "A cinematic archival-style image prompt.",
      "sfx_prompt": "Low dramatic whoosh",
      "duration": 8.0
    }
  ]
}
```

The script also supports the repo's older `scenes` manifest format and adapts it automatically.

## Thumbnail Prompt

If `youtube.md` exists in the project directory, the script will look for a `Fal.ai Prompt:` entry and generate:

```text
assets/thumbnail.png
```

Example:

```md
## Fal.ai Prompt

Cinematic YouTube thumbnail, tense Cold War bunker, red phone, bold contrast, archival thriller style
```

## Run The Script

Change into your project directory, then call the script from `bin/`:

```bash
cd /path/to/your/video-project
python /Users/jarbas/Documents/youtube-farmer/bin/assemble.py
```

Useful options:

```bash
python /Users/jarbas/Documents/youtube-farmer/bin/assemble.py --limit 2
python /Users/jarbas/Documents/youtube-farmer/bin/assemble.py --force-audio
python /Users/jarbas/Documents/youtube-farmer/bin/assemble.py --force-images --force-sfx
python /Users/jarbas/Documents/youtube-farmer/bin/assemble.py --force-music
python /Users/jarbas/Documents/youtube-farmer/bin/assemble.py --threads 8
python /Users/jarbas/Documents/youtube-farmer/bin/assemble.py --sample 20:60
python /Users/jarbas/Documents/youtube-farmer/bin/assemble.py --input short_1.json --shorts
python /Users/jarbas/Documents/youtube-farmer/bin/publish.py --dry-run
python /Users/jarbas/Documents/youtube-farmer/bin/publish.py --sync-existing --dry-run
python /Users/jarbas/Documents/youtube-farmer/bin/publish.py --shorts
```

`--sample <length>:<offset>` affects rendering only. It still reuses the normal generated assets, but writes a shorter test export for just that time window from the assembled timeline.

## What The Script Does

- validates `manifest.json` with Pydantic
- creates missing asset folders automatically
- skips existing assets unless a matching `--force-*` flag is provided
- generates narration with ElevenLabs
- generates images with fal.ai
- generates optional sound effects with ElevenLabs
- generates background music from `metadata.bg_music_prompt` with ElevenLabs into `assets/music/background_music.mp3`
- in shorts mode, uses `background_music_shorts.mp3` if available and otherwise leaves music off
- reads `youtube.md`, extracts a `Fal.ai Prompt`, and generates `assets/thumbnail.png`
- appends a CTA outro at the end of long-form videos
- sets each scene duration from the generated narration length
- applies a Ken Burns zoom to scene images
- layers narration, SFX, and looping background music
- scales background music per scene using each scene's `music_intensity`
- ducks background music during narration
- writes the final video into `output/`

## Outro

The assembly step can extend the final scene with a like-and-subscribe outro automatically.

Behavior:

- keeps the same last image on screen
- inserts a short silence gap after the last scene narration
- then plays the outro narration

The narration file used is:

```text
assets/outro/like_subscribe_narration.mp3
```

The gap before the CTA narration is controlled by:

```env
OUTRO_GAP_SECONDS=1.0
```

If you want to disable the outro entirely:

```env
ENABLE_OUTRO=false
```

## Output

By default, the final video is written to:

```text
output/assembled.mp4
```

If `metadata.output_filename` is present in `manifest.json`, that filename is used instead.
If you pass `--input short_1.json` and no `output_filename` exists, the output defaults to `output/short_1.mp4`.

## Publish To YouTube

`publish.py` is run from inside a video project directory, just like `assemble.py`.

It reads:

- `youtube.md`
- `manifest.json`
- the rendered video from `output/`
- `assets/thumbnail.png` if it exists

Install the extra publishing dependencies:

```bash
pip install google-api-python-client google-auth-oauthlib google-auth-httplib2
```

Add Google OAuth credentials:

- put your OAuth desktop app file at `bin/client_secrets.json`
- the script will create `bin/youtube_token.json` on first login

Current `youtube.md` support:

- chooses a title from `## Selected Title` if present
- otherwise falls back to `## Final Title`, `## Title`, then the proposed titles
- builds the description from `The Hook`, `Chapter Timestamps`, and `SEO Paragraph`
- parses tags from `## Tags`

Shorts publishing:

- use `publish.py --shorts` for vertical short-form uploads
- shorts use the SEO title from `youtube.md`
- shorts upload the newest MP4 in `output/` unless `--file` is passed
- shorts upload hidden YouTube tags from the Shorts tags table
- shorts keep visible hashtags in the description footer

Publishing also writes an internal episode review file at `assets/editorial_review.md` with the selected title source, source basis, and reminder notes for human fact-checking and editorial judgment.

Assembly inserts a short AI disclosure intro before the main program when `ENABLE_AI_DISCLOSURE_INTRO=true`:

- if `AI_DISCLOSURE_INTRO_VIDEO` points to an existing video file, that clip is prepended before the main program
- if no intro video is configured, the script falls back to a generated disclosure clip

To prepend that same disclosure intro onto an already assembled export, run:

```bash
python /Users/jarbas/Documents/youtube-farmer/bin/prepend_ai_disclosure.py
```

Publishing options for already-uploaded videos:

- `--sync-existing` updates metadata and thumbnail only
- `--replace-existing` sets the current video to private, then uploads the new media file as a replacement
- `--shorts` reads `youtube.md` from the current project, uses the Shorts SEO title, uploads the hidden Shorts tags table, and uses the visible hashtag block in the description
- `--shorts` also prefers the newest MP4 in `output/` unless you pass `--file`

Shorts publishing notes:

- Shorts metadata lives in `youtube.md`
- the title should come from the Shorts SEO title
- hidden YouTube tags come from the Shorts tags table
- visible hashtags stay in the description footer

Shorts publishing:

- use `--shorts` to publish the newest `*.mp4` from `output/` as the Shorts upload
- if you want to target a specific short export, you can still pass `--file`

Run a dry-run first:

```bash
cd /path/to/your/video-project
python /Users/jarbas/Documents/youtube-farmer/bin/publish.py --dry-run
```

Then upload:

```bash
python /Users/jarbas/Documents/youtube-farmer/bin/publish.py
```

To sync metadata/thumbnail for an already uploaded video without re-uploading the media file:

```bash
python /Users/jarbas/Documents/youtube-farmer/bin/publish.py --sync-existing --dry-run
python /Users/jarbas/Documents/youtube-farmer/bin/publish.py --sync-existing
```

If needed, target a specific video explicitly:

```bash
python /Users/jarbas/Documents/youtube-farmer/bin/publish.py --sync-existing --video-id VIDEO_ID
```

Useful overrides:

```bash
python /Users/jarbas/Documents/youtube-farmer/bin/publish.py --privacy-status unlisted
python /Users/jarbas/Documents/youtube-farmer/bin/publish.py --category-id 27
python /Users/jarbas/Documents/youtube-farmer/bin/publish.py --file output/assembled.mp4
```

After upload, the script writes:

```text
assets/youtube_upload.json
```
