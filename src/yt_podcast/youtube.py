"""YouTube channel RSS monitoring, interest search, filtering, and transcript extraction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import re
import ssl
from typing import Any, Callable
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from yt_podcast.config import DiscoveryConfig
from yt_podcast.state import StateStore

logger = logging.getLogger(__name__)

ATOM_NS = "http://www.w3.org/2005/Atom"
YT_NS = "http://www.youtube.com/xml/schemas/2015"
MEDIA_NS = "http://search.yahoo.com/mrss/"

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_CHANNEL_ID_RE = re.compile(r"^UC[0-9A-Za-z_-]{20,}$")
_URL_CHANNEL_RE = re.compile(r"youtube\.com/channel/(UC[0-9A-Za-z_-]{20,})")
_HTML_CHANNEL_PATTERNS = (
    re.compile(r'"externalId"\s*:\s*"(UC[0-9A-Za-z_-]{20,})"'),
    re.compile(r'"channelId"\s*:\s*"(UC[0-9A-Za-z_-]{20,})"'),
    re.compile(r'itemprop="identifier"\s+content="(UC[0-9A-Za-z_-]{20,})"'),
    re.compile(r"youtube\.com/feeds/videos\.xml\?channel_id=(UC[0-9A-Za-z_-]{20,})"),
)


@dataclass(slots=True)
class DiscoveredVideo:
    video_id: str
    title: str
    channel_name: str
    channel_id: str
    url: str
    published_at: datetime
    duration_seconds: int | None = None
    source_type: str = "channel"
    source_query: str = ""
    description: str = ""
    transcript: str = ""
    transcript_language: str = ""

    @property
    def is_short_url(self) -> bool:
        return "/shorts/" in self.url


def _build_ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def default_http_get(url: str, timeout: int = 20) -> str:
    """Fetch URL content as UTF-8 text using urllib."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    ctx = _build_ssl_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def extract_channel_id_from_html(html: str) -> str | None:
    """Extract a UC... YouTube channel ID from channel page HTML."""
    for pattern in _HTML_CHANNEL_PATTERNS:
        match = pattern.search(html)
        if match:
            return match.group(1)
    return None


def _normalize_channel_url(channel_input: str) -> str:
    text = channel_input.strip()
    if text.startswith("http://") or text.startswith("https://"):
        return text
    if text.startswith("@"):
        return f"https://www.youtube.com/{text}"
    return f"https://www.youtube.com/@{text}"


def resolve_channel_id(
    channel_input: str,
    state: StateStore | None = None,
    http_get: Callable[[str], str] | None = None,
) -> str:
    """Resolve a UC... channel ID, @handle, or channel URL to a canonical UC... ID."""
    cleaned = channel_input.strip()
    if _CHANNEL_ID_RE.match(cleaned):
        return cleaned

    url_match = _URL_CHANNEL_RE.search(cleaned)
    if url_match:
        return url_match.group(1)

    if state is not None:
        cached = state.get_cached_channel(cleaned)
        if cached is not None:
            return cached[0]

    fetcher = http_get or default_http_get
    target_url = _normalize_channel_url(cleaned)
    channel_id: str | None = None

    try:
        html = fetcher(target_url)
        channel_id = extract_channel_id_from_html(html)
    except Exception as exc:
        logger.debug("HTML channel lookup failed for %s: %s", cleaned, exc)

    if channel_id is None:
        channel_id = _resolve_channel_id_via_ytdlp(target_url)

    if not channel_id:
        raise ValueError(
            f"Could not resolve YouTube channel ID for '{channel_input}'. "
            "Try providing the UC... channel ID directly."
        )

    if state is not None:
        state.cache_channel(cleaned, channel_id)
    return channel_id


def _resolve_channel_id_via_ytdlp(url: str) -> str | None:
    try:
        import yt_dlp
    except ImportError:
        return None

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlistend": 1,
        "ignoreerrors": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if isinstance(info, dict):
            cid = info.get("channel_id") or info.get("uploader_id")
            if isinstance(cid, str) and _CHANNEL_ID_RE.match(cid):
                return cid
    except Exception as exc:
        logger.debug("yt-dlp channel resolution failed for %s: %s", url, exc)
    return None


