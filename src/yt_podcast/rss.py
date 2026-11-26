"""Apple Podcasts & Spotify compatible RSS 2.0 feed and static web player generator."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import format_datetime
import html
import xml.etree.ElementTree as ET

from yt_podcast.config import PodcastConfig
from yt_podcast.state import EpisodeRecord

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"


def format_duration_hms(seconds: int) -> str:
    """Format seconds into HH:MM:SS for <itunes:duration>."""
    safe = max(0, int(seconds))
    hours, rem = divmod(safe, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _format_episode_description(episode: EpisodeRecord) -> str:
    parts = [episode.summary.strip()]
    if episode.source_links:
        parts.append("")
        parts.append("Sources:")
        for link in episode.source_links:
            title = link.get("title", "Video")
            channel = link.get("channel_name", "")
            url = link.get("url", "")
            label = f"{title} ({channel})" if channel else title
            parts.append(f"- {label}: {url}" if url else f"- {label}")
    return "\n".join(parts).strip()


def build_rss_feed(podcast: PodcastConfig, episodes: list[EpisodeRecord]) -> str:
    """Build a valid RSS 2.0 + iTunes podcast XML feed."""
    ET.register_namespace("itunes", ITUNES_NS)
    ET.register_namespace("atom", ATOM_NS)

    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")

    base_url = podcast.base_url.rstrip("/")
    feed_url = f"{base_url}/feed.xml"
    explicit_str = "yes" if podcast.explicit else "no"

    ET.SubElement(channel, "title").text = podcast.title
    ET.SubElement(channel, "link").text = f"{base_url}/"
    ET.SubElement(channel, "description").text = podcast.description
    ET.SubElement(channel, "language").text = podcast.language
    ET.SubElement(channel, "generator").text = "yt-podcast"

    build_dt = episodes[0].published_at if episodes else datetime.now(timezone.utc)
    if build_dt.tzinfo is None:
        build_dt = build_dt.replace(tzinfo=timezone.utc)
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(build_dt)

    ET.SubElement(
        channel,
        f"{{{ATOM_NS}}}link",
        {"href": feed_url, "rel": "self", "type": "application/rss+xml"},
    )

    ET.SubElement(channel, f"{{{ITUNES_NS}}}subtitle").text = podcast.subtitle
    ET.SubElement(channel, f"{{{ITUNES_NS}}}author").text = podcast.author
    ET.SubElement(channel, f"{{{ITUNES_NS}}}summary").text = podcast.description
    ET.SubElement(channel, f"{{{ITUNES_NS}}}explicit").text = explicit_str
    ET.SubElement(
        channel, f"{{{ITUNES_NS}}}category", {"text": podcast.category}
    )

    owner = ET.SubElement(channel, f"{{{ITUNES_NS}}}owner")
    ET.SubElement(owner, f"{{{ITUNES_NS}}}name").text = podcast.author
    ET.SubElement(owner, f"{{{ITUNES_NS}}}email").text = podcast.email

    if podcast.image_url:
        ET.SubElement(
            channel, f"{{{ITUNES_NS}}}image", {"href": podcast.image_url}
        )
        img = ET.SubElement(channel, "image")
        ET.SubElement(img, "url").text = podcast.image_url
        ET.SubElement(img, "title").text = podcast.title
        ET.SubElement(img, "link").text = f"{base_url}/"

    for ep in episodes:
        item = ET.SubElement(channel, "item")
        ep_dt = ep.published_at
        if ep_dt.tzinfo is None:
            ep_dt = ep_dt.replace(tzinfo=timezone.utc)
        desc = _format_episode_description(ep)

        ET.SubElement(item, "title").text = ep.title
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = ep.guid
        ET.SubElement(item, "pubDate").text = format_datetime(ep_dt)
        ET.SubElement(item, "description").text = desc
        ET.SubElement(item, f"{{{ITUNES_NS}}}summary").text = desc
        ET.SubElement(item, f"{{{ITUNES_NS}}}author").text = podcast.author
        ET.SubElement(
            item, f"{{{ITUNES_NS}}}duration"
        ).text = format_duration_hms(ep.duration_seconds)
        ET.SubElement(item, f"{{{ITUNES_NS}}}explicit").text = explicit_str
        ET.SubElement(
            item,
            "enclosure",
            {
                "url": ep.audio_url,
                "length": str(ep.byte_size),
                "type": "audio/mpeg",
            },
        )

    ET.indent(rss, space="  ")
    return ET.tostring(rss, encoding="unicode", xml_declaration=True) + "\n"


def build_index_html(podcast: PodcastConfig, episodes: list[EpisodeRecord]) -> str:
    """Build a static HTML5 podcast page with audio players and RSS subscribe URL."""
    base_url = podcast.base_url.rstrip("/")
    feed_url = f"{base_url}/feed.xml"

    cards: list[str] = []
    for ep in episodes:
        date_str = ep.published_at.strftime("%Y-%m-%d %H:%M UTC")
        duration_str = format_duration_hms(ep.duration_seconds)
        rel_audio = f"episodes/{html.escape(ep.audio_filename, quote=True)}"
        links_html = ""
        if ep.source_links:
            items = []
            for src in ep.source_links:
                t = html.escape(src.get("title", "Video"))
                ch = html.escape(src.get("channel_name", ""))
                u = html.escape(src.get("url", ""), quote=True)
                label = f"{t} ({ch})" if ch else t
                if u:
                    items.append(
                        f'<li><a href="{u}" target="_blank" rel="noopener">{label}</a></li>'
                    )
                else:
                    items.append(f"<li>{label}</li>")
            links_html = f'<ul class="sources">{"".join(items)}</ul>'

        summary_html = html.escape(ep.summary).replace("\n", "<br>")
        cards.append(
            f"""
            <article class="episode">
              <div class="meta">{html.escape(date_str)} · 时长 {html.escape(duration_str)}</div>
              <h2>{html.escape(ep.title)}</h2>
              <audio controls preload="none" src="{rel_audio}"></audio>
              <p class="summary">{summary_html}</p>
              {links_html}
            </article>
            """
        )

    episodes_body = (
        "\n".join(cards)
        if cards
        else '<p class="empty">还没有生成节目。运行 <code>yt-podcast run</code> 开始制作第一期播客。</p>'
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(podcast.title)}</title>
  <link rel="alternate" type="application/rss+xml" title="{html.escape(podcast.title)}" href="feed.xml" />
  <style>
    :root {{ color-scheme: light dark; --bg: #0b0f17; --card: #151b26; --text: #eef2f8; --muted: #9aa7bd; --accent: #4f8cff; --border: #253042; }}
    @media (prefers-color-scheme: light) {{
      :root {{ --bg: #f6f8fc; --card: #ffffff; --text: #111827; --muted: #5b6475; --accent: #2563eb; --border: #e5e7eb; }}
    }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Noto Sans SC", sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 2rem 1rem; line-height: 1.6; }}
    main {{ max-width: 780px; margin: 0 auto; }}
    header {{ margin-bottom: 2rem; border-bottom: 1px solid var(--border); padding-bottom: 1.5rem; }}
    h1 {{ margin: 0 0 0.4rem 0; font-size: 1.9rem; }}
    .subtitle {{ color: var(--muted); margin: 0 0 1rem 0; }}
    .rss-box {{ display: inline-flex; gap: 0.6rem; align-items: center; background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 0.45rem 0.8rem; font-size: 0.9rem; }}
    .rss-box a {{ color: var(--accent); text-decoration: none; font-weight: 600; }}
    .episode {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 1.25rem 1.4rem; margin-bottom: 1.25rem; }}
    .episode h2 {{ margin: 0.35rem 0 0.8rem 0; font-size: 1.2rem; }}
    .meta {{ color: var(--muted); font-size: 0.85rem; }}
    audio {{ width: 100%; margin: 0.4rem 0 0.8rem 0; }}
    .summary {{ margin: 0.5rem 0; font-size: 0.95rem; }}
    .sources {{ margin: 0.6rem 0 0 1.2rem; padding: 0; font-size: 0.9rem; color: var(--muted); }}
    .sources a {{ color: var(--accent); text-decoration: none; }}
    .empty {{ color: var(--muted); }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>{html.escape(podcast.title)}</h1>
      <p class="subtitle">{html.escape(podcast.description)}</p>
      <div class="rss-box">
        <span>RSS 订阅地址:</span>
        <a href="feed.xml">{html.escape(feed_url)}</a>
      </div>
    </header>
    {episodes_body}
  </main>
</body>
</html>
"""
