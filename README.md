# yt-podcast

Automated pipeline that monitors your favorite YouTube channels and interest topics, extracts key highlights, translates and synthesizes a ~15-minute spoken Chinese podcast digest, renders natural Mandarin MP3 audio via Edge TTS, and publishes an Apple Podcasts / Spotify-compatible RSS feed (`feed.xml`) + static web player (`index.html`).

## Features

1. **YouTube Discovery (`youtube.py`)**:
   - Monitors subscribed channels (`UC...` IDs, `@handles`, or URLs) via YouTube's public Atom RSS feeds.
   - Searches by custom interest topics via `yt-dlp` (`ytsearchN:<query>`).
   - Filters by lookback window, skips YouTube Shorts and ultra-long livestreams, and deduplicates across runs in SQLite (`data/state.db`).
   - Fetches transcripts via `youtube-transcript-api` (falls back to video description if captions are unavailable).
2. **Highlight Extraction & Chinese Translation (`llm.py`)**:
   - **Default (`provider = "antigravity"`)**: Uses the official [Antigravity Python SDK](https://github.com/google-antigravity/antigravity-sdk-python) (`google-antigravity`) directly in read-only mode.
   - **Fallback (`provider = "staged"`)**: Writes a ready-to-run prompt markdown file under `data/staged/` so you can run it in any Antigravity chat and resume with `yt-podcast run --from-script <file>`.
   - **Optional (`provider = "gemini"`)**: Calls the Gemini API directly via `GEMINI_API_KEY`.
   - Two-pass workflow: extracts structured high-signal takeaways per video, then synthesizes a natural, conversational ~3,600-character Chinese script (~15 minutes at 240 chars/min) without markdown symbols or raw URLs.
3. **Free Natural Chinese Audio (`tts.py`)**:
   - Uses `edge-tts` (default voice `zh-CN-YunxiNeural`, single-host narration; free, no API key).
   - Splits long scripts on Chinese/English sentence boundaries, synthesizes chunks, strips ID3v2/ID3v1 tags, and concatenates raw MPEG frames cleanly in pure Python (no `ffmpeg` required).
4. **Podcast RSS & Hosting (`rss.py`, `publisher.py`)**:
   - Generates RSS 2.0 + `itunes` + `atom` XML (`feed.xml`) and a responsive HTML5 player (`index.html`).
   - Supports `local`, `github_pages` (`git add/commit/push`), and `s3` (Cloudflare R2, AWS S3, Backblaze B2).

---

## Setup

### 1. Create a virtual environment & install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Optional extras:

```bash
# If using Gemini API or S3/Cloudflare R2 hosting:
pip install -e ".[all]"
```

> **Note on `google-antigravity`**: The Antigravity Python SDK (`pip install google-antigravity`) includes the compiled Antigravity runtime binary inside its PyPI wheel and uses your Antigravity model session directly without needing a separate API key.

### 2. Configure `config.toml`

Edit [config.toml](config.toml) (or copy from [config.example.toml](config.example.toml)):

- `[discovery]`: Add your `channels` (`@handle`, `UC...`, or URL) and `interests` search queries.
- `[llm]`: Keep `provider = "antigravity"` (or set `"staged"` / `"gemini"`), adjust `target_minutes = 15`, `language = "zh-CN"`, and `style_notes`.
- `[tts]`: Pick your favorite Mandarin voice (list options with `yt-podcast voices --locale zh`).
- `[podcast]`: Set your podcast title, author, email, and public `base_url`.
- `[publishing]`:
  - `backend = "local"` — writes files under `docs/` for preview.
  - `backend = "github_pages"` — commits (and optionally pushes with `auto_push = true`) `docs/` to GitHub Pages.
  - `backend = "s3"` — uploads MP3 + `feed.xml` + `index.html` to Cloudflare R2 / S3 (configure credentials in `.env` from `.env.example`).

---

## Usage

```bash
# Preview discovered YouTube videos & transcripts without spending LLM/TTS calls
yt-podcast discover --config config.toml

# Full pipeline run (discover -> highlights & Chinese translation -> MP3 -> RSS & publish)
yt-podcast run --config config.toml

# Dry run (discover only via pipeline)
yt-podcast run --dry-run

# Resume from a pre-written or staged script file
yt-podcast run --from-script data/staged/script_20261122.txt

# List Chinese Edge TTS voices
yt-podcast voices --locale zh

# Regenerate feed.xml and index.html from existing episodes in data/state.db
yt-podcast rebuild-feed
```

---

## Hosting Your Podcast RSS Feed

### Option A: GitHub Pages (Free, zero extra accounts)

1. Push this repo to GitHub.
2. In your GitHub repository **Settings → Pages**, set Source to **Deploy from a branch**, branch `main`, folder `/docs`.
3. Update `config.toml`:
   ```toml
   [podcast]
   base_url = "https://<your-username>.github.io/<your-repo>"

   [publishing]
   backend = "github_pages"
   output_dir = "docs"
   auto_push = true
   ```
4. Subscribe in **Apple Podcasts**, **Overcast**, **Pocket Casts**, or **Castro** using:
   `https://<your-username>.github.io/<your-repo>/feed.xml`

### Option B: Cloudflare R2 / S3-Compatible Storage

1. Create a public R2 bucket and enable its `r2.dev` public URL or custom domain.
2. Copy `.env.example` to `.env` and fill in `S3_BUCKET`, `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`.
3. Set `podcast.base_url = "https://pub-xxxx.r2.dev"` and `publishing.backend = "s3"` in `config.toml`.

---

## Scheduling Daily Runs on macOS (`launchd`)

Generate a macOS `launchd` plist and runner script (for example, daily at 08:00 AM):

```bash
yt-podcast install-schedule --hour 8 --minute 0
```

Then follow the printed `launchctl` commands to load `~/Library/LaunchAgents/com.ytpodcast.digest.plist`.

Alternatively, add a `cron` job (`crontab -e`):

```cron
0 8 * * * /Users/chenhuashen/Documents/agy/scripts/run_digest.sh >> /Users/chenhuashen/Documents/agy/logs/cron.log 2>&1
```