def _parse_iso_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_channel_feed_xml(
    xml_text: str, source_query: str = ""
) -> list[DiscoveredVideo]:
    """Parse a YouTube Atom RSS feed XML document into DiscoveredVideo entries."""
    root = ET.fromstring(xml_text)
    channel_title = (root.findtext(f"{{{ATOM_NS}}}title") or "").strip()
    channel_id = (root.findtext(f"{{{YT_NS}}}channelId") or "").strip()

    videos: list[DiscoveredVideo] = []
    for entry in root.findall(f"{{{ATOM_NS}}}entry"):
        video_id = (entry.findtext(f"{{{YT_NS}}}videoId") or "").strip()
        title = (entry.findtext(f"{{{ATOM_NS}}}title") or "").strip()
        published_raw = (entry.findtext(f"{{{ATOM_NS}}}published") or "").strip()
        entry_channel_id = (
            entry.findtext(f"{{{YT_NS}}}channelId") or channel_id
        ).strip()
        author_name = (
            entry.findtext(f"{{{ATOM_NS}}}author/{{{ATOM_NS}}}name") or channel_title
        ).strip()

        link_el = entry.find(f"{{{ATOM_NS}}}link[@rel='alternate']")
        url = (
            link_el.attrib.get("href", "")
            if link_el is not None
            else f"https://www.youtube.com/watch?v={video_id}"
        )

        media_group = entry.find(f"{{{MEDIA_NS}}}group")
        description = ""
        if media_group is not None:
            description = (
                media_group.findtext(f"{{{MEDIA_NS}}}description") or ""
            ).strip()

        if not video_id or not published_raw:
            continue

        videos.append(
            DiscoveredVideo(
                video_id=video_id,
                title=title or video_id,
                channel_name=author_name or "Unknown Channel",
                channel_id=entry_channel_id,
                url=url or f"https://www.youtube.com/watch?v={video_id}",
                published_at=_parse_iso_datetime(published_raw),
                duration_seconds=None,
                source_type="channel",
                source_query=source_query or entry_channel_id,
                description=description,
            )
        )
    return videos


def fetch_channel_videos(
    channel_input: str,
    max_results: int = 3,
    state: StateStore | None = None,
    http_get: Callable[[str], str] | None = None,
) -> list[DiscoveredVideo]:
    """Fetch recent videos from a YouTube channel's public Atom RSS feed."""
    channel_id = resolve_channel_id(channel_input, state=state, http_get=http_get)
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    fetcher = http_get or default_http_get
    xml_text = fetcher(feed_url)
    videos = parse_channel_feed_xml(xml_text, source_query=channel_input)
    if state is not None and videos:
        state.cache_channel(channel_input, channel_id, videos[0].channel_name)
    return videos[:max_results]


def _parse_ytdlp_entry(
    entry: dict[str, Any],
    source_type: str,
    source_query: str,
    now: datetime | None = None,
) -> DiscoveredVideo | None:
    video_id = entry.get("id")
    if not video_id or not isinstance(video_id, str):
        return None

    published_at: datetime | None = None
    ts = entry.get("timestamp") or entry.get("release_timestamp")
    if isinstance(ts, (int, float)):
        published_at = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    else:
        upload_date = entry.get("upload_date")
        if isinstance(upload_date, str) and len(upload_date) == 8:
            try:
                published_at = datetime.strptime(upload_date, "%Y%m%d").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                published_at = None

    if published_at is None:
        published_at = now or datetime.now(timezone.utc)

    duration = entry.get("duration")
    duration_seconds = int(duration) if isinstance(duration, (int, float)) else None
    url = str(entry.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}")
    channel_name = str(entry.get("channel") or entry.get("uploader") or "Unknown Channel")
    channel_id = str(entry.get("channel_id") or "")
    description = str(entry.get("description") or "").strip()
    title = str(entry.get("title") or video_id).strip()

    return DiscoveredVideo(
        video_id=video_id,
        title=title,
        channel_name=channel_name,
        channel_id=channel_id,
        url=url,
        published_at=published_at,
        duration_seconds=duration_seconds,
        source_type=source_type,
        source_query=source_query,
        description=description,
    )


def search_videos_by_interest(query: str, max_results: int = 5) -> list[DiscoveredVideo]:
    """Search YouTube videos by topic/interest using yt-dlp."""
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError(
            "yt-dlp is required for interest search. Install it with: pip install yt-dlp"
        ) from exc

    search_expr = f"ytsearch{max_results}:{query}"
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        result = ydl.extract_info(search_expr, download=False)

    if not isinstance(result, dict):
        return []
    entries = result.get("entries") or []
    videos: list[DiscoveredVideo] = []
    for entry in entries:
        if isinstance(entry, dict):
            parsed = _parse_ytdlp_entry(
                entry, source_type="interest", source_query=query
            )
            if parsed is not None:
                videos.append(parsed)
    return videos


def enrich_video_duration(video: DiscoveredVideo) -> None:
    """Populate duration_seconds (and missing description) via yt-dlp if installed."""
    if video.duration_seconds is not None:
        return
    try:
        import yt_dlp
    except ImportError:
        return

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video.url, download=False)
        if isinstance(info, dict):
            dur = info.get("duration")
            if isinstance(dur, (int, float)):
                video.duration_seconds = int(dur)
            if not video.description and info.get("description"):
                video.description = str(info["description"]).strip()
    except Exception as exc:
        logger.debug("Could not enrich metadata for %s: %s", video.video_id, exc)


