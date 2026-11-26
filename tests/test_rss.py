from __future__ import annotations

from datetime import datetime, timezone
import unittest
import xml.etree.ElementTree as ET

from yt_podcast.config import PodcastConfig
from yt_podcast.rss import (
    ATOM_NS,
    ITUNES_NS,
    build_index_html,
    build_rss_feed,
    format_duration_hms,
)
from yt_podcast.state import EpisodeRecord


class RSSTests(unittest.TestCase):
    def test_format_duration_hms(self) -> None:
        self.assertEqual(format_duration_hms(0), "00:00:00")
        self.assertEqual(format_duration_hms(905), "00:15:05")
        self.assertEqual(format_duration_hms(3661), "01:01:01")

    def test_build_rss_feed_and_index_html(self) -> None:
        podcast = PodcastConfig(
            title="AI 中文精选",
            subtitle="每日科技摘要",
            description="追踪最新 AI 视频并总结为中文播客。",
            author="Host",
            email="host@example.com",
            base_url="https://demo.github.io/pod",
            image_url="https://demo.github.io/pod/cover.jpg",
        )
        ep = EpisodeRecord(
            guid="ep-101",
            title="第一期：大模型架构演进 & <测试>",
            summary="介绍最新的推理技术。",
            audio_filename="ep101.mp3",
            audio_url="https://demo.github.io/pod/episodes/ep101.mp3",
            byte_size=543210,
            duration_seconds=905,
            published_at=datetime(2026, 11, 22, 8, 0, tzinfo=timezone.utc),
            video_ids=["v1"],
            source_links=[
                {
                    "title": "Inference Deep Dive",
                    "channel_name": "Tech Lab",
                    "url": "https://www.youtube.com/watch?v=v1",
                }
            ],
        )

        xml_str = build_rss_feed(podcast, [ep])
        root = ET.fromstring(xml_str)
        self.assertEqual(root.tag, "rss")
        channel = root.find("channel")
        assert channel is not None
        self.assertEqual(channel.findtext("title"), "AI 中文精选")
        atom_link = channel.find(f"{{{ATOM_NS}}}link")
        assert atom_link is not None
        self.assertEqual(atom_link.attrib["href"], "https://demo.github.io/pod/feed.xml")

        item = channel.find("item")
        assert item is not None
        self.assertEqual(item.findtext("title"), "第一期：大模型架构演进 & <测试>")
        self.assertEqual(item.findtext(f"{{{ITUNES_NS}}}duration"), "00:15:05")
        enclosure = item.find("enclosure")
        assert enclosure is not None
        self.assertEqual(
            enclosure.attrib["url"], "https://demo.github.io/pod/episodes/ep101.mp3"
        )
        self.assertEqual(enclosure.attrib["length"], "543210")
        self.assertEqual(enclosure.attrib["type"], "audio/mpeg")

        html_str = build_index_html(podcast, [ep])
        self.assertIn("<audio controls", html_str)
        self.assertIn("episodes/ep101.mp3", html_str)
        self.assertIn("https://www.youtube.com/watch?v=v1", html_str)
        self.assertIn("&lt;测试&gt;", html_str)


if __name__ == "__main__":
    unittest.main()
