#!/usr/bin/env python3
"""Generate a reusable AI disclosure intro video for project exports."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from typing import Optional

import httpx
import numpy as np
from moviepy import AudioClip, AudioFileClip, ColorClip, CompositeAudioClip, ImageClip, concatenate_videoclips
from PIL import Image, ImageDraw, ImageFont


VIDEO_SIZE = (1920, 1080)
DEFAULT_OUTPUT_NAME = "ai_disclosure_intro.mp4"
DISCLOSURE_TEXT = "This video uses AI-generated imagery and visualizations to illustrate historical events."
TITLE_TEXT = "AI-Assisted Visualization"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=str, default=None, help="Output MP4 path.")
    parser.add_argument("--duration", type=float, default=10.0, help="Minimum intro duration in seconds.")
    parser.add_argument("--voice-id", type=str, default=None, help="Override ElevenLabs voice id.")
    parser.add_argument("--model-id", type=str, default=None, help="Override ElevenLabs model id.")
    parser.add_argument("--dry-run", action="store_true", help="Build the assets but do not write the MP4.")
    return parser.parse_args()


def load_env(project_dir: Path, script_dir: Path) -> None:
    for dotenv_path in (project_dir / ".env", script_dir / ".env"):
        if not dotenv_path.exists():
            continue
        for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


async def generate_narration(text: str, destination: Path, voice_id: str, model_id: str) -> Path:
    api_key = require_env("ELEVENLABS_API_KEY")
    output_format = os.getenv("ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    payload = {
        "text": text,
        "model_id": model_id,
        "output_format": output_format,
    }
    headers = {
        "xi-api-key": api_key,
        "accept": "audio/mpeg",
        "content-type": "application/json",
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(response.content)
    return destination


def build_static_card(duration: float) -> ImageClip:
    overlay = Image.new("RGBA", VIDEO_SIZE, (8, 8, 8, 255))
    draw = ImageDraw.Draw(overlay)
    try:
        title_font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 78)
        body_font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 46)
    except Exception:
        title_font = ImageFont.load_default()
        body_font = ImageFont.load_default()

    title_bbox = draw.textbbox((0, 0), TITLE_TEXT, font=title_font)
    disclosure_bbox = draw.textbbox((0, 0), DISCLOSURE_TEXT, font=body_font)

    title_x = (VIDEO_SIZE[0] - (title_bbox[2] - title_bbox[0])) / 2
    disclosure_x = (VIDEO_SIZE[0] - (disclosure_bbox[2] - disclosure_bbox[0])) / 2

    draw.text((title_x, 360), TITLE_TEXT, fill=(255, 255, 255, 255), font=title_font)
    draw.text((disclosure_x, 490), DISCLOSURE_TEXT, fill=(230, 230, 230, 255), font=body_font)
    return ImageClip(np.array(overlay)).with_duration(duration)


def build_video(intro_audio: Path, output: Path, min_duration: float) -> None:
    audio = AudioFileClip(str(intro_audio))
    duration = max(min_duration, audio.duration)
    silence = AudioClip(lambda t: np.zeros((1,)), duration=duration, fps=44100)
    clip = build_static_card(duration).with_audio(CompositeAudioClip([silence, audio]))
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        clip.write_videofile(
            str(output),
            fps=24,
            codec="libx264",
            audio_codec="aac",
            threads=4,
            preset="medium",
        )
    finally:
        audio.close()
        silence.close()
        clip.close()


def main() -> int:
    args = parse_args()
    project_dir = Path.cwd()
    script_dir = Path(__file__).resolve().parent
    load_env(project_dir, script_dir)

    voice_id = args.voice_id or os.getenv("ELEVENLABS_VOICE_ID")
    if not voice_id:
        raise RuntimeError("Missing ElevenLabs voice id. Set ELEVENLABS_VOICE_ID or pass --voice-id.")

    model_id = args.model_id or os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")
    output = Path(args.output).expanduser() if args.output else project_dir / "assets" / "intro" / DEFAULT_OUTPUT_NAME
    audio_path = output.with_suffix(".mp3")

    if args.dry_run:
        print(f"Would generate narration: {audio_path}")
        print(f"Would generate video: {output}")
        return 0

    asyncio.run(generate_narration(DISCLOSURE_TEXT, audio_path, voice_id, model_id))
    build_video(audio_path, output, args.duration)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
