from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from yt_podcast.state import StateStore
from yt_podcast.youtube import (
    DiscoveredVideo,
    _parse_ytdlp_entry,
    extract_channel_id_from_html,
    fetch_transcript,
    filter_videos,
    parse_channel_feed_xml,
    resolve_channel_id,
)

SAMPLE_ATOM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
  <yt:channelId>UCbfYPyITQ-7l4upoX8nvctg</yt:channelId>
  <title>Two Minute Papers</title>
  <entry>
    <id>yt:video:abc123XYZ</id>
    <yt:videoId>abc123XYZ</yt:videoId>
    <yt:channelId>UCbfYPyITQ-7l4upoX8nvctg</yt:channelId>
    <title>New AI Paper Breakthrough</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=abc123XYZ"/>
    <author><name>Two Minute Papers</name></author>
    <published>2026-11-22T10:00:00+00:00</published>
    <media:group>
      <media:description>A concise look at the new paper.</media:description>
    </media:group>
  </entry>
</feed>
"""


class YouTubeTests(unittest.TestCase):
    def test_parse_channel_feed_xml(self) -> None:
        videos = parse_channel_feed_xml(SAMPLE_ATOM_XML, source_query="@TwoMinutePapers")
        self.assertEqual(len(videos), 1)
        v = videos[0]
        self.assertEqual(v.video_id, "abc123XYZ")
        self.assertEqual(v.title, "New AI Paper Breakthrough")
        self.assertEqual(v.channel_name, "Two Minute Papers")
        self.assertEqual(v.channel_id, "UCbfYPyITQ-7l4upoX8nvctg")
        self.assertEqual(v.description, "A concise look at the new paper.")
        self.assertEqual(v.source_type, "channel")

    def test_resolve_channel_id_uses_html_and_caches(self) -> None:
        calls: list[str] = []

        def fake_http_get(url: str) -> str:
            calls.append(url)
            return '<html><script>{"externalId":"UCxxxxxxxxxxxxxxxxxxxxxx"}</script></html>'

        with tempfile.TemporaryDirectory() as tmp:
            with StateStore(Path(tmp) / "state.db") as state:
                cid1 = resolve_channel_id("@SomeHandle", state=state, http_get=fake_http_get)
                cid2 = resolve_channel_id("@SomeHandle", state=state, http_get=fake_http_get)
                self.assertEqual(cid1, "UCxxxxxxxxxxxxxxxxxxxxxx")
                self.assertEqual(cid2, "UCxxxxxxxxxxxxxxxxxxxxxx")
                self.assertEqual(len(calls), 1)

    def test_extract_channel_id_patterns(self) -> None:
        self.assertEqual(
            extract_channel_id_from_html('<meta itemprop="identifier" content="UC1234567890123456789012">'),
            "UC1234567890123456789012",
        )
        self.assertEqual(
            resolve_channel_id("https://www.youtube.com/channel/UC1234567890123456789012"),
            "UC1234567890123456789012",
        )

    def test_filter_videos_dedup_recency_duration_and_seen(self) -> None:
        now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            with StateStore(Path(tmp) / "state.db") as state:
                state.mark_videos_seen([{"video_id": "seen1"}])

                candidates = [
                    DiscoveredVideo(
                        video_id="seen1",
                        title="Already Seen",
                        channel_name="C",
                        channel_id="UC1",
                        url="https://www.youtube.com/watch?v=seen1",
                        published_at=now - timedelta(hours=2),
                        duration_seconds=600,
                    ),
                    DiscoveredVideo(
                        video_id="old1",
                        title="Too Old",
                        channel_name="C",
                        channel_id="UC1",
                        url="https://www.youtube.com/watch?v=old1",
                        published_at=now - timedelta(hours=100),
                        duration_seconds=600,
                    ),
                    DiscoveredVideo(
                        video_id="short1",
                        title="Short",
                        channel_name="C",
                        channel_id="UC1",
                        url="https://www.youtube.com/watch?v=short1",
                        published_at=now - timedelta(hours=3),
                        duration_seconds=45,
                    ),
                    DiscoveredVideo(
                        video_id="dup1",
                        title="Dup Interest",
                        channel_name="C",
                        channel_id="UC1",
                        url="https://www.youtube.com/watch?v=dup1",
                        published_at=now - timedelta(hours=4),
                        duration_seconds=500,
                        source_type="interest",
                    ),
                    DiscoveredVideo(
                        video_id="dup1",
                        title="Dup Channel",
                        channel_name="C",
                        channel_id="UC1",
                        url="https://www.youtube.com/watch?v=dup1",
                        published_at=now - timedelta(hours=4),
                        duration_seconds=500,
                        source_type="channel",
                    ),
                    DiscoveredVideo(
                        video_id="ok_interest",
                        title="Good Interest",
                        channel_name="C2",
                        channel_id="UC2",
                        url="https://www.youtube.com/watch?v=ok_interest",
                        published_at=now - timedelta(hours=1),
                        duration_seconds=900,
                        source_type="interest",
                    ),
                ]

                kept = filter_videos(
                    candidates,
                    state=state,
                    lookback_hours=72,
                    min_duration_seconds=180,
                    max_duration_seconds=7200,
                    max_videos_total=5,
                    now=now,
                )
                self.assertEqual([v.video_id for v in kept], ["dup1", "ok_interest"])
                self.assertEqual(kept[0].source_type, "channel")

    def test_parse_ytdlp_entry_and_description_fallback(self) -> None:
        entry = {
            "id": "vid999",
            "title": "Deep Dive into KV Cache",
            "channel": "Systems Lab",
            "channel_id": "UC999",
            "duration": 1200,
            "upload_date": "20261121",
            "webpage_url": "https://www.youtube.com/watch?v=vid999",
            "description": "KV cache optimization walkthrough.",
        }
        v = _parse_ytdlp_entry(entry, source_type="interest", source_query="LLM inference")
        self.assertIsNotNone(v)
        assert v is not None
        self.assertEqual(v.duration_seconds, 1200)
        self.assertEqual(v.published_at.strftime("%Y-%m-%d"), "2026-11-21")

        text, lang = fetch_transcript(v, languages=["en"], max_chars=500)
        self.assertTrue(len(text) > 0)
        self.assertEqual(lang, "description")


if __name__ == "__main__":
    unittest.main()
