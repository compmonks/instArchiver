# Instagram Daily Archive

Local-only archiver for your Instagram account using the official Instagram Graph API. The script pulls your media daily (or on demand), saves metadata and captions, and downloads each media file to a date/media-id folder structure.

## Features
- Uses only official Instagram Graph API endpoints (no scraping, no browsers).
- Idempotent: stops once it reaches the last saved media ID marker.
- Handles carousels by downloading every child item.
- Retries with exponential backoff and logs to a file.
- Works from cron/Task Scheduler or manual runs.

## Setup
1. Create a long-lived Instagram access token and the Instagram Business/User ID from Meta.
2. Copy the sample environment file and fill in your values:
   ```bash
   cp config.example.env .env
   # edit .env to set IG_USER_ID and IG_ACCESS_TOKEN
   ```
3. (Optional) Export the env vars directly instead of using a `.env` loader:
   ```bash
   export IG_USER_ID=1234567890
   export IG_ACCESS_TOKEN=EAAG...
   export IG_API_VERSION=v19.0  # optional
   export IG_ARCHIVE_DIR=archive  # optional output directory
   ```
4. Ensure Python 3.11+ is available. Install the only dependency:
   ```bash
   python -m pip install --upgrade requests
   ```

## Running the archive
```bash
python ig_archive.py --output-dir archive
```

Useful flags:
- `--page-size`: up to 50; defaults to 50.
- `--max-pages`: stop after N pages (helpful for testing).
- `--log-file`: override log location (defaults to `<output-dir>/archive.log`).

Outputs are organized as:
```
archive/
  2024-05-20/
    1234567890/
      metadata.json
      caption.txt
      <media file(s)>
      child_<child-id>_<filename>
  last_media_id.txt
  archive.log
```

Re-running the script is safe: it skips existing files and halts once it encounters the `last_media_id.txt` marker.

## Automation
Add a cron entry (runs daily at 2:15 AM):
```
15 2 * * * cd /path/to/instArchiver && /usr/bin/env IG_USER_ID=... IG_ACCESS_TOKEN=... /usr/bin/python ig_archive.py --output-dir /path/to/archive >> /path/to/archive/cron.log 2>&1
```
On Windows Task Scheduler, create a basic task that calls `python ig_archive.py` with the environment variables set in the task configuration.

## Troubleshooting
- Ensure the access token has not expired and includes `instagram_basic` and `pages_show_list` permissions.
- Check `archive.log` for rate-limit or permission errors.
- Use `--max-pages 1` for a quick sanity check while configuring.