def filter_videos(
    videos: list[DiscoveredVideo],
    state: StateStore | None,
    lookback_hours: int,
    min_duration_seconds: int,
    max_duration_seconds: int,
    max_videos_total: int,
    now: datetime | None = None,
) -> list[DiscoveredVideo]:
    """Deduplicate and filter videos by seen state, recency, and duration."""
    reference_now = now or datetime.now(timezone.utc)
    cutoff = reference_now - timedelta(hours=lookback_hours)

    deduped: dict[str, DiscoveredVideo] = {}
    for video in videos:
        existing = deduped.get(video.video_id)
        if existing is None or (
            existing.source_type != "channel" and video.source_type == "channel"
        ):
            deduped[video.video_id] = video

    filtered: list[DiscoveredVideo] = []
    for video in deduped.values():
        if state is not None and state.is_video_seen(video.video_id):
            continue
        if video.published_at < cutoff:
            continue
        if video.is_short_url:
            continue
        if video.duration_seconds is not None:
            if video.duration_seconds < min_duration_seconds:
                continue
            if video.duration_seconds > max_duration_seconds:
                continue
        filtered.append(video)

    filtered.sort(
        key=lambda v: (0 if v.source_type == "channel" else 1, -v.published_at.timestamp())
    )
    return filtered[:max_videos_total]


def _extract_transcript_text(raw_transcript: Any) -> tuple[str, str]:
    language = str(getattr(raw_transcript, "language_code", "") or "")
    lines: list[str] = []
    for item in raw_transcript:
        if isinstance(item, dict):
            text = str(item.get("text", "")).strip()
        else:
            text = str(getattr(item, "text", "")).strip()
        if text:
            lines.append(text)
    joined = re.sub(r"\s+", " ", " ".join(lines)).strip()
    return joined, language


def fetch_transcript(
    video: DiscoveredVideo,
    languages: list[str],
    max_chars: int = 12000,
) -> tuple[str, str]:
    """Fetch video transcript via youtube-transcript-api, falling back to description."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        logger.warning(
            "youtube-transcript-api is not installed; falling back to video description."
        )
        return video.description[:max_chars], "description"

    try:
        if hasattr(YouTubeTranscriptApi, "get_transcript"):
            raw = YouTubeTranscriptApi.get_transcript(
                video.video_id, languages=languages
            )
        else:
            api = YouTubeTranscriptApi()
            raw = api.fetch(video.video_id, languages=languages)
        text, lang = _extract_transcript_text(raw)
        if text:
            return text[:max_chars], lang or (languages[0] if languages else "unknown")
    except Exception as exc:
        logger.info(
            "No transcript available for %s (%s); using description fallback.",
            video.video_id,
            exc,
        )

    fallback = video.description[:max_chars] if video.description else video.title
    return fallback, "description"


def discover_and_prepare_videos(
    config: DiscoveryConfig,
    state: StateStore | None = None,
    enrich_channel_durations: bool = True,
) -> list[DiscoveredVideo]:
    """Run channel RSS + interest search discovery, filter, and attach transcripts."""
    collected: list[DiscoveredVideo] = []

    for channel in config.channels:
        try:
            collected.extend(
                fetch_channel_videos(
                    channel,
                    max_results=config.max_results_per_channel,
                    state=state,
                )
            )
        except Exception as exc:
            logger.warning("Failed to fetch channel '%s': %s", channel, exc)

    for interest in config.interests:
        try:
            collected.extend(
                search_videos_by_interest(
                    interest,
                    max_results=config.max_results_per_interest,
                )
            )
        except Exception as exc:
            logger.warning("Failed to search interest '%s': %s", interest, exc)

    # Pre-filter before expensive duration enrichment so seen/old videos are skipped first.
    candidates = filter_videos(
        collected,
        state=state,
        lookback_hours=config.lookback_hours,
        min_duration_seconds=config.min_duration_seconds,
        max_duration_seconds=config.max_duration_seconds,
        max_videos_total=max(config.max_videos_total * 2, config.max_videos_total),
    )

    if enrich_channel_durations:
        for video in candidates:
            if video.duration_seconds is None:
                enrich_video_duration(video)
        candidates = filter_videos(
            candidates,
            state=state,
            lookback_hours=config.lookback_hours,
            min_duration_seconds=config.min_duration_seconds,
            max_duration_seconds=config.max_duration_seconds,
            max_videos_total=config.max_videos_total,
        )
    else:
        candidates = candidates[: config.max_videos_total]

    for video in candidates:
        transcript, lang = fetch_transcript(
            video,
            languages=config.transcript_languages,
            max_chars=config.max_transcript_chars,
        )
        video.transcript = transcript
        video.transcript_language = lang

    return candidates
