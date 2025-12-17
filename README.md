# Instagram Daily Archive

Lightweight, **official-API-only** archiver for your Instagram professional account. The script uses the Instagram Graph API to download your media, captions, and metadata into a predictable folder structure that can be scheduled daily.

## What it does
- Calls only documented Instagram Graph API endpoints (`/{ig-user-id}/media`, `/{media-id}/children`, and `oauth/access_token`).
- Saves each media item (and carousel children) to disk with metadata and captions.
- Idempotent: respects a `state.json` marker so reruns skip already archived posts.
- Retries with backoff and redacts tokens from logs to keep secrets out of files.

## Requirements
- Python **3.11+**.
- `requests` (install with `python -m pip install --upgrade requests`).
- An **Instagram professional account** (Business or Creator) linked to a Facebook Page, plus a long-lived user access token with `instagram_basic` and `pages_show_list` (and any additional scopes required for your approved use case). Make sure you are following Meta platform policies and only accessing your own data with permission.

## Environment variables
Copy `config.example.env` to `.env` (or export the variables) and fill in the values:

| Variable | Required | Description |
| --- | --- | --- |
| `IG_USER_ID` | Yes | Instagram Business/User ID for the linked professional account. |
| `IG_ACCESS_TOKEN` | Yes | Long-lived user access token for the Graph API. Refresh per Meta guidance. |
| `IG_API_VERSION` | No | Graph API version (defaults to `v19.0`). |
| `IG_ARCHIVE_DIR` | No | Output directory for the archive (defaults to `InstagramArchive`). |

> The script never writes token values to disk or console; query parameters containing `token` are redacted in logs.

## Setup
1. Ensure you have a long-lived token for your professional account. Follow Meta docs to exchange a short-lived token if needed.
2. Create a working directory and clone/download this project.
3. Copy the example environment file and edit your values:
   ```bash
   cp config.example.env .env
   # edit .env to set IG_USER_ID and IG_ACCESS_TOKEN
   ```
4. Install the dependency:
   ```bash
   python -m pip install --upgrade requests
   ```

## Usage
Run commands from the project root (ensure environment variables are set in your shell or `.env` loader):

- **Archive all available media (default):**
  ```bash
  python ig_archive.py run --output-dir /path/to/InstagramArchive
  ```
- **Archive until last saved media (for daily runs):**
  ```bash
  python ig_archive.py run --since-last
  ```
- **Backfill older media with a page cap:**
  ```bash
  python ig_archive.py backfill --max-pages 5
  ```
- **Verify configuration and token without downloading media:**
  ```bash
  python ig_archive.py doctor
  ```

### Optional flags
- `--page-size` (default `50`): number of items per API page (Instagram Graph API max is 50).
- `--max-pages`: stop after N pages (useful for testing/backfills).
- `--log-file`: override the log path (defaults to `<output-dir>/archive.log`).

## Output layout
```
InstagramArchive/
  state.json           # run bookkeeping (last_saved_media_id, processed_ids, last_run_iso)
  archive.log          # log file (tokens redacted)
  2024-05-20/
    1234567890/
      meta.json        # full API payload for the media item
      caption.txt      # caption (empty if none)
      media_01.jpg     # main media file
      child_01.jpg     # carousel children (child_02.jpg, ...)
```
Re-running is safe: files are skipped if they already exist and the crawler stops when it reaches the previously saved media ID (when `--since-last` is used).

## Scheduling
Example cron entry (daily at 02:15 UTC):
```
15 2 * * * cd /path/to/instArchiver \
  && IG_USER_ID=123 IG_ACCESS_TOKEN=EAAG... \
  /usr/bin/python ig_archive.py run --output-dir /path/to/InstagramArchive \
  >> /path/to/InstagramArchive/archive.log 2>&1
```
On Windows Task Scheduler, create a basic task that runs `python ig_archive.py run` from the project directory and set the required environment variables in the task configuration.

## Token helper (optional)
You can exchange a short-lived token for a long-lived one using the helper:
```python
from ig_archive import exchange_short_lived_token

payload = exchange_short_lived_token(
    short_lived_token="<short-lived-token>",
    app_id="<app-id>",
    app_secret="<app-secret>",
)
print(payload["access_token"], payload.get("expires_in"))
```
Store the returned long-lived token securely and set it via `IG_ACCESS_TOKEN`.

## Notes on compliance
- The script uses only documented Instagram Graph API endpoints and requires you to supply a token issued for your account. It does not scrape or automate UI interactions.
- Ensure your app and token have the correct permissions and that you comply with Meta's Platform Policies when downloading or storing data.
