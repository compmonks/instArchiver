#!/usr/bin/env python3
"""Instagram Daily Archive utility using the Instagram Graph API.

This script archives media for an Instagram professional account using only
documented Graph API endpoints. It does not scrape or automate any UI flows
and requires you to supply a valid long-lived user access token. All token
values are read from environment variables and redacted from logs.
"""
from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

DEFAULT_API_VERSION = os.getenv("IG_API_VERSION", "v19.0")
GRAPH_API_BASE = f"https://graph.facebook.com/{DEFAULT_API_VERSION}"
DEFAULT_ARCHIVE_DIR = (
    os.getenv("IG_ARCHIVE_DATA_DIR")
    or os.getenv("IG_ARCHIVE_DIR")
    or "InstagramArchive"
)
STATE_FILENAME = "state.json"

JsonDict = Dict[str, Any]

# IG User Media edge request fields required to archive the feed.
# This follows Meta's documentation for GET /{ig-user-id}/media to fetch a
# user's media along with carousel children details needed for downloads.
MEDIA_FIELDS = "id,caption,media_type,media_url,permalink,timestamp,children{id,media_url,media_type}"
CHILDREN_FIELDS = "id,media_type,media_url,thumbnail_url,timestamp"


class ArchiveError(Exception):
    """Raised for unrecoverable archiving errors."""


def parse_args() -> argparse.Namespace:
    """Define CLI arguments and subcommands."""
    parser = argparse.ArgumentParser(description="Archive Instagram media locally.")
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_ARCHIVE_DIR,
        help="Directory where media and metadata will be stored.",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Optional path for the log file. Defaults to <output-dir>/archive.log.",
    )

    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser(
        "run", help="Archive Instagram media (default command)."
    )
    run_parser.set_defaults(command="run")
    run_parser.add_argument(
        "--page-size",
        type=int,
        default=50,
        help="Number of media items to request per page (max allowed by API is 50).",
    )
    run_parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Optional limit to the number of pages fetched (useful for debugging).",
    )
    run_parser.add_argument(
        "--since-last",
        action="store_true",
        help="Stop when the previously saved media id is encountered.",
    )

    doctor_parser = subparsers.add_parser(
        "doctor", help="Validate environment variables and configuration."
    )
    doctor_parser.set_defaults(command="doctor")

    backfill_parser = subparsers.add_parser(
        "backfill", help="Page backwards through media with an optional limit."
    )
    backfill_parser.set_defaults(command="backfill")
    backfill_parser.add_argument(
        "--page-size",
        type=int,
        default=50,
        help="Number of media items to request per page (max allowed by API is 50).",
    )
    backfill_parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Optional limit to the number of pages fetched (useful for debugging).",
    )

    args = parser.parse_args()
    if args.command is None:
        args.command = "run"
        args.page_size = 50
        args.max_pages = None
        args.since_last = False

    return args


def ensure_logging(log_file: Path) -> None:
    """Configure logging to both file and stdout."""
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


def load_credentials() -> tuple[str, str]:
    """Fetch the Instagram user ID and access token from the environment."""

    user_id = get_env_var("IG_USER_ID")
    access_token = get_env_var("IG_ACCESS_TOKEN")
    return user_id, access_token


