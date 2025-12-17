#!/usr/bin/env python3
"""Instagram Daily Archive utility using the Instagram Graph API.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlparse

import requests

DEFAULT_API_VERSION = os.getenv("IG_API_VERSION", "v19.0")
GRAPH_API_BASE = f"https://graph.facebook.com/{DEFAULT_API_VERSION}"
STATE_FILENAME = "state.json"

# IG User Media edge request fields required to archive the feed.
# This follows Meta's documentation for GET /{ig-user-id}/media to fetch a
# user's media along with carousel children details needed for downloads.
MEDIA_FIELDS = "id,caption,media_type,media_url,permalink,timestamp,children{id,media_url,media_type}"


class ArchiveError(Exception):
    """Raised for unrecoverable archiving errors."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Archive Instagram media locally.")
    parser.add_argument(
        "--output-dir",
        default=os.getenv("IG_ARCHIVE_DIR", "InstagramArchive"),
        help="Directory where media and metadata will be stored.",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Optional path for the log file. Defaults to <output-dir>/archive.log.",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=50,
        help="Number of media items to request per page (max allowed by API is 50).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Optional limit to the number of pages fetched (useful for debugging).",
    )
    return parser.parse_args()


def ensure_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def get_env_var(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ArchiveError(f"Environment variable {name} is required.")
    return value


def load_state(output_dir: Path) -> Dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / STATE_FILENAME
    if not state_path.exists():
        return {
            "last_saved_media_id": None,
            "last_run_iso": None,
            "processed_ids": [],
        }

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ArchiveError(f"Invalid state file: {state_path}") from exc

    state.setdefault("processed_ids", [])
    state.setdefault("last_saved_media_id", None)
    state.setdefault("last_run_iso", None)
    return state


def save_state(output_dir: Path, state: Dict) -> None:
    state_path = output_dir / STATE_FILENAME
    state["last_run_iso"] = datetime.utcnow().isoformat()
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def request_with_retry(url: str, params: Dict[str, str], max_attempts: int = 5) -> Dict:
    backoff = 1.5
    session = requests.Session()
    for attempt in range(1, max_attempts + 1):
        try:
            response = session.get(url, params=params, timeout=30)
            if response.status_code == 429 or 500 <= response.status_code < 600:
                logging.warning(
                    "Request to %s failed with status %s (attempt %s/%s).",
                    url,
                    response.status_code,
                    attempt,
                    max_attempts,
                )
                raise ArchiveError("Temporary API error")

            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise ArchiveError(str(data["error"]))
            return data
        except (requests.RequestException, ArchiveError) as exc:  # noqa: PERF203
            if attempt == max_attempts:
                raise ArchiveError(f"Request failed after {max_attempts} attempts: {exc}")
            sleep_time = backoff ** attempt
            logging.info("Retrying in %.1f seconds...", sleep_time)
            time.sleep(sleep_time)
    raise ArchiveError("Unreachable code reached in request_with_retry")


def fetch_media_page(url: str, params: Optional[Dict[str, str]]) -> Dict:
    return request_with_retry(url, params or {})


def parse_timestamp(timestamp: str) -> datetime:
    normalized = timestamp
    if timestamp.endswith("Z"):
        normalized = timestamp.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ArchiveError(f"Invalid timestamp format: {timestamp}") from exc


def ensure_media_dir(base_dir: Path, timestamp: str, media_id: str) -> Path:
    dt = parse_timestamp(timestamp)
    media_dir = base_dir / dt.strftime("%Y-%m-%d") / media_id
    media_dir.mkdir(parents=True, exist_ok=True)
    return media_dir


def derive_extension(url: str) -> str:
    path = urlparse(url).path
    suffix = Path(path).suffix
    return suffix or ""


def download_file(url: str, dest: Path) -> None:
    if dest.exists():
        logging.info("File %s already exists; skipping download.", dest)
        return

    logging.info("Downloading %s -> %s", url, dest)
    with requests.get(url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as handle:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    handle.write(chunk)


def save_metadata(media_dir: Path, metadata: Dict) -> None:
    metadata_path = media_dir / "meta.json"
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    caption = metadata.get("caption") or ""
    (media_dir / "caption.txt").write_text(caption, encoding="utf-8")


def archive_children(media_dir: Path, children: Iterable[Dict]) -> List[str]:
    downloaded: List[str] = []
    for index, child in enumerate(children, start=1):
        url = child.get("media_url") or child.get("thumbnail_url")
        if not url:
            logging.warning("Child %s has no downloadable URL; skipping.", child.get("id"))
            continue
        ext = derive_extension(url)
        dest = media_dir / f"child_{index:02d}{ext}"
        download_file(url, dest)
        downloaded.append(str(dest))
    return downloaded


def archive_media_item(base_dir: Path, media: Dict) -> None:
    media_id = media["id"]
    timestamp = media.get("timestamp") or ""
    if not timestamp:
        logging.warning("Skipping media %s without timestamp.", media_id)
        return

    media_dir = ensure_media_dir(base_dir, timestamp, media_id)
    save_metadata(media_dir, media)

    download_target = media.get("media_url") or media.get("thumbnail_url")
    if download_target:
        ext = derive_extension(download_target)
        download_file(download_target, media_dir / f"media_01{ext}")
    else:
        logging.warning("No media_url/thumbnail_url for %s; metadata saved only.", media_id)

    children = media.get("children", {}).get("data", []) if isinstance(media.get("children"), dict) else []
    if children:
        logging.info("Archiving %s children for carousel %s", len(children), media_id)
        archive_children(media_dir, children)


def archive(user_id: str, access_token: str, output_dir: Path, page_size: int, max_pages: Optional[int]) -> None:
    state = load_state(output_dir)
    last_saved_id = state.get("last_saved_media_id")
    processed_ids = set(state.get("processed_ids", []))
    logging.info("Last archived media id: %s", last_saved_id or "<none>")

    current_url: Optional[str] = f"{GRAPH_API_BASE}/{user_id}/media"
    params: Optional[Dict[str, str]] = {
        # GET /{ig-user-id}/media with the media fields required to archive
        # content locally (id, caption, media type/url, timestamp, permalink)
        # and carousel children (media_url + media_type) per Meta docs.
        "fields": MEDIA_FIELDS,
        "access_token": access_token,
        "limit": str(page_size),
    }
    pages_fetched = 0
    new_latest_id: Optional[str] = None
    reached_existing = False

    while current_url:
        if max_pages is not None and pages_fetched >= max_pages:
            logging.info("Reached max page limit (%s).", max_pages)
            break

        payload = fetch_media_page(current_url, params)
        params = None  # Subsequent requests use the fully qualified paging.next URL.
        media_list = payload.get("data", [])
        if not media_list:
            logging.info("No more media returned by API.")
            break

        pages_fetched += 1
        logging.info("Fetched page %s with %s items.", pages_fetched, len(media_list))

        for media in media_list:
            media_id = media.get("id")
            if not media_id:
                continue
            if new_latest_id is None:
                new_latest_id = media_id
            if media_id in processed_ids:
                logging.info("Media %s already processed; skipping.", media_id)
                continue
            if last_saved_id and media_id == last_saved_id:
                reached_existing = True
                logging.info("Reached previously archived media id %s; stopping.", media_id)
                break
            archive_media_item(output_dir, media)
            processed_ids.add(media_id)

        paging = payload.get("paging", {})
        next_url = paging.get("next") if isinstance(paging, dict) else None
        current_url = next_url if not reached_existing else None
        if reached_existing or not current_url:
            break

    if new_latest_id:
        state["last_saved_media_id"] = new_latest_id
        logging.info("Updated state marker to %s", new_latest_id)

    state["processed_ids"] = sorted(processed_ids)
    save_state(output_dir, state)


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    log_file = Path(args.log_file) if args.log_file else output_dir / "archive.log"
    ensure_logging(log_file)

    try:
        user_id = get_env_var("IG_USER_ID")
        access_token = get_env_var("IG_ACCESS_TOKEN")
    except ArchiveError as exc:
        logging.error(exc)
        sys.exit(1)

    try:
        archive(user_id, access_token, output_dir, args.page_size, args.max_pages)
    except ArchiveError as exc:
        logging.error("Archiving failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
