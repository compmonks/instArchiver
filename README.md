# Instagram Daily Archive

Local-only archiver for your Instagram account using the official Instagram Graph API. The script pulls your media daily (or on demand), saves metadata and captions, and downloads each media file to a date/media-id folder structure.

## Features
- Uses only official Instagram Graph API endpoints (no scraping, no browsers).
- Idempotent: stops once it reaches the last saved media ID marker.
- Handles carousels by downloading every child item.
- Retries with exponential backoff and logs to a file.
- Works from cron/Task Scheduler or manual runs.

## Setup
### Instagram Graph API access checklist (required for legal use)
1. Use a **professional Instagram account** (Creator or Business). Personal accounts cannot access the Instagram Graph API.【F:https://developers.facebook.com/docs/instagram-api/getting-started†L25-L33】
2. **Link the Instagram professional account to a Facebook Page** you manage; this creates/associates the Instagram Business Account (IBA). The **Instagram Business Account ID** comes from the linked Page (e.g., via the Page's `instagram_business_account` field in Graph API or Page settings).【F:https://developers.facebook.com/docs/instagram-api/getting-started†L41-L50】
3. Create a **Facebook App** with Instagram Basic Display/Instagram Graph permissions and note your **App ID**, **App Secret**, and a **Redirect URI** that you control.【F:https://developers.facebook.com/docs/instagram-basic-display-api/getting-started†L50-L67】
4. Obtain a **user access token** with the scopes `instagram_basic`, `pages_show_list`, and `pages_read_engagement` (plus any others your use-case requires). Start with a **short-lived user access token** (via the OAuth authorize → redirect → `code` → token exchange flow) and then exchange it for a **long-lived user access token** to avoid quick expiry.【F:https://developers.facebook.com/docs/instagram-basic-display-api/guides/getting-access-tokens-and-permissions†L1-L36】
5. The OAuth/token exchange path requires you to collect: `app_id`, `app_secret`, `redirect_uri`, the temporary `code` returned to your redirect URI, the initial short-lived `access_token`, and (after exchange) the long-lived `access_token` and its expiry. Perform the long-lived exchange by calling Meta's **Access Token endpoint** with your short-lived token, App ID, and App Secret.【F:https://developers.facebook.com/docs/facebook-login/guides/access-tokens/get-long-lived†L36-L59】
6. Long-lived tokens can be refreshed (via the same endpoint) before expiration; store the token and its expiry securely.

### Project configuration
1. Create a long-lived Instagram access token and the Instagram Business/User ID from Meta (see checklist above).
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
