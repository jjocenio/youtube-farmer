#!/usr/bin/env python3
"""Generate narration/images/SFX assets and assemble a video from manifest.json.

Run this script from inside a project directory. It loads `.env` and `manifest.json`
from the current working directory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Union

import httpx
import numpy as np
from moviepy import (
    AudioClip,
    AudioFileClip,
    ColorClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ImageClip,
    VideoClip,
    VideoFileClip,
    concatenate_audioclips,
    concatenate_videoclips,
)
from pydantic import BaseModel, Field, ValidationError
from PIL import Image
from tqdm import tqdm


VIDEO_SIZE = (1920, 1080)
DEFAULT_FPS = 24
DEFAULT_EXPORT_THREADS = 4
DEFAULT_MANIFEST = "manifest.json"
DEFAULT_OUTPUT_NAME = "assembled.mp4"
FADE_DURATION = 1.5
MAX_CONCURRENCY = 5
DEFAULT_ELEVENLABS_CONCURRENCY = 2
THUMBNAIL_NAME = "thumbnail.png"
OUTRO_NARRATION_NAME = "like_subscribe_narration.mp3"

ELEVENLABS_BASE_URL = "https://api.elevenlabs.io"
FAL_QUEUE_BASE_URL = "https://queue.fal.run"


class Scene(BaseModel):
    index: int
    narration: str
    visual_prompt: str
    sfx_prompt: str = ""
    duration: float = Field(gt=0)
    pause_after: float = 0.0
    music_intensity: Optional[float] = None
    transition: Optional[str] = None


class Manifest(BaseModel):
    metadata: dict[str, Any] = Field(default_factory=dict)
    timeline: list[Scene]


@dataclass
class SceneAssets:
    scene: Scene
    narration_path: Path
    image_path: Path
    sfx_path: Optional[Path]


@dataclass
class CostTracker:
    narration_characters: int = 0
    sfx_characters: int = 0
    images_generated: int = 0
    narration_generated: int = 0
    sfx_generated: int = 0
    music_generated: int = 0
    outro_generated: int = 0

    @property
    def total_characters(self) -> int:
        return self.narration_characters + self.sfx_characters


class ConfigError(RuntimeError):
    pass


class AssetGenerationError(RuntimeError):
    pass


class NonRetryableAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class SampleWindow:
    length: float
    offset: float

    @property
    def end(self) -> float:
        return self.offset + self.length


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force-audio", action="store_true", help="Re-generate narration assets.")
    parser.add_argument("--force-images", action="store_true", help="Re-generate image assets.")
    parser.add_argument("--force-sfx", action="store_true", help="Re-generate SFX assets.")
    parser.add_argument("--force-music", action="store_true", help="Re-generate background music.")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N scenes.")
    parser.add_argument(
        "--sample",
        type=str,
        default=None,
        help="Render only a time window in the form <length>:<offset>, in seconds. Example: 20:60",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=DEFAULT_EXPORT_THREADS,
        help=f"MoviePy export threads (default: {DEFAULT_EXPORT_THREADS}).",
    )
    return parser.parse_args()


def load_dotenv(dotenv_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not dotenv_path.exists():
        return values

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        values[key] = value
        os.environ.setdefault(key, value)
    return values


def load_runtime_env(project_dir: Path, script_dir: Path) -> dict[str, str]:
    loaded: dict[str, str] = {}
    for dotenv_path in (project_dir / ".env", script_dir / ".env"):
        if dotenv_path.exists():
            loaded.update(load_dotenv(dotenv_path))
    return loaded


def ensure_directories(project_dir: Path) -> dict[str, Path]:
    asset_root = project_dir / "assets"
    paths = {
        "asset_root": asset_root,
        "narration": asset_root / "narration",
        "images": asset_root / "images",
        "sfx": asset_root / "sfx",
        "music": asset_root / "music",
        "outro": asset_root / "outro",
        "output": project_dir / "output",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def scene_file_path(directory: Path, index: int, asset_type: str, ext: str) -> Path:
    return directory / f"scene_{index:03d}_{asset_type}.{ext}"


def parse_sample_window(raw_value: Optional[str]) -> Optional[SampleWindow]:
    if not raw_value:
        return None

    parts = raw_value.split(":", 1)
    if len(parts) != 2:
        raise ValueError("Sample must use the format <length>:<offset> in seconds, for example 20:60.")

    try:
        length = float(parts[0])
        offset = float(parts[1])
    except ValueError as exc:
        raise ValueError("Sample values must be numeric seconds, for example 20:60.") from exc

    if length <= 0:
        raise ValueError("Sample length must be greater than 0.")
    if offset < 0:
        raise ValueError("Sample offset must be 0 or greater.")

    return SampleWindow(length=length, offset=offset)


def load_manifest(manifest_path: Path, limit: Optional[int] = None) -> Manifest:
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))

    if is_native_manifest(raw):
        manifest = Manifest.model_validate(raw)
    elif is_current_project_manifest(raw):
        manifest = adapt_current_project_manifest(raw)
    else:
        manifest = adapt_legacy_manifest(raw)

    if limit is not None:
        manifest = Manifest(metadata=manifest.metadata, timeline=manifest.timeline[:limit])

    if not manifest.timeline:
        raise ValueError("Manifest contains no scenes to process.")
    return manifest


def is_native_manifest(raw: dict[str, Any]) -> bool:
    timeline = raw.get("timeline")
    if not isinstance(timeline, list) or not timeline:
        return False
    first = timeline[0]
    return isinstance(first, dict) and {"index", "visual_prompt", "duration"}.issubset(first.keys())


def is_current_project_manifest(raw: dict[str, Any]) -> bool:
    timeline = raw.get("timeline")
    if not isinstance(timeline, list) or not timeline:
        return False
    first = timeline[0]
    return isinstance(first, dict) and {"narration", "visual_description"}.issubset(first.keys())


def adapt_current_project_manifest(raw: dict[str, Any]) -> Manifest:
    metadata = dict(raw.get("metadata", {}))
    global_visual_style = metadata.get("global_visual_style", "").strip()
    timeline_entries = raw.get("timeline", [])
    scenes: list[Scene] = []

    for position, entry in enumerate(timeline_entries, start=1):
        visual_description = str(entry.get("visual_description", "")).strip()
        visual_prompt = join_prompt_parts([visual_description, global_visual_style])

        duration = infer_scene_duration(position, timeline_entries, entry)

        scenes.append(
            Scene(
                index=position,
                narration=str(entry.get("narration", "")).strip(),
                visual_prompt=visual_prompt,
                sfx_prompt=str(entry.get("sfx_prompt", "")).strip(),
                duration=duration,
                pause_after=float(entry.get("pause_after") or 0.0),
                music_intensity=float(entry["music_intensity"]) if entry.get("music_intensity") is not None else None,
                transition=str(entry["transition"]) if entry.get("transition") else None,
            )
        )

    return Manifest(metadata=metadata, timeline=scenes)


def join_prompt_parts(parts: list[str]) -> str:
    return ", ".join(part for part in parts if part)


def infer_scene_duration(position: int, timeline_entries: list[dict[str, Any]], entry: dict[str, Any]) -> float:
    if position < len(timeline_entries):
        current_start = entry.get("timestamp_start")
        next_start = timeline_entries[position].get("timestamp_start")
        try:
            delta = float(next_start) - float(current_start)
            if delta > 0:
                return delta
        except (TypeError, ValueError):
            pass

    pause_after = entry.get("pause_after")
    try:
        pause_value = float(pause_after) if pause_after is not None else 0.0
    except (TypeError, ValueError):
        pause_value = 0.0
    return max(1.0, 6.0 + pause_value)


def adapt_legacy_manifest(raw: dict[str, Any]) -> Manifest:
    scenes: list[Scene] = []
    metadata = {k: v for k, v in raw.items() if k != "scenes"}

    for position, entry in enumerate(raw.get("scenes", []), start=1):
        dialogue = entry.get("dialogue", [])
        narration_parts = [item.get("text", "").strip() for item in dialogue if item.get("speaker") == "NARRATOR"]
        narration = " ".join(part for part in narration_parts if part).strip()
        scenes.append(
            Scene(
                index=int(entry.get("id") or position),
                narration=narration,
                visual_prompt=entry.get("image_prompt", ""),
                sfx_prompt=entry.get("sfx_prompt", "") or "",
                duration=float(entry.get("approx_duration_sec") or 1.0),
            )
        )

    return Manifest(metadata=metadata, timeline=scenes)


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def optional_env(name: str, default: str) -> str:
    return os.getenv(name, default)


def mask_secret(value: str, *, prefix: int = 6, suffix: int = 4) -> str:
    if len(value) <= prefix + suffix:
        return value
    return f"{value[:prefix]}...{value[-suffix:]}"


def print_runtime_env_summary() -> None:
    print("Runtime environment")
    for key in ("ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "ELEVENLABS_MODEL_ID", "FAL_KEY", "FAL_MODEL"):
        value = os.getenv(key)
        if not value:
            print(f"  {key}=<missing>")
            continue
        if "KEY" in key:
            print(f"  {key}={mask_secret(value)} (len={len(value)})")
        else:
            print(f"  {key}={value}")


def pick_background_music(project_dir: Path, metadata: dict[str, Any]) -> Optional[Path]:
    candidates: list[Path] = []
    for key in ("background_music", "bg_music", "music", "background_music_path"):
        value = metadata.get(key)
        if value:
            candidates.append((project_dir / str(value)).resolve())

    candidates.extend(
        [
            project_dir / "background_music.mp3",
            project_dir / "background_music.wav",
            project_dir / "music.mp3",
            project_dir / "music.wav",
            project_dir / "assets" / "background_music.mp3",
            project_dir / "assets" / "background_music.wav",
        ]
    )

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def output_path(project_dir: Path, metadata: dict[str, Any]) -> Path:
    filename = metadata.get("output_filename") or DEFAULT_OUTPUT_NAME
    return project_dir / "output" / str(filename)


def sampled_output_path(base_output: Path, sample_window: Optional[SampleWindow]) -> Path:
    if sample_window is None:
        return base_output

    length_label = format_sample_seconds(sample_window.length)
    offset_label = format_sample_seconds(sample_window.offset)
    return base_output.with_name(f"{base_output.stem}_sample_{length_label}s_from_{offset_label}s{base_output.suffix}")


def format_sample_seconds(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return str(value).replace(".", "_")


async def request_with_backoff(
    func: Callable[[], Any],
    *,
    attempts: int = 5,
    base_delay: float = 1.0,
    operation_name: str,
) -> Any:
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return await func()
        except NonRetryableAPIError:
            raise
        except (httpx.HTTPError, httpx.TimeoutException, AssetGenerationError) as exc:
            last_error = exc
            if attempt == attempts:
                break
            delay = retry_delay_for_exception(exc, attempt, base_delay)
            print(
                f"[retry] {operation_name} failed on attempt {attempt}/{attempts}: {exc}. "
                f"Retrying in {delay:.1f}s...",
                file=sys.stderr,
            )
            await asyncio.sleep(delay)
    raise RuntimeError(f"{operation_name} failed after {attempts} attempts") from last_error


def retry_delay_for_exception(exc: Exception, attempt: int, base_delay: float) -> float:
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return max(float(retry_after), base_delay)
                except ValueError:
                    pass
    return base_delay * (2 ** (attempt - 1))


def raise_for_status_with_retry_policy(response: httpx.Response, service_name: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        response_text = exc.response.text.strip()
        detail = f"{service_name} returned HTTP {status_code}"
        if response_text:
            detail = f"{detail}: {response_text[:300]}"

        if status_code in {401, 403}:
            raise NonRetryableAPIError(
                f"{detail}. Check your API key, account permissions, and selected voice/model IDs."
            ) from exc

        if status_code in {400, 404, 422}:
            raise NonRetryableAPIError(detail) from exc

        raise


class ElevenLabsClient:
    def __init__(self, api_key: str, voice_id: str, model_id: str, timeout: float = 120.0) -> None:
        self.voice_id = voice_id
        self.model_id = model_id
        self.client = httpx.AsyncClient(
            base_url=ELEVENLABS_BASE_URL,
            timeout=httpx.Timeout(timeout),
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def generate_narration(self, text: str, destination: Path) -> int:
        async def _run() -> int:
            response = await self.client.post(
                f"/v1/text-to-speech/{self.voice_id}",
                params={"output_format": optional_env("ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128")},
                json={
                    "text": text,
                    "model_id": self.model_id,
                    "voice_settings": {
                        "stability": float(optional_env("ELEVENLABS_STABILITY", "0.35")),
                        "similarity_boost": float(optional_env("ELEVENLABS_SIMILARITY_BOOST", "0.75")),
                        "style": float(optional_env("ELEVENLABS_STYLE", "0.0")),
                    },
                },
            )
            raise_for_status_with_retry_policy(response, "ElevenLabs TTS")
            destination.write_bytes(response.content)
            header_value = response.headers.get("x-character-count")
            return int(header_value) if header_value and header_value.isdigit() else len(text)

        return await request_with_backoff(_run, operation_name=f"ElevenLabs narration -> {destination.name}")

    async def generate_sfx(self, text: str, duration_seconds: float, destination: Path) -> int:
        async def _run() -> int:
            response = await self.client.post(
                "/v1/sound-generation",
                params={"output_format": optional_env("ELEVENLABS_SFX_OUTPUT_FORMAT", "mp3_44100_128")},
                json={
                    "text": text,
                    "duration_seconds": max(0.5, min(duration_seconds, 30.0)),
                    "prompt_influence": float(optional_env("ELEVENLABS_SFX_PROMPT_INFLUENCE", "0.5")),
                    "model_id": optional_env("ELEVENLABS_SFX_MODEL_ID", "eleven_text_to_sound_v2"),
                },
            )
            raise_for_status_with_retry_policy(response, "ElevenLabs SFX")
            destination.write_bytes(response.content)
            header_value = response.headers.get("character-cost")
            return int(header_value) if header_value and header_value.isdigit() else len(text)

        return await request_with_backoff(_run, operation_name=f"ElevenLabs SFX -> {destination.name}")

    async def generate_music(self, prompt: str, duration_seconds: float, destination: Path) -> None:
        async def _run() -> None:
            response = await self.client.post(
                "/v1/music",
                params={"output_format": optional_env("ELEVENLABS_MUSIC_OUTPUT_FORMAT", "mp3_44100_128")},
                json={
                    "prompt": prompt,
                    "music_length_ms": int(max(3000, min(duration_seconds * 1000, 600000))),
                    "model_id": optional_env("ELEVENLABS_MUSIC_MODEL_ID", "music_v1"),
                    "force_instrumental": True,
                },
            )
            raise_for_status_with_retry_policy(response, "ElevenLabs Music")
            destination.write_bytes(response.content)

        await request_with_backoff(_run, operation_name=f"ElevenLabs music -> {destination.name}")


class FalClient:
    def __init__(self, api_key: str, model_id: str, timeout: float = 120.0) -> None:
        self.model_id = model_id
        self.submit_path, self.status_path = fal_model_paths(model_id)
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            headers={"Authorization": f"Key {api_key}", "Content-Type": "application/json"},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def generate_image(
        self,
        prompt: str,
        destination: Path,
        *,
        image_format: str = "jpeg",
        image_size: str = "landscape_16_9",
    ) -> None:
        async def _run() -> None:
            submit_response = await self.client.post(
                f"{FAL_QUEUE_BASE_URL}/{self.submit_path}",
                json={
                    "prompt": prompt,
                    "image_size": image_size,
                    "num_images": 1,
                    "format": image_format,
                },
            )
            submit_response.raise_for_status()
            payload = submit_response.json()
            request_id = payload["request_id"]

            result_url = await self._wait_for_result(request_id)
            result_response = await self.client.get(result_url)
            result_response.raise_for_status()
            result_payload = result_response.json()

            image_url = extract_fal_image_url(result_payload)
            if not image_url:
                raise AssetGenerationError(f"Fal response did not include an image URL for {destination.name}")

            image_response = await self.client.get(image_url)
            image_response.raise_for_status()
            destination.write_bytes(image_response.content)

        await request_with_backoff(_run, operation_name=f"Fal image -> {destination.name}")

    async def _wait_for_result(self, request_id: str) -> str:
        status_url = f"{FAL_QUEUE_BASE_URL}/{self.status_path}/requests/{request_id}/status"
        result_url = f"{FAL_QUEUE_BASE_URL}/{self.status_path}/requests/{request_id}"

        while True:
            response = await self.client.get(status_url, params={"logs": "true"})
            response.raise_for_status()
            payload = response.json()
            status = payload.get("status")

            if status == "COMPLETED":
                if payload.get("error"):
                    raise AssetGenerationError(payload["error"])
                return payload.get("response_url", result_url)

            if status not in {"IN_QUEUE", "IN_PROGRESS"}:
                raise AssetGenerationError(f"Unexpected Fal status: {status!r}")

            await asyncio.sleep(2.0 if status == "IN_PROGRESS" else 3.0)


def extract_fal_image_url(payload: dict[str, Any]) -> Optional[str]:
    images = payload.get("images") or payload.get("data", {}).get("images") or []
    if not images:
        return None
    first = images[0]
    if isinstance(first, str):
        return first
    if isinstance(first, dict):
        return first.get("url")
    return None


def fal_model_paths(model_id: str) -> tuple[str, str]:
    parts = [part for part in model_id.strip("/").split("/") if part]
    if len(parts) <= 2:
        normalized = "/".join(parts)
        return normalized, normalized
    submit_path = "/".join(parts)
    status_path = "/".join(parts[:2])
    return submit_path, status_path


async def gather_with_progress(tasks: Iterable[asyncio.Task[Any]], total: int, description: str) -> list[Any]:
    results: list[Any] = []
    with tqdm(total=total, desc=description, unit="scene") as progress:
        for task in asyncio.as_completed(list(tasks)):
            results.append(await task)
            progress.update(1)
    return results


async def acquire_assets(
    manifest: Manifest,
    asset_paths: dict[str, Path],
    args: argparse.Namespace,
) -> tuple[list[SceneAssets], CostTracker, Optional[Path], Optional[Path]]:
    fal_semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    elevenlabs_semaphore = asyncio.Semaphore(int(optional_env("ELEVENLABS_MAX_CONCURRENCY", str(DEFAULT_ELEVENLABS_CONCURRENCY))))
    metadata_voice_id = str(manifest.metadata.get("voice_id", "")).strip()
    voice_id = metadata_voice_id if metadata_voice_id and metadata_voice_id != "TO_BE_SET" else require_env("ELEVENLABS_VOICE_ID")
    elevenlabs = ElevenLabsClient(
        api_key=require_env("ELEVENLABS_API_KEY"),
        voice_id=voice_id,
        model_id=optional_env("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2"),
    )
    fal = FalClient(
        api_key=require_env("FAL_KEY"),
        model_id=optional_env("FAL_MODEL", "fal-ai/flux/dev"),
    )
    costs = CostTracker()

    async def process_scene(scene: Scene) -> SceneAssets:
        narration_path = scene_file_path(asset_paths["narration"], scene.index, "narration", "mp3")
        image_path = scene_file_path(asset_paths["images"], scene.index, "image", "jpg")
        sfx_path = scene_file_path(asset_paths["sfx"], scene.index, "sfx", "mp3") if scene.sfx_prompt.strip() else None

        try:
            async with elevenlabs_semaphore:
                if narration_path.exists() and not args.force_audio:
                    pass
                else:
                    costs.narration_characters += await elevenlabs.generate_narration(scene.narration, narration_path)
                    costs.narration_generated += 1

            async with fal_semaphore:
                if image_path.exists() and not args.force_images:
                    pass
                else:
                    await fal.generate_image(scene.visual_prompt, image_path)
                    costs.images_generated += 1

            if sfx_path:
                async with elevenlabs_semaphore:
                    if sfx_path.exists() and not args.force_sfx:
                        pass
                    else:
                        costs.sfx_characters += await elevenlabs.generate_sfx(scene.sfx_prompt, scene.duration, sfx_path)
                        costs.sfx_generated += 1
        except Exception as exc:
            raise RuntimeError(f"Scene {scene.index:03d} failed: {exc}") from exc

        return SceneAssets(scene=scene, narration_path=narration_path, image_path=image_path, sfx_path=sfx_path)

    try:
        tasks = [asyncio.create_task(process_scene(scene)) for scene in manifest.timeline]
        assets = await gather_with_progress(tasks, total=len(tasks), description="Generating assets")
        sorted_assets = sorted(assets, key=lambda item: item.scene.index)
        background_music_path = await maybe_generate_background_music(
            manifest,
            sorted_assets,
            asset_paths["music"],
            args,
            elevenlabs,
            elevenlabs_semaphore,
            costs,
        )
        thumbnail_path = await maybe_generate_thumbnail(
            project_dir=Path.cwd(),
            asset_root=asset_paths["asset_root"],
            args=args,
            fal=fal,
            fal_semaphore=fal_semaphore,
            costs=costs,
        )
        await maybe_generate_outro_narration(
            manifest=manifest,
            outro_dir=asset_paths["outro"],
            args=args,
            elevenlabs=elevenlabs,
            elevenlabs_semaphore=elevenlabs_semaphore,
            costs=costs,
        )
        return sorted_assets, costs, background_music_path, thumbnail_path
    finally:
        await elevenlabs.close()
        await fal.close()


async def maybe_generate_thumbnail(
    *,
    project_dir: Path,
    asset_root: Path,
    args: argparse.Namespace,
    fal: FalClient,
    fal_semaphore: asyncio.Semaphore,
    costs: CostTracker,
) -> Optional[Path]:
    youtube_md_path = project_dir / "youtube.md"
    if not youtube_md_path.exists():
        print("Thumbnail generation skipped: youtube.md not found.")
        return None

    thumbnail_prompt = extract_thumbnail_prompt(youtube_md_path)
    if not thumbnail_prompt:
        print("Thumbnail generation skipped: no Fal.ai prompt found in youtube.md.")
        return None

    destination = asset_root / THUMBNAIL_NAME
    if destination.exists() and not args.force_images:
        print(f"Thumbnail generation skipped: {destination} already exists.")
        return destination

    async with fal_semaphore:
        await fal.generate_image(
            thumbnail_prompt,
            destination,
            image_format="png",
            image_size="landscape_16_9",
        )
        costs.images_generated += 1
    return destination


def extract_thumbnail_prompt(youtube_md_path: Path) -> Optional[str]:
    content = youtube_md_path.read_text(encoding="utf-8")

    inline_match = re.search(r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?fal\.ai prompt(?:\*\*)?\s*:\s*(.+?)\s*$", content)
    if inline_match:
        return inline_match.group(1).strip()

    block_match = re.search(
        r"(?ims)^\s*(?:#+\s*)?(?:\*\*)?fal\.ai prompt(?:\*\*)?\s*:?\s*$\n(?P<body>.*?)(?:\n\s*\n|\n\s*(?:#+\s|[-*]\s+\S|[A-Z][^\n]{0,80}:))",
        content,
    )
    if block_match:
        prompt = block_match.group("body").strip()
        if prompt:
            return " ".join(line.strip() for line in prompt.splitlines() if line.strip())

    return None


async def maybe_generate_background_music(
    manifest: Manifest,
    scene_assets: list[SceneAssets],
    music_dir: Path,
    args: argparse.Namespace,
    elevenlabs: ElevenLabsClient,
    elevenlabs_semaphore: asyncio.Semaphore,
    costs: CostTracker,
) -> Optional[Path]:
    prompt = str(manifest.metadata.get("bg_music_prompt", "")).strip()
    if not prompt:
        return None

    destination = music_dir / "background_music.mp3"
    if destination.exists() and not args.force_music:
        return destination

    total_duration = estimate_total_runtime_seconds(scene_assets)
    async with elevenlabs_semaphore:
        await elevenlabs.generate_music(prompt, total_duration, destination)
        costs.music_generated += 1
    return destination


def estimate_total_runtime_seconds(scene_assets: list[SceneAssets]) -> float:
    total = 0.0
    for scene_asset in scene_assets:
        if scene_asset.narration_path.exists():
            narration = AudioFileClip(str(scene_asset.narration_path))
            try:
                total += narration.duration + max(0.0, scene_asset.scene.pause_after)
            finally:
                narration.close()
        else:
            total += scene_asset.scene.duration + max(0.0, scene_asset.scene.pause_after)
    return total


async def maybe_generate_outro_narration(
    manifest: Manifest,
    outro_dir: Path,
    args: argparse.Namespace,
    elevenlabs: ElevenLabsClient,
    elevenlabs_semaphore: asyncio.Semaphore,
    costs: CostTracker,
) -> Optional[Path]:
    if optional_env("ENABLE_OUTRO", "true").lower() != "true":
        return None

    outro_text = optional_env(
        "OUTRO_NARRATION_TEXT",
        "If you enjoyed this video, please like, subscribe, and let me know what Cold War story we should cover next.",
    ).strip()
    if not outro_text:
        return None

    destination = outro_dir / OUTRO_NARRATION_NAME
    if destination.exists() and not args.force_audio:
        return destination

    async with elevenlabs_semaphore:
        costs.narration_characters += await elevenlabs.generate_narration(outro_text, destination)
        costs.narration_generated += 1
        costs.outro_generated += 1
    return destination


def loop_audio_clip(source: AudioFileClip, duration: float, start_offset: float = 0.0) -> AudioClip:
    parts = []
    remaining = duration
    cursor = start_offset % source.duration if source.duration > 0 else 0.0
    while remaining > 0:
        available = source.duration - cursor if source.duration > 0 else remaining
        part_duration = min(available, remaining)
        parts.append(source.subclipped(cursor, cursor + part_duration))
        remaining -= part_duration
        cursor = 0.0
    if len(parts) == 1:
        return parts[0]
    return concatenate_audioclips(parts)


def apply_volume_envelope(
    clip: AudioClip,
    envelope: Callable[[Union[float, np.ndarray]], Union[float, np.ndarray]],
) -> AudioClip:
    fps = getattr(clip, "fps", 44100)

    def make_frame(t: Union[float, np.ndarray]) -> np.ndarray:
        frame = clip.get_frame(t)
        gain = np.asarray(envelope(t))
        if gain.ndim == 0:
            return frame * float(gain)
        if getattr(frame, "ndim", 1) > 1 and gain.ndim == 1:
            gain = gain[:, np.newaxis]
        return frame * gain

    return AudioClip(make_frame, duration=clip.duration, fps=fps)


def background_ducking_envelope(duration: float) -> Callable[[Union[float, np.ndarray]], Union[float, np.ndarray]]:
    if duration <= 0:
        return lambda _t: 0.1

    fade = min(FADE_DURATION, duration / 2.0)

    def envelope(t: Union[float, np.ndarray]) -> Union[float, np.ndarray]:
        t_array = np.asarray(t)
        if duration <= fade * 2:
            result = np.full_like(t_array, 0.1, dtype=float)
        else:
            fade_in = 1.0 - 0.9 * (t_array / fade)
            fade_out = 0.1 + 0.9 * ((t_array - (duration - fade)) / fade)
            result = np.where(
                t_array < fade,
                fade_in,
                np.where(t_array > duration - fade, fade_out, 0.1),
            )

        if np.isscalar(t):
            return float(result)
        return result

    return envelope


def sfx_ducking_envelope(narration_duration: float, duck_level: float = 0.65) -> Callable[[Union[float, np.ndarray]], Union[float, np.ndarray]]:
    if narration_duration <= 0:
        return lambda _t: 1.0

    def envelope(t: Union[float, np.ndarray]) -> Union[float, np.ndarray]:
        t_array = np.asarray(t)
        result = np.where(t_array < narration_duration, duck_level, 1.0)
        if np.isscalar(t):
            return float(result)
        return result

    return envelope


def build_ken_burns_clip(image_path: Path, duration: float) -> VideoClip:
    video_width, video_height = VIDEO_SIZE
    zoom_delta = 0.1
    max_zoom = 1.0 + zoom_delta
    supersample = max(1.0, float(optional_env("KEN_BURNS_SUPERSAMPLE", "2.0")))

    with Image.open(image_path) as image:
        source = image.convert("RGB")
        width, height = source.size
        base_scale = max(video_width / width, video_height / height)
        max_scale = base_scale * max_zoom
        working_width = int(round(video_width * supersample))
        working_height = int(round(video_height * supersample))
        scaled_width = max(working_width, int(round(width * max_scale * supersample)))
        scaled_height = max(working_height, int(round(height * max_scale * supersample)))
        scaled_source = source.resize((scaled_width, scaled_height), Image.Resampling.LANCZOS)

    def make_frame(t: float) -> np.ndarray:
        progress = 0.0 if duration <= 0 else min(max(t / duration, 0.0), 1.0)
        zoom = 1.0 + (zoom_delta * progress)
        crop_width = min(working_width * (max_zoom / zoom), float(scaled_width))
        crop_height = min(working_height * (max_zoom / zoom), float(scaled_height))

        left = max(0.0, (scaled_width - crop_width) / 2.0)
        top = max(0.0, (scaled_height - crop_height) / 2.0)
        right = left + crop_width
        bottom = top + crop_height

        frame = scaled_source.crop((left, top, right, bottom))
        if frame.size != VIDEO_SIZE:
            frame = frame.resize(VIDEO_SIZE, Image.Resampling.LANCZOS)
        return np.array(frame)

    return VideoClip(make_frame, duration=duration)


def build_scene_clip(
    scene_assets: SceneAssets,
    background_music_path: Optional[Path],
    timeline_start: float = 0.0,
) -> CompositeVideoClip:
    narration: Optional[AudioFileClip] = None
    narration_duration = 0.0
    if scene_assets.narration_path.exists():
        narration = AudioFileClip(str(scene_assets.narration_path))
        narration_duration = narration.duration
        scene_duration = narration_duration + max(0.0, scene_assets.scene.pause_after)
    else:
        scene_duration = scene_assets.scene.duration + max(0.0, scene_assets.scene.pause_after)

    silence = AudioClip(lambda t: np.zeros((1,)), duration=scene_duration, fps=44100)
    layers: list[AudioClip] = [silence]
    if narration is not None:
        layers.append(narration)

    sfx_clip: Optional[AudioFileClip] = None
    if scene_assets.sfx_path and scene_assets.sfx_path.exists():
        sfx_clip = AudioFileClip(str(scene_assets.sfx_path))
        if sfx_clip.duration > scene_duration:
            sfx_clip = sfx_clip.subclipped(0, scene_duration)
        sfx_volume = float(optional_env("SFX_VOLUME", "0.22"))
        sfx_segment = sfx_clip.with_volume_scaled(sfx_volume)
        if narration_duration > 0:
            sfx_segment = apply_volume_envelope(
                sfx_segment,
                sfx_ducking_envelope(
                    narration_duration,
                    duck_level=float(optional_env("SFX_DUCKING_LEVEL", "0.65")),
                ),
            )
        layers.append(sfx_segment.with_start(0))

    music_source: Optional[AudioFileClip] = None
    if background_music_path and background_music_path.exists():
        music_source = AudioFileClip(str(background_music_path))
        music_segment = loop_audio_clip(music_source, scene_duration, start_offset=timeline_start)
        base_music_intensity = scene_assets.scene.music_intensity if scene_assets.scene.music_intensity is not None else float(
            optional_env("DEFAULT_MUSIC_INTENSITY", "0.25")
        )
        music_segment = music_segment.with_volume_scaled(base_music_intensity)
        music_segment = apply_volume_envelope(music_segment, background_ducking_envelope(scene_duration))
        layers.append(music_segment.with_start(0))

    audio = CompositeAudioClip(layers)
    video = build_ken_burns_clip(scene_assets.image_path, scene_duration).with_audio(audio)

    close_targets: list[Any] = [silence]
    if narration is not None:
        close_targets.append(narration)
    if sfx_clip is not None:
        close_targets.append(sfx_clip)
    if music_source is not None:
        close_targets.append(music_source)

    setattr(video, "_close_targets", close_targets)
    return video


def resolve_outro_narration_path(project_dir: Path) -> Optional[Path]:
    candidate = project_dir / "assets" / "outro" / OUTRO_NARRATION_NAME
    return candidate if candidate.exists() else None


def build_extended_final_scene_clip(
    scene_assets: SceneAssets,
    background_music_path: Optional[Path],
    project_dir: Path,
    timeline_start: float = 0.0,
) -> Optional[CompositeVideoClip]:
    if optional_env("ENABLE_OUTRO", "true").lower() != "true":
        return None

    narration_path = resolve_outro_narration_path(project_dir)
    if not narration_path:
        return None

    base_clip = build_scene_clip(scene_assets, background_music_path, timeline_start=timeline_start)
    outro_narration = AudioFileClip(str(narration_path))
    gap_seconds = float(optional_env("OUTRO_GAP_SECONDS", "1.0"))
    gap_seconds = max(0.0, gap_seconds)
    extended_duration = base_clip.duration + gap_seconds + outro_narration.duration

    silence = AudioClip(lambda t: np.zeros((1,)), duration=extended_duration, fps=44100)
    audio_layers: list[AudioClip] = [silence]
    if base_clip.audio is not None:
        audio_layers.append(base_clip.audio)
    audio_layers.append(outro_narration.with_start(base_clip.duration + gap_seconds))

    audio = CompositeAudioClip(audio_layers)
    video = build_ken_burns_clip(scene_assets.image_path, extended_duration).with_audio(audio)
    close_targets = list(getattr(base_clip, "_close_targets", []))
    close_targets.extend([outro_narration, silence])
    setattr(video, "_close_targets", close_targets)
    base_clip.close()
    return video


def assemble_video(
    scenes: list[SceneAssets],
    project_dir: Path,
    metadata: dict[str, Any],
    threads: int,
    sample_window: Optional[SampleWindow] = None,
    background_music_path: Optional[Path] = None,
) -> Path:
    music_path = background_music_path or pick_background_music(project_dir, metadata)
    fps = int(metadata.get("fps", DEFAULT_FPS))
    output = sampled_output_path(output_path(project_dir, metadata), sample_window)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        print(f"Render skipped: {output} already exists.")
        return output
    temp_dir = output.parent / f"{output.stem}_scenes"
    temp_dir.mkdir(parents=True, exist_ok=True)

    rendered_scene_paths: list[Path] = []
    progress = tqdm(scenes, desc="Assembling video", unit="scene")
    timeline_cursor = 0.0
    try:
        for scene_assets in progress:
            progress.set_postfix(scene=scene_assets.scene.index)
            scene_output = temp_dir / f"scene_{scene_assets.scene.index:03d}.mp4"
            is_last_scene = scene_assets == scenes[-1]
            if is_last_scene:
                clip = build_extended_final_scene_clip(scene_assets, music_path, project_dir, timeline_start=timeline_cursor)
                if clip is None:
                    clip = build_scene_clip(scene_assets, music_path, timeline_start=timeline_cursor)
            else:
                clip = build_scene_clip(scene_assets, music_path, timeline_start=timeline_cursor)
            render_clip = clip
            try:
                clip_start = timeline_cursor
                clip_end = clip_start + clip.duration
                timeline_cursor = clip_end

                if sample_window is not None:
                    overlap_start = max(clip_start, sample_window.offset)
                    overlap_end = min(clip_end, sample_window.end)
                    if overlap_end <= overlap_start:
                        continue
                    clip_duration = float(clip.duration)
                    local_start = max(0.0, min(overlap_start - clip_start, clip_duration))
                    local_end = max(local_start, min(overlap_end - clip_start, clip_duration))
                    if local_end <= local_start:
                        continue
                    render_clip = clip.subclipped(local_start, local_end)

                render_clip.write_videofile(
                    str(scene_output),
                    fps=fps,
                    codec="libx264",
                    audio_codec="aac",
                    threads=threads,
                    preset="medium",
                    logger=None,
                )
                rendered_scene_paths.append(scene_output)
            finally:
                if render_clip is not clip:
                    render_clip.close()
                for item in getattr(clip, "_close_targets", []):
                    item.close()
                clip.close()

        concat_scene_files(rendered_scene_paths, output)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return output


def concat_scene_files(scene_paths: list[Path], output: Path) -> None:
    if not scene_paths:
        raise RuntimeError("No rendered scene files were produced.")

    ffmpeg_binary = shutil.which("ffmpeg")
    if ffmpeg_binary:
        concat_with_ffmpeg(ffmpeg_binary, scene_paths, output)
        return

    clips = [VideoFileClip(str(path)) for path in scene_paths]
    try:
        final_clip = concatenate_videoclips(clips, method="compose")
        final_clip.write_videofile(
            str(output),
            codec="libx264",
            audio_codec="aac",
            preset="medium",
        )
        final_clip.close()
    finally:
        for clip in clips:
            clip.close()


def concat_with_ffmpeg(ffmpeg_binary: str, scene_paths: list[Path], output: Path) -> None:
    concat_list = output.parent / f"{output.stem}_concat.txt"
    concat_body = "\n".join(f"file '{path.resolve().as_posix()}'" for path in scene_paths)
    concat_list.write_text(concat_body + "\n", encoding="utf-8")
    try:
        command = [
            ffmpeg_binary,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-c",
            "copy",
            str(output),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "ffmpeg concat failed")
    finally:
        concat_list.unlink(missing_ok=True)


def print_cost_summary(costs: CostTracker, started_at: float) -> None:
    elapsed = time.time() - started_at
    print("\nCost summary")
    print(f"  Narration characters: {costs.narration_characters}")
    print(f"  SFX characters: {costs.sfx_characters}")
    print(f"  Total characters: {costs.total_characters}")
    print(f"  Images generated: {costs.images_generated}")
    print(f"  Narration files generated: {costs.narration_generated}")
    print(f"  SFX files generated: {costs.sfx_generated}")
    print(f"  Music files generated: {costs.music_generated}")
    print(f"  Outro narration files generated: {costs.outro_generated}")
    print(f"  Elapsed time: {elapsed:.1f}s")


def validate_inputs(manifest: Manifest) -> None:
    for scene in manifest.timeline:
        if not scene.visual_prompt.strip():
            raise ValueError(f"Scene {scene.index} is missing visual_prompt.")


def main() -> int:
    started_at = time.time()
    args = parse_args()
    project_dir = Path.cwd()
    script_dir = Path(__file__).resolve().parent
    load_runtime_env(project_dir, script_dir)
    print_runtime_env_summary()
    asset_paths = ensure_directories(project_dir)

    try:
        manifest = load_manifest(project_dir / DEFAULT_MANIFEST, limit=args.limit)
        validate_inputs(manifest)
    except (FileNotFoundError, ValidationError, ValueError) as exc:
        print(f"Manifest error: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"Failed to parse manifest.json: {exc}", file=sys.stderr)
        return 1

    try:
        sample_window = parse_sample_window(args.sample)
        scene_assets, costs, background_music_path, thumbnail_path = asyncio.run(acquire_assets(manifest, asset_paths, args))
        output = assemble_video(
            scene_assets,
            project_dir,
            manifest.metadata,
            threads=args.threads,
            sample_window=sample_window,
            background_music_path=background_music_path,
        )
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Argument error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Assembly failed: {exc}", file=sys.stderr)
        return 1

    print(f"\nOutput written to: {output}")
    if thumbnail_path is not None:
        print(f"Thumbnail written to: {thumbnail_path}")
    print_cost_summary(costs, started_at)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
