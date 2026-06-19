#!/usr/bin/env python3
"""Generate temporary video thumbnails for GitHub Pages deployment artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

VIDEO_THUMB_DIR = "video_thumbs"
MAX_VIDEO_THUMBS = 80
MAX_WORKERS = 6
FFMPEG_TIMEOUT_SECONDS = 12
SOURCE_DATA_PATTERN = re.compile(
    r"(const sourceData = )(.*?)(;\n\s*let current(?:Category|Timeline) =)",
    flags=re.S,
)


def is_video_url(url: str) -> bool:
    if "video.twimg.com" in url:
        return True
    return bool(re.search(r"\.(mp4|webm|m3u8)(?:\?|$)", url, flags=re.I))


def extract_source_data(html_path: Path) -> tuple[str, list[dict[str, Any]], re.Match[str]]:
    html = html_path.read_text(encoding="utf-8")
    matched = SOURCE_DATA_PATTERN.search(html)
    if not matched:
        raise RuntimeError(f"sourceData block not found: {html_path}")
    source_data = json.loads(matched.group(2))
    if not isinstance(source_data, list):
        source_data = []
    rows = [dict(item) for item in source_data if isinstance(item, dict)]
    return html, rows, matched


def write_source_data(html_path: Path, html: str, rows: list[dict[str, Any]]) -> None:
    encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    replaced = SOURCE_DATA_PATTERN.sub(
        lambda match: f"{match.group(1)}{encoded}{match.group(3)}",
        html,
        count=1,
    )
    html_path.write_text(replaced, encoding="utf-8")


def build_video_thumbnails(rows: list[dict[str, Any]], html_path: Path) -> None:
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        print("[INFO] Video thumbnails: ffmpeg not found; using fallback video cards.")
        return

    urls: list[str] = []
    for item in rows:
        for url in item.get("mediaUrls", []):
            if isinstance(url, str) and is_video_url(url) and url not in urls:
                urls.append(url)

    if not urls:
        print(f"[INFO] Video thumbnails: no video URLs found in {html_path}.")
        return

    thumb_dir = html_path.parent / VIDEO_THUMB_DIR
    thumb_dir.mkdir(parents=True, exist_ok=True)

    target_urls = urls[:MAX_VIDEO_THUMBS]
    existing_count = sum(
        1
        for url in target_urls
        if (thumb_dir / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()[:20]}.jpg").exists()
    )
    new_count = len(target_urls) - existing_count
    skipped_count = max(0, len(urls) - len(target_urls))
    estimated_seconds = max(1, int((new_count / MAX_WORKERS) * FFMPEG_TIMEOUT_SECONDS * 0.75))
    print(
        "[INFO] Video thumbnails: "
        f"{html_path}: {len(urls)} videos found, {existing_count} cached, "
        f"{new_count} to generate"
        f"{f', {skipped_count} skipped by cap' if skipped_count else ''}. "
        f"Estimated time: about {estimated_seconds:.0f}s."
    )

    def ensure_thumb(url: str) -> tuple[str, str | None, bool]:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
        thumb_path = thumb_dir / f"{digest}.jpg"
        if not thumb_path.exists() or thumb_path.stat().st_size == 0:
            cmd = [
                ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                "00:00:00.4",
                "-rw_timeout",
                str(FFMPEG_TIMEOUT_SECONDS * 1_000_000),
                "-i",
                url,
                "-frames:v",
                "1",
                "-vf",
                "scale='min(720,iw)':-2",
                "-q:v",
                "3",
                str(thumb_path),
            ]
            try:
                subprocess.run(cmd, check=True, timeout=FFMPEG_TIMEOUT_SECONDS)
            except (subprocess.SubprocessError, OSError):
                if thumb_path.exists() and thumb_path.stat().st_size == 0:
                    thumb_path.unlink(missing_ok=True)
                return url, None, False
        if thumb_path.exists() and thumb_path.stat().st_size > 0:
            return url, f"{VIDEO_THUMB_DIR}/{thumb_path.name}", True
        return url, None, False

    thumb_map: dict[str, str] = {}
    started_at = time.monotonic()
    generated_count = 0
    failed_count = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(ensure_thumb, url) for url in target_urls]
        for future in as_completed(futures):
            url, relative_path, ok = future.result()
            if ok and relative_path:
                thumb_map[url] = relative_path
                generated_count += 1
            else:
                failed_count += 1

    elapsed = time.monotonic() - started_at
    print(
        "[INFO] Video thumbnails: "
        f"{generated_count} generated, {len(thumb_map)} available, {failed_count} failed "
        f"in {elapsed:.1f}s."
    )

    if not thumb_map:
        return

    for item in rows:
        item["mediaThumbs"] = {
            url: thumb_map[url]
            for url in item.get("mediaUrls", [])
            if isinstance(url, str) and url in thumb_map
        }


def process_html(html_path: Path) -> None:
    html, rows, _matched = extract_source_data(html_path)
    build_video_thumbnails(rows, html_path)
    write_source_data(html_path, html, rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate temporary dashboard video thumbnails for Pages artifacts."
    )
    parser.add_argument("html", nargs="+", help="Dashboard index.html path(s).")
    args = parser.parse_args()

    for raw_path in args.html:
        path = Path(raw_path)
        if path.exists():
            process_html(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
