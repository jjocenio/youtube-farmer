#!/usr/bin/env python3
"""Upload a rendered video and thumbnail to YouTube from a project directory."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx


DEFAULT_OUTPUT_NAME = "assembled.mp4"
DEFAULT_THUMBNAIL_NAME = "thumbnail.png"
UPLOAD_RECORD_NAME = "youtube_upload.json"
RETRIABLE_STATUS_CODES = {500, 502, 503, 504}
MAX_RETRIES = 10
YOUTUBE_TAG_BUDGET = 500
YOUTUBE_TAG_MAX_LENGTH = 30
YOUTUBE_DESCRIPTION_MAX_LENGTH = 4900


@dataclass
class PublishMetadata:
    title: str
    description: str
    tags: list[str]
    selected_title_source: str
    thumbnail_candidates: list[dict[str, Any]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=str, default=None, help="Override video file path.")
    parser.add_argument("--thumbnail", type=str, default=None, help="Override thumbnail file path.")
    parser.add_argument(
        "--privacy-status",
        type=str,
        default=None,
        choices=["private", "unlisted", "public"],
        help="Override privacy status.",
    )
    parser.add_argument("--category-id", type=str, default=None, help="Override YouTube category ID.")
    parser.add_argument("--video-id", type=str, default=None, help="Existing YouTube video id to update.")
    parser.add_argument(
        "--sync-existing",
        action="store_true",
        help="Update an already uploaded YouTube video's metadata/thumbnail without re-uploading the video file.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print metadata and files without uploading.")
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
        values[key.strip()] = value.strip().strip("'").strip('"')
        os.environ.setdefault(key.strip(), values[key.strip()])
    return values


def load_runtime_env(project_dir: Path, script_dir: Path) -> None:
    for dotenv_path in (project_dir / ".env", script_dir / ".env"):
        load_dotenv(dotenv_path)


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def optional_env(name: str, default: str) -> str:
    return os.getenv(name, default)


def manifest_output_path(project_dir: Path) -> Path:
    manifest_path = project_dir / "manifest.json"
    if not manifest_path.exists():
        return project_dir / "output" / DEFAULT_OUTPUT_NAME

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if isinstance(raw.get("metadata"), dict):
        filename = raw["metadata"].get("output_filename") or DEFAULT_OUTPUT_NAME
    else:
        filename = raw.get("output_filename") or DEFAULT_OUTPUT_NAME
    return project_dir / "output" / str(filename)


def load_manifest(project_dir: Path) -> dict[str, Any]:
    manifest_path = project_dir / "manifest.json"
    if not manifest_path.exists():
        return {}
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def parse_youtube_md(youtube_md_path: Path) -> PublishMetadata:
    if not youtube_md_path.exists():
        raise FileNotFoundError(f"youtube.md not found: {youtube_md_path}")

    content = youtube_md_path.read_text(encoding="utf-8")
    title, title_source = select_title(content)
    project_dir = youtube_md_path.parent
    description = build_description(content, project_dir)
    tags = sanitize_youtube_tags(extract_tags(content))
    thumbnail_candidates = extract_thumbnail_candidates(content)

    if not title:
        raise ValueError("Could not determine a YouTube title from youtube.md.")
    if not description.strip():
        raise ValueError("Could not build a description from youtube.md.")

    return PublishMetadata(
        title=title,
        description=description,
        tags=tags,
        selected_title_source=title_source,
        thumbnail_candidates=thumbnail_candidates,
    )


def select_title(content: str) -> tuple[str, str]:
    direct_patterns = [
        (r"(?ims)^\s*##\s*Selected Title\s*\n+(?P<body>.+?)(?:\n\s*\n|\n##\s)", "Selected Title"),
        (r"(?ims)^\s*##\s*Final Title\s*\n+(?P<body>.+?)(?:\n\s*\n|\n##\s)", "Final Title"),
        (r"(?ims)^\s*##\s*Title\s*\n+(?P<body>.+?)(?:\n\s*\n|\n##\s)", "Title"),
    ]
    for pattern, source in direct_patterns:
        match = re.search(pattern, content)
        if match:
            body = " ".join(line.strip() for line in match.group("body").splitlines() if line.strip())
            if body:
                return body, source

    proposed_titles = extract_proposed_titles(content)
    for preferred_label in ("Narrative / Dramatic", "SEO-Focused", "Clickbait / High-Curiosity"):
        if preferred_label in proposed_titles:
            return proposed_titles[preferred_label], f"Proposed Titles: {preferred_label}"

    heading_match = re.search(r"(?m)^#\s+YouTube Metadata:\s*(.+?)\s*$", content)
    if heading_match:
        return heading_match.group(1).strip(), "Document Heading"

    if proposed_titles:
        label, title = next(iter(proposed_titles.items()))
        return title, f"Proposed Titles: {label}"

    return "", ""


def extract_proposed_titles(content: str) -> dict[str, str]:
    matches = re.findall(
        r"(?ims)^\s*###\s*(?P<label>[^\n]+)\n+(?P<title>.+?)(?=(?:\n\s*###\s|\n\s*---|\n\s*##\s|\Z))",
        content,
    )
    titles: dict[str, str] = {}
    for label, raw_title in matches:
        title = " ".join(line.strip() for line in raw_title.splitlines() if line.strip())
        if title:
            normalized_label = re.sub(r"^\d+\.\s*", "", label.strip())
            titles[normalized_label] = title
    return titles


def build_description(content: str, project_dir: Path) -> str:
    hook = extract_section_body(content, "The Hook")
    chapters = extract_code_block_after_heading(content, "Chapter Timestamps")
    seo = extract_section_body(content, "SEO Paragraph")
    sources_block = build_sources_block(project_dir, max_chars=1600)

    pieces: list[str] = []
    if hook:
        pieces.append(hook)
    if chapters:
        pieces.append("Chapters:\n" + chapters)
    if sources_block:
        pieces.append(sources_block)
    if seo:
        pieces.append(seo)

    description = "\n\n".join(piece.strip() for piece in pieces if piece.strip())
    if len(description) <= YOUTUBE_DESCRIPTION_MAX_LENGTH:
        return description

    trimmed = []
    running = 0
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        separator = 2 if trimmed else 0
        budget = YOUTUBE_DESCRIPTION_MAX_LENGTH - running - separator
        if budget <= 0:
            break
        if len(piece) > budget:
            piece = piece[: max(0, budget - 1)].rstrip()
        trimmed.append(piece)
        running += len(piece) + separator
    return "\n\n".join(trimmed).strip()


def build_sources_block(project_dir: Path, max_chars: int = 1600) -> str:
    sources = load_structured_sources(project_dir)
    if not sources:
        sources = parse_sources_from_info_md(project_dir / "info.md")
    if not sources:
        return ""

    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key in ("book", "document", "article", "archive", "documentary", "other")}
    for source in sources:
        if not isinstance(source, dict):
            continue
        source_type = str(source.get("type", "other")).strip().lower()
        if source_type not in grouped:
            source_type = "other"
        grouped[source_type].append(source)

    order = [
        ("book", "Books", "📖"),
        ("document", "Declassified Documents", "📄"),
        ("article", "Articles", "📰"),
        ("archive", "Archives", "🗂"),
        ("documentary", "Documentaries", "🎞"),
        ("other", "Other", "•"),
    ]
    lines = ["📚 SOURCES & FURTHER READING"]
    remaining = max_chars - len(lines[0])
    for key, label, icon in order:
        items = grouped.get(key, [])
        if not items:
            continue
        section_header = f"{icon} {label}"
        candidate_lines = ["", section_header]
        for source in items:
            title = str(source.get("title", "")).strip()
            author = str(source.get("author", source.get("institution", ""))).strip()
            year = str(source.get("year", "")).strip()
            meta_bits = ", ".join(bit for bit in (author, year) if bit)
            candidate_lines.append(f"{title} — {meta_bits}" if meta_bits else title)
            url = source.get("url")
            if url:
                candidate_lines.append(str(url).strip())

        candidate_text = "\n".join(candidate_lines).strip()
        if len(candidate_text) <= remaining:
            lines.append("")
            lines.append(section_header)
            for entry in candidate_lines[2:]:
                lines.append(entry)
            remaining -= len(candidate_text)
            continue

        for entry in candidate_lines[2:]:
            entry_cost = len(entry) + 1
            if entry_cost > remaining:
                break
            if not lines or lines[-1] != section_header:
                if len("\n".join(lines + ["", section_header])) <= max_chars:
                    lines.append("")
                    lines.append(section_header)
                    remaining -= len("\n".join(["", section_header]))
                else:
                    break
            lines.append(entry)
            remaining -= entry_cost
        if remaining <= 0:
            break
    return "\n".join(lines).strip()[:max_chars]


def load_structured_sources(project_dir: Path) -> list[dict[str, Any]]:
    manifest_path = project_dir / "manifest.json"
    if not manifest_path.exists():
        return []
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    metadata = raw.get("metadata", {}) if isinstance(raw.get("metadata"), dict) else {}
    sources = metadata.get("sources", [])
    return sources if isinstance(sources, list) else []


def parse_sources_from_info_md(info_md_path: Path) -> list[dict[str, Any]]:
    if not info_md_path.exists():
        return []

    content = info_md_path.read_text(encoding="utf-8")
    sources: list[dict[str, Any]] = []

    for raw_line in re.findall(r"(?m)^\s*\*\*Sources:\*\*\s*(.+?)\s*$", content):
        sources.extend(parse_inline_sources(raw_line))

    bibliography_match = re.search(r"(?ims)^\s*##\s*Bibliography & Link Map\s*(?P<body>.+?)(?=^\s*##\s|\Z)", content)
    if bibliography_match:
        sources.extend(parse_bibliography_entries(bibliography_match.group("body")))

    if not sources:
        sources.extend(parse_generic_reference_lines(content))

    sources = dedupe_sources(sources)
    return sources


def parse_inline_sources(raw_line: str) -> list[dict[str, Any]]:
    parts = [part.strip() for part in raw_line.split(";") if part.strip()]
    sources: list[dict[str, Any]] = []
    for part in parts:
        match = re.search(r"\[(?P<title>[^\]]+)\]\((?P<url>https?://[^)]+)\)(?:\s*—\s*(?P<meta>.+))?$", part)
        if not match:
            continue
        title = match.group("title").strip()
        url = match.group("url").strip()
        meta = (match.group("meta") or "").strip()
        sources.append(source_from_title_meta(title, meta, url))
    return sources


def parse_bibliography_entries(body: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for match in re.finditer(r"(?m)^\s*\d+\.\s+\[(?P<title>[^\]]+)\]\((?P<url>https?://[^)]+)\)\s*—\s*(?P<meta>.+?)\s*$", body):
        entries.append(source_from_title_meta(match.group("title"), match.group("meta"), match.group("url")))
    return entries


def parse_generic_reference_lines(content: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for match in re.finditer(r"(?m)^\s*(?:\d+\.\s+)?(?P<title>[^\n]+?)\s*—\s*(?P<meta>.+?)\s*$", content):
        title = match.group("title").strip()
        meta = match.group("meta").strip()
        url_match = re.search(r"https?://\S+", meta)
        url = url_match.group(0).rstrip(").,]") if url_match else None
        if not url:
            continue
        entries.append(source_from_title_meta(title, meta, url))
    return entries


def source_from_title_meta(title: str, meta: str, url: str) -> dict[str, Any]:
    year_match = re.search(r"\b(19|20)\d{2}\b", meta)
    year = year_match.group(0) if year_match else ""
    author = re.sub(r"https?://\S+", "", meta).strip(" ,—-")
    if year:
        author = author.replace(year, "").strip(" ,—-")
    return {
        "title": title.strip(),
        "author": author,
        "year": year,
        "url": url,
        "type": infer_source_type(title, meta, url),
    }


def dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for source in sources:
        key = (str(source.get("title", "")).casefold(), str(source.get("url", "")).casefold())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(source)
    return deduped
    return sources


def infer_source_type(title: str, meta: str, url: Optional[str]) -> str:
    haystack = f"{title} {meta} {url or ''}".casefold()
    if any(word in haystack for word in ("book", "published by", "press")):
        return "book"
    if any(word in haystack for word in ("documentary", "film", "tv", "television")):
        return "documentary"
    if any(word in haystack for word in ("archive", "national archives", "declassified", "pdf")):
        return "document"
    if "substack" in haystack or "blog" in haystack or "magazine" in haystack or "news" in haystack:
        return "article"
    return "other"


def extract_thumbnail_candidates(content: str) -> list[dict[str, Any]]:
    match = re.search(r"(?ims)^\s*###\s*Thumbnail Strategy\s*\n+(?P<body>.+?)(?=(?:\n\s*###\s|\n\s*---|\Z))", content)
    if not match:
        return []
    body = match.group("body")
    candidates = []
    for url in re.findall(r"https?://\S+", body):
        candidates.append({"url": url.rstrip(").,]")})
    return candidates


def select_thumbnail_source(project_dir: Path, youtube_md_path: Path, args: argparse.Namespace) -> Optional[dict[str, Any]]:
    if args.thumbnail:
        return {"path": str(Path(args.thumbnail).expanduser())}

    manifest = load_manifest(project_dir)
    metadata = manifest.get("metadata", {}) if isinstance(manifest.get("metadata"), dict) else {}
    public_candidates = metadata.get("public_images", []) if isinstance(metadata.get("public_images"), list) else []
    public_candidates = [candidate for candidate in public_candidates if isinstance(candidate, dict) and candidate.get("url")]
    if public_candidates:
        return choose_thumbnail_candidate(public_candidates)

    youtube_candidates = extract_thumbnail_candidates(youtube_md_path.read_text(encoding="utf-8")) if youtube_md_path.exists() else []
    if youtube_candidates:
        return choose_thumbnail_candidate(youtube_candidates)

    return None


def choose_thumbnail_candidate(candidates: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not candidates:
        return None

    print("Thumbnail candidates found:")
    for idx, candidate in enumerate(candidates, start=1):
        url = str(candidate.get("url", "")).strip()
        attribution = str(candidate.get("attribution", "")).strip()
        label = candidate.get("title") or candidate.get("name") or candidate.get("source") or f"Candidate {idx}"
        print(f"  {idx}. {label}")
        print(f"     {url}")
        if attribution:
            print(f"     attribution: {attribution}")

    default_index = 0
    if len(candidates) > 1:
        prompt = f"Choose thumbnail [1-{len(candidates)}] (default 1): "
        try:
            raw_choice = input(prompt).strip()
        except EOFError:
            raw_choice = ""
        if raw_choice:
            try:
                chosen = int(raw_choice)
                if 1 <= chosen <= len(candidates):
                    default_index = chosen - 1
            except ValueError:
                print("Invalid choice, using candidate 1.")

    return candidates[default_index]


async def download_thumbnail_source(source: dict[str, Any], destination: Path) -> Optional[Path]:
    if "path" in source:
        return Path(source["path"]).expanduser()
    url = source.get("url")
    if not url:
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": optional_env("IMAGE_DOWNLOAD_USER_AGENT", "youtube-farmer/1.0 (+https://github.com/)")},
    ) as client:
        response = await client.get(str(url))
        response.raise_for_status()
        destination.write_bytes(response.content)
    return destination


def extract_section_body(content: str, heading: str) -> str:
    pattern = rf"(?ims)^\s*###\s*{re.escape(heading)}\s*\n+(?P<body>.+?)(?=(?:\n\s*###\s|\n\s*---|\n\s*##\s|\Z))"
    match = re.search(pattern, content)
    if not match:
        return ""
    body = match.group("body").strip()
    body = re.sub(r"(?ims)^```.*?^```$", "", body).strip()
    return " ".join(line.strip() for line in body.splitlines() if line.strip())


def extract_code_block_after_heading(content: str, heading: str) -> str:
    pattern = rf"(?ims)^\s*###\s*{re.escape(heading)}\s*\n+```(?:\w+)?\n(?P<body>.+?)\n```"
    match = re.search(pattern, content)
    return match.group("body").strip() if match else ""


def extract_tags(content: str) -> list[str]:
    match = re.search(r"(?ims)^\s*##\s*Tags\s*\n+(?P<body>.+?)(?=(?:\n\s*---|\n\s*##\s|\Z))", content)
    if not match:
        return []
    raw = " ".join(line.strip() for line in match.group("body").splitlines() if line.strip())
    tags = [tag.strip() for tag in raw.split(",")]
    return [tag for tag in tags if tag]


def sanitize_youtube_tags(tags: list[str]) -> list[str]:
    sanitized: list[str] = []
    seen: set[str] = set()

    for tag in tags:
        cleaned = " ".join(tag.split()).replace(",", " ").strip()
        if not cleaned:
            continue
        if len(cleaned) > YOUTUBE_TAG_MAX_LENGTH:
            cleaned = cleaned[:YOUTUBE_TAG_MAX_LENGTH].rstrip()
        dedupe_key = cleaned.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        sanitized.append(cleaned)

    budgeted: list[str] = []
    current_budget = 0
    for tag in sanitized:
        tag_cost = youtube_tag_cost(tag)
        separator_cost = 1 if budgeted else 0
        if current_budget + separator_cost + tag_cost > YOUTUBE_TAG_BUDGET:
            break
        if separator_cost:
            current_budget += separator_cost
        current_budget += tag_cost
        budgeted.append(tag)

    return budgeted


def youtube_tag_cost(tag: str) -> int:
    if " " in tag:
        return len(tag) + 2
    return len(tag)


def print_runtime_summary(video_path: Path, thumbnail_path: Optional[Path], metadata: PublishMetadata) -> None:
    print("Publish summary")
    print(f"  Video: {video_path}")
    print(f"  Thumbnail: {thumbnail_path if thumbnail_path else '<none>'}")
    print(f"  Title source: {metadata.selected_title_source}")
    print(f"  Title: {metadata.title}")
    print(f"  Tags: {len(metadata.tags)}")


def print_sync_summary(video_id: str, thumbnail_path: Optional[Path], metadata: PublishMetadata) -> None:
    print("Sync summary")
    print(f"  Video id: {video_id}")
    print(f"  Thumbnail: {thumbnail_path if thumbnail_path else '<none>'}")
    print(f"  Title source: {metadata.selected_title_source}")
    print(f"  Title: {metadata.title}")
    print(f"  Tags: {len(metadata.tags)}")


def get_authenticated_service(project_dir: Path, script_dir: Path):
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            "Missing Google API dependencies. Install: google-api-python-client google-auth-oauthlib google-auth-httplib2"
        ) from exc

    scopes = [
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.force-ssl",
    ]

    client_secrets_path = resolve_config_path(
        optional_env("YOUTUBE_CLIENT_SECRETS_FILE", str(script_dir / "client_secrets.json")),
        project_dir,
        script_dir,
    )
    token_path = resolve_config_path(
        optional_env("YOUTUBE_TOKEN_FILE", str(script_dir / "youtube_token.json")),
        project_dir,
        script_dir,
    )

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_path), scopes)
        creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return build("youtube", "v3", credentials=creds)


def resolve_config_path(raw_value: str, project_dir: Path, script_dir: Path) -> Path:
    candidate = Path(raw_value).expanduser()
    if candidate.is_absolute():
        return candidate

    project_candidate = project_dir / candidate
    if project_candidate.exists():
        return project_candidate
    return script_dir / candidate


def upload_video(project_dir: Path, script_dir: Path, video_path: Path, thumbnail_path: Optional[Path], metadata: PublishMetadata, args: argparse.Namespace) -> dict:
    from googleapiclient.http import MediaFileUpload

    youtube = get_authenticated_service(project_dir, script_dir)

    privacy_status = args.privacy_status or optional_env("YOUTUBE_PRIVACY_STATUS", "private")
    category_id = args.category_id or optional_env("YOUTUBE_CATEGORY_ID", "27")
    default_language = optional_env("YOUTUBE_DEFAULT_LANGUAGE", "en-US")
    made_for_kids = optional_env("YOUTUBE_MADE_FOR_KIDS", "false").lower() == "true"
    embeddable = optional_env("YOUTUBE_EMBEDDABLE", "true").lower() == "true"
    public_stats_viewable = optional_env("YOUTUBE_PUBLIC_STATS_VIEWABLE", "true").lower() == "true"
    notify_subscribers = optional_env("YOUTUBE_NOTIFY_SUBSCRIBERS", "false").lower() == "true"

    body = {
        "snippet": {
            "title": metadata.title,
            "description": metadata.description,
            "tags": metadata.tags,
            "categoryId": category_id,
            "defaultLanguage": default_language,
            "defaultAudioLanguage": default_language,
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": made_for_kids,
            "embeddable": embeddable,
            "publicStatsViewable": public_stats_viewable,
        },
    }

    insert_request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=MediaFileUpload(str(video_path), chunksize=8 * 1024 * 1024, resumable=True),
        notifySubscribers=notify_subscribers,
    )

    response = resumable_upload(insert_request)
    video_id = response["id"]

    if thumbnail_path and thumbnail_path.exists():
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(thumbnail_path), mimetype="image/png", resumable=False),
        ).execute()

    result = {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": metadata.title,
        "privacy_status": privacy_status,
        "category_id": category_id,
        "thumbnail_uploaded": bool(thumbnail_path and thumbnail_path.exists()),
        "mode": "upload",
        "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    write_upload_record(project_dir, result)
    return result


def write_upload_record(project_dir: Path, result: dict[str, Any]) -> None:
    record_path = project_dir / "assets" / UPLOAD_RECORD_NAME
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(result, indent=2), encoding="utf-8")


def resolve_existing_video_id(project_dir: Path, explicit_video_id: Optional[str]) -> str:
    if explicit_video_id:
        return explicit_video_id

    record_path = project_dir / "assets" / UPLOAD_RECORD_NAME
    if record_path.exists():
        data = json.loads(record_path.read_text(encoding="utf-8"))
        video_id = str(data.get("video_id", "")).strip()
        if video_id:
            return video_id

    raise RuntimeError("No existing video id found. Pass --video-id or ensure assets/youtube_upload.json exists.")


def fetch_existing_video(youtube, video_id: str) -> dict[str, Any]:
    response = youtube.videos().list(part="snippet,status", id=video_id).execute()
    items = response.get("items", [])
    if not items:
        raise RuntimeError(f"Video not found on YouTube: {video_id}")
    return items[0]


def build_desired_video_resource(existing_video: dict[str, Any], metadata: PublishMetadata, args: argparse.Namespace) -> dict[str, Any]:
    privacy_status = args.privacy_status or optional_env("YOUTUBE_PRIVACY_STATUS", "private")
    category_id = args.category_id or optional_env("YOUTUBE_CATEGORY_ID", "27")
    default_language = optional_env("YOUTUBE_DEFAULT_LANGUAGE", "en-US")
    made_for_kids = optional_env("YOUTUBE_MADE_FOR_KIDS", "false").lower() == "true"
    embeddable = optional_env("YOUTUBE_EMBEDDABLE", "true").lower() == "true"
    public_stats_viewable = optional_env("YOUTUBE_PUBLIC_STATS_VIEWABLE", "true").lower() == "true"

    snippet = dict(existing_video.get("snippet", {}))
    status = dict(existing_video.get("status", {}))

    snippet["title"] = metadata.title
    snippet["description"] = metadata.description
    snippet["tags"] = metadata.tags
    snippet["categoryId"] = category_id
    snippet["defaultLanguage"] = default_language
    snippet["defaultAudioLanguage"] = default_language

    status["privacyStatus"] = privacy_status
    status["selfDeclaredMadeForKids"] = made_for_kids
    status["embeddable"] = embeddable
    status["publicStatsViewable"] = public_stats_viewable

    return {
        "id": existing_video["id"],
        "snippet": snippet,
        "status": status,
    }


def diff_video_resource(existing_video: dict[str, Any], desired_video: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    existing_snippet = existing_video.get("snippet", {})
    desired_snippet = desired_video.get("snippet", {})
    existing_status = existing_video.get("status", {})
    desired_status = desired_video.get("status", {})

    for field in ("title", "description", "tags", "categoryId", "defaultLanguage", "defaultAudioLanguage"):
        if existing_snippet.get(field) != desired_snippet.get(field):
            changes.append(
                {
                    "field": f"snippet.{field}",
                    "before": existing_snippet.get(field),
                    "after": desired_snippet.get(field),
                }
            )

    for field in ("privacyStatus", "selfDeclaredMadeForKids", "embeddable", "publicStatsViewable"):
        if existing_status.get(field) != desired_status.get(field):
            changes.append(
                {
                    "field": f"status.{field}",
                    "before": existing_status.get(field),
                    "after": desired_status.get(field),
                }
            )

    return changes


def format_diff_value(field: str, value: Any) -> str:
    if value is None:
        return "<unset>"
    if field == "snippet.description":
        compact = " ".join(str(value).split())
        return truncate_text(compact, 180)
    if field == "snippet.tags":
        tags = ", ".join(value) if isinstance(value, list) else str(value)
        return truncate_text(tags, 180)
    return truncate_text(str(value), 180)


def truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def sync_existing_video(
    project_dir: Path,
    script_dir: Path,
    video_id: str,
    thumbnail_path: Optional[Path],
    metadata: PublishMetadata,
    args: argparse.Namespace,
) -> dict[str, Any]:
    from googleapiclient.http import MediaFileUpload

    youtube = get_authenticated_service(project_dir, script_dir)
    existing_video = fetch_existing_video(youtube, video_id)
    desired_video = build_desired_video_resource(existing_video, metadata, args)
    changed_fields = diff_video_resource(existing_video, desired_video)

    print("Existing video check")
    if changed_fields:
        for change in changed_fields:
            field = change["field"]
            print(f"  will update: {field}")
            print(f"    before: {format_diff_value(field, change['before'])}")
            print(f"    after:  {format_diff_value(field, change['after'])}")
    else:
        print("  metadata already matches local project state")

    thumbnail_uploaded = False
    if not args.dry_run and changed_fields:
        youtube.videos().update(part="snippet,status", body=desired_video).execute()

    if thumbnail_path and thumbnail_path.exists():
        if args.dry_run:
            print("  will update: thumbnail")
        else:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(thumbnail_path), mimetype="image/png", resumable=False),
            ).execute()
            thumbnail_uploaded = True

    result = {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": metadata.title,
        "privacy_status": desired_video["status"]["privacyStatus"],
        "category_id": desired_video["snippet"]["categoryId"],
        "thumbnail_uploaded": thumbnail_uploaded,
        "mode": "sync",
        "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "changed_fields": [change["field"] for change in changed_fields],
    }

    if not args.dry_run:
        write_upload_record(project_dir, result)
    return result


def resumable_upload(insert_request) -> dict:
    from googleapiclient.errors import HttpError

    response = None
    retry = 0
    while response is None:
        try:
            print("Uploading video...")
            _status, response = insert_request.next_chunk()
        except HttpError as exc:
            if exc.resp.status not in RETRIABLE_STATUS_CODES:
                raise
            error = f"Retriable HTTP error {exc.resp.status}: {exc.content}"
        except OSError as exc:
            error = f"Retriable transport error: {exc}"
        else:
            continue

        retry += 1
        if retry > MAX_RETRIES:
            raise RuntimeError("Exceeded maximum upload retries.")
        sleep_seconds = random.random() * (2**retry)
        print(f"{error}\nRetrying in {sleep_seconds:.1f}s...")
        time.sleep(sleep_seconds)

    if "id" not in response:
        raise RuntimeError(f"Upload succeeded but response did not include a video id: {response}")
    return response


def main() -> int:
    args = parse_args()
    project_dir = Path.cwd()
    script_dir = Path(__file__).resolve().parent
    load_runtime_env(project_dir, script_dir)

    try:
        youtube_md_path = project_dir / "youtube.md"
        metadata = parse_youtube_md(youtube_md_path)
        video_path = Path(args.file).expanduser() if args.file else manifest_output_path(project_dir)
        thumbnail_source = select_thumbnail_source(project_dir, youtube_md_path, args)
        thumbnail_path = Path(args.thumbnail).expanduser() if args.thumbnail else project_dir / "assets" / DEFAULT_THUMBNAIL_NAME
        if thumbnail_source and not args.thumbnail:
            downloaded = asyncio_run(download_thumbnail_source(thumbnail_source, thumbnail_path))
            thumbnail_path = downloaded if downloaded and downloaded.exists() else None
        elif thumbnail_path and not thumbnail_path.exists():
            thumbnail_path = None

        if args.sync_existing:
            video_id = resolve_existing_video_id(project_dir, args.video_id)
            print_sync_summary(video_id, thumbnail_path, metadata)
            result = sync_existing_video(project_dir, script_dir, video_id, thumbnail_path, metadata, args)
            if args.dry_run:
                return 0
            print("\nSync complete")
        else:
            if not video_path.exists():
                raise FileNotFoundError(f"Video file not found: {video_path}")
            print_runtime_summary(video_path, thumbnail_path, metadata)
            if args.dry_run:
                return 0
            result = upload_video(project_dir, script_dir, video_path, thumbnail_path, metadata, args)
            print("\nUpload complete")

        print(f"  Video id: {result['video_id']}")
        print(f"  URL: {result['url']}")
        print(f"  Record: {project_dir / 'assets' / UPLOAD_RECORD_NAME}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"Publish failed: {exc}", file=sys.stderr)
        return 1


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


if __name__ == "__main__":
    raise SystemExit(main())
