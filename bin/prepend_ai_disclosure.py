#!/usr/bin/env python3
"""Prepend a disclosure intro video to an existing assembled export."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Optional

from moviepy import VideoFileClip, concatenate_videoclips


DEFAULT_MANIFEST = "manifest.json"
DEFAULT_OUTPUT_NAME = "assembled.mp4"
INTRO_VIDEO_NAME = "ai_disclosure_intro.mp4"
AI_DISCLOSURE_INTRO_VIDEO_ENV = "AI_DISCLOSURE_INTRO_VIDEO"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=str, default=None, help="Existing assembled video path.")
    parser.add_argument("--output", type=str, default=None, help="Output path for the disclosure-prefixed video.")
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


def manifest_output_path(project_dir: Path) -> Path:
    manifest_path = project_dir / DEFAULT_MANIFEST
    if not manifest_path.exists():
        return project_dir / "output" / DEFAULT_OUTPUT_NAME

    raw = manifest_path.read_text(encoding="utf-8")
    try:
        import json

        data = json.loads(raw)
    except Exception:
        return project_dir / "output" / DEFAULT_OUTPUT_NAME

    metadata = data.get("metadata", {}) if isinstance(data, dict) else {}
    filename = metadata.get("output_filename") if isinstance(metadata, dict) else None
    return project_dir / "output" / str(filename or DEFAULT_OUTPUT_NAME)


def resolve_intro_video(project_dir: Path) -> Optional[Path]:
    raw_value = os.getenv(AI_DISCLOSURE_INTRO_VIDEO_ENV, "").strip()
    if raw_value:
        candidate = Path(raw_value).expanduser()
        if not candidate.is_absolute():
            candidate = (project_dir / candidate).resolve()
        if candidate.exists():
            return candidate

    repo_root = Path(__file__).resolve().parent.parent
    candidates = [
        repo_root / "assets" / "intro" / INTRO_VIDEO_NAME,
        project_dir / "assets" / "intro" / INTRO_VIDEO_NAME,
        project_dir / "intro" / INTRO_VIDEO_NAME,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def prepend_video(intro_path: Path, input_path: Path, output_path: Path) -> None:
    intro_clip = VideoFileClip(str(intro_path))
    main_clip = VideoFileClip(str(input_path))
    try:
        final_clip = concatenate_videoclips([intro_clip, main_clip], method="compose")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        final_clip.write_videofile(
            str(output_path),
            codec="libx264",
            audio_codec="aac",
            preset="medium",
        )
        final_clip.close()
    finally:
        intro_clip.close()
        main_clip.close()


def main() -> int:
    args = parse_args()
    project_dir = Path.cwd()
    script_dir = Path(__file__).resolve().parent
    load_env(project_dir, script_dir)

    input_path = Path(args.input).expanduser() if args.input else manifest_output_path(project_dir)
    if not input_path.exists():
        raise FileNotFoundError(f"Existing assembled video not found: {input_path}")

    intro_path = resolve_intro_video(project_dir)
    if not intro_path:
        raise FileNotFoundError(
            "AI disclosure intro video not found. Set AI_DISCLOSURE_INTRO_VIDEO or place "
            "assets/intro/ai_disclosure_intro.mp4 in the project or repo root."
        )

    output_path = (
        Path(args.output).expanduser()
        if args.output
        else input_path.with_name(f"{input_path.stem}_with_disclosure{input_path.suffix}")
    )
    prepend_video(intro_path, input_path, output_path)
    print(f"Wrote disclosure-prefixed video: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