def check_write_permissions(path: Path) -> None:
    """Verify that the process can create and write files in the path."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        test_file = path / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
    except OSError as exc:
        raise ArchiveError(f"Cannot write to {path}: {exc}") from exc


def load_state(output_dir: Path) -> JsonDict:
    """Load run state from disk (idempotent if state file is missing)."""
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


def save_state(output_dir: Path, state: JsonDict) -> None:
    """Persist run state to disk with an updated timestamp."""
    state_path = output_dir / STATE_FILENAME
    state["last_run_iso"] = datetime.utcnow().isoformat()
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def validate_access_token(user_id: str, access_token: str) -> None:
    """Run a lightweight Graph API call to verify token validity without logging it."""

    url = f"{GRAPH_API_BASE}/{user_id}"
    params = {
        "fields": "id,username",
        "access_token": access_token,
    }

    try:
        response = requests.get(url, params=params, timeout=15)
    except requests.RequestException as exc:  # noqa: PERF203
        raise ArchiveError(
            f"Token validation request failed: {safe_error_context(exc)}"
        ) from exc

    if response.status_code == 401:
        raise ArchiveError(
            "Access token unauthorized (401). Refresh the long-lived token following Meta docs."
        )

    try:
        data = response.json()
    except ValueError as exc:  # noqa: PERF203
        raise ArchiveError("Unexpected response during token validation.") from exc

    if "error" in data:
        message = data.get("error", {}).get("message") or "Unknown error"
        raise ArchiveError(f"Access token validation failed: {message}")

    resolved_id = data.get("id")
    if resolved_id and resolved_id != user_id:
        raise ArchiveError(
            "Validated token belongs to a different Instagram account; check IG_USER_ID."
        )

    logging.info("Access token validated for Instagram user %s", resolved_id or user_id)


def exchange_short_lived_token(
    short_lived_token: str, app_id: str, app_secret: str
) -> Dict[str, str]:
    """
    Optional helper to exchange a short-lived token for a long-lived one using
    Meta's access token endpoint. Returns the long-lived token payload (includes
    `access_token` and `expires_in`). Keep tokens out of logs when calling this.
    """

    url = f"{GRAPH_API_BASE}/oauth/access_token"
    params = {
        "grant_type": "fb_exchange_token",
        "client_id": app_id,
        "client_secret": app_secret,
        "fb_exchange_token": short_lived_token,
    }

    try:
        response = requests.get(url, params=params, timeout=15)
    except requests.RequestException as exc:  # noqa: PERF203
        raise ArchiveError(
            f"Long-lived token exchange request failed: {safe_error_context(exc)}"
        ) from exc

    try:
        data = response.json()
    except ValueError as exc:  # noqa: PERF203
        raise ArchiveError("Unexpected response during token exchange.") from exc

    if not response.ok:
        error_message = data.get("error", {}).get("message") if isinstance(data, dict) else None
        message = error_message or f"HTTP {response.status_code} during token exchange"
        raise ArchiveError(message)

    if "access_token" not in data:
        raise ArchiveError("Long-lived token exchange response missing access_token.")

    return data


def redact_tokens(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    redacted_query = [
        (key, value)
        for key, value in query
        if "token" not in key.lower()
    ]
    sanitized_query = urlencode(redacted_query, doseq=True)
    sanitized = parsed._replace(query=sanitized_query)
    return urlunparse(sanitized)


def safe_error_context(exc: Exception) -> str:
    if isinstance(exc, requests.RequestException):
        request_url = getattr(getattr(exc, "request", None), "url", None)
        status = getattr(getattr(exc, "response", None), "status_code", None)
        parts = []
        if status:
            parts.append(f"status {status}")
        if request_url:
            parts.append(f"url {redact_tokens(request_url)}")
        return "; ".join(parts) or exc.__class__.__name__
    return str(exc)


def request_with_retry(url: str, params: Dict[str, str], max_attempts: int = 5) -> JsonDict:
    """Perform a GET request with retry handling for transient errors."""
    backoff = 1.5
    session = requests.Session()
    for attempt in range(1, max_attempts + 1):
        try:
            response = session.get(url, params=params, timeout=30)
            if response.status_code == 429 or 500 <= response.status_code < 600:
                logging.warning(
                    "Request to %s failed with status %s (attempt %s/%s).",
                    redact_tokens(url),
                    response.status_code,
                    attempt,
                    max_attempts,
                )
                raise ArchiveError("Temporary API error")

            if not response.ok:
                error_message = None
                try:
                    error_message = response.json().get("error", {}).get("message")
                except ValueError:
                    error_message = response.text or None
                message = error_message or f"HTTP {response.status_code}"
                raise ArchiveError(message)

            data = response.json()
            if "error" in data:
                raise ArchiveError(str(data["error"]))
            return data
        except (requests.RequestException, ArchiveError) as exc:  # noqa: PERF203
            if attempt == max_attempts:
                safe_detail = safe_error_context(exc)
                raise ArchiveError(
                    f"Request failed after {max_attempts} attempts: {safe_detail}"
                )
            sleep_time = backoff ** attempt
            logging.info("Retrying in %.1f seconds...", sleep_time)
            time.sleep(sleep_time)
    raise ArchiveError("Unreachable code reached in request_with_retry")


def fetch_media_page(url: str, params: Optional[Dict[str, str]]) -> JsonDict:
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


def determine_extension(content_type: Optional[str], url: str) -> str:
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed:
            return guessed
    return derive_extension(url)


def download_file(url: str, dest: Path, timeout: int = 30, max_attempts: int = 3) -> Optional[Path]:
    try:
        if dest.exists() and dest.stat().st_size > 0:
            logging.info("File %s already exists; skipping download.", dest)
            return dest
    except OSError as exc:
        logging.warning("Could not check existing file %s: %s", dest, exc)

    dest.parent.mkdir(parents=True, exist_ok=True)
    backoff = 1.5

    for attempt in range(1, max_attempts + 1):
        try:
            with requests.get(url, stream=True, timeout=timeout) as resp:
                resp.raise_for_status()
                ext = dest.suffix or determine_extension(resp.headers.get("Content-Type"), url)
                final_dest = dest if dest.suffix or not ext else dest.with_suffix(ext)

                try:
                    if final_dest.exists() and final_dest.stat().st_size > 0:
                        logging.info("File %s already exists; skipping download.", final_dest)
                        return final_dest
                except OSError as exc:
                    logging.warning("Could not check existing file %s: %s", final_dest, exc)

                tmp_path = final_dest.with_suffix(final_dest.suffix + ".part")
                with tmp_path.open("wb") as handle:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            handle.write(chunk)
                tmp_path.replace(final_dest)
                logging.info("Downloaded %s -> %s", url, final_dest)
                return final_dest
        except requests.RequestException as exc:
            logging.warning(
                "Download failed for %s (attempt %s/%s): %s",
                url,
                attempt,
                max_attempts,
                exc,
            )
            if attempt < max_attempts:
                sleep_time = backoff ** attempt
                logging.info("Retrying download in %.1f seconds...", sleep_time)
                time.sleep(sleep_time)
        except OSError as exc:
            logging.error("Failed to write %s: %s", dest, exc)
            break

    logging.error("Giving up on downloading %s after %s attempts.", url, max_attempts)
    return None


def save_metadata(media_dir: Path, metadata: JsonDict) -> None:
    metadata_path = media_dir / "meta.json"
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    caption = metadata.get("caption") or ""
    (media_dir / "caption.txt").write_text(caption, encoding="utf-8")


def archive_children(media_dir: Path, children: Iterable[JsonDict]) -> List[str]:
    """Download carousel children media into the media directory."""
    ordered_children = sorted(
        list(children),
        key=lambda child: (
            child.get("timestamp") or "",
            child.get("id") or "",
        ),
    )
    downloaded: List[str] = []
    for index, child in enumerate(ordered_children, start=1):
        url = child.get("media_url") or child.get("thumbnail_url")
        if not url:
            logging.warning("Child %s has no downloadable URL; skipping.", child.get("id"))
            continue
        dest = media_dir / f"child_{index:02d}"
        downloaded_path = download_file(url, dest)
        if downloaded_path:
            downloaded.append(str(downloaded_path))
    return downloaded


def fetch_carousel_children(media_id: str, access_token: str) -> List[JsonDict]:
    """Fetch carousel children using the `/children` edge for a media item."""
    url = f"{GRAPH_API_BASE}/{media_id}/children"
    params: Optional[Dict[str, str]] = {
        "fields": CHILDREN_FIELDS,
        "access_token": access_token,
    }
    children: List[JsonDict] = []

    while url:
        payload = fetch_media_page(url, params)
        params = None
        children.extend(payload.get("data", []))
        paging = payload.get("paging", {})
        url = paging.get("next") if isinstance(paging, dict) else None

    return children


def archive_media_item(base_dir: Path, media: JsonDict, access_token: str) -> None:
    """Persist metadata and download media (including carousel children)."""
    media_id = media["id"]
    timestamp = media.get("timestamp") or ""
    if not timestamp:
        logging.warning("Skipping media %s without timestamp.", media_id)
        return

    media_dir = ensure_media_dir(base_dir, timestamp, media_id)
    save_metadata(media_dir, media)

    download_target = media.get("media_url") or media.get("thumbnail_url")
    if download_target:
        download_file(download_target, media_dir / "media_01")
    else:
        logging.warning("No media_url/thumbnail_url for %s; metadata saved only.", media_id)

    is_carousel = media.get("media_type") == "CAROUSEL_ALBUM"
    children_payload = media.get("children") if isinstance(media.get("children"), dict) else {}
    children_data = children_payload.get("data", []) if isinstance(children_payload, dict) else []

    if is_carousel and not children_data:
        logging.info("Fetching carousel children for %s via children edge", media_id)
        children_data = fetch_carousel_children(media_id, access_token)

    if children_data:
        logging.info("Archiving %s children for carousel %s", len(children_data), media_id)
        archive_children(media_dir, children_data)


def archive(
    user_id: str,
    access_token: str,
    output_dir: Path,
    page_size: int,
    max_pages: Optional[int],
    stop_at_last_saved: bool,
) -> None:
    """Archive media for the configured Instagram user."""
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
            if stop_at_last_saved and last_saved_id and media_id == last_saved_id:
                reached_existing = True
                logging.info("Reached previously archived media id %s; stopping.", media_id)
                break
            archive_media_item(output_dir, media, access_token)
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


def run_doctor(output_dir: Path, log_file: Path) -> None:
    """Validate environment variables, token, and filesystem permissions."""
    user_id, access_token = load_credentials()

    check_write_permissions(output_dir)
    check_write_permissions(log_file.parent)
    validate_access_token(user_id, access_token)

    logging.info("IG_USER_ID present: %s", user_id)
    logging.info("Log file path: %s", log_file)
    logging.info("Output directory writable: %s", output_dir)
    logging.info("Access token validated successfully.")


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    log_file = Path(args.log_file) if args.log_file else output_dir / "archive.log"
    try:
        check_write_permissions(output_dir)
        check_write_permissions(log_file.parent)
    except ArchiveError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)

    ensure_logging(log_file)

    if args.command == "doctor":
        try:
            run_doctor(output_dir, log_file)
        except ArchiveError as exc:
            logging.error(exc)
            sys.exit(1)
        return

    try:
        user_id, access_token = load_credentials()
        validate_access_token(user_id, access_token)
    except ArchiveError as exc:
        logging.error(exc)
        sys.exit(1)

    try:
        archive(
            user_id,
            access_token,
            output_dir,
            args.page_size,
            args.max_pages,
            stop_at_last_saved=getattr(args, "since_last", False),
        )
    except ArchiveError as exc:
        logging.error("Archiving failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
