from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from yt_podcast.state import EpisodeRecord, StateStore


class StateStoreTests(unittest.TestCase):
    def test_seen_videos_and_channel_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with StateStore(Path(tmp) / "state.db") as store:
                self.assertFalse(store.is_video_seen("vid1"))
                store.mark_videos_seen(
                    [
                        {
                            "video_id": "vid1",
                            "title": "Test Video",
                            "channel_name": "Chan",
                            "url": "https://www.youtube.com/watch?v=vid1",
                            "published_at": "2026-11-20T00:00:00+00:00",
                        }
                    ]
                )
                self.assertTrue(store.is_video_seen("vid1"))
                self.assertFalse(store.is_video_seen("vid2"))

                store.cache_channel("@TwoMinutePapers", "UCbfYPyITQ-7l4upoX8nvctg", "Two Minute Papers")
                cached = store.get_cached_channel("@TwoMinutePapers")
                self.assertEqual(
                    cached, ("UCbfYPyITQ-7l4upoX8nvctg", "Two Minute Papers")
                )

    def test_episode_recording_and_ordering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with StateStore(Path(tmp) / "state.db") as store:
                t1 = datetime(2026, 11, 20, 8, 0, tzinfo=timezone.utc)
                t2 = t1 + timedelta(days=1)

                ep1 = EpisodeRecord(
                    guid="ep-1",
                    title="Episode 1",
                    summary="Summary 1",
                    audio_filename="ep1.mp3",
                    audio_url="https://example.com/episodes/ep1.mp3",
                    byte_size=1024,
                    duration_seconds=900,
                    published_at=t1,
                    video_ids=["v1"],
                    source_links=[{"title": "V1", "channel_name": "C1", "url": "https://youtu.be/v1"}],
                )
                ep2 = EpisodeRecord(
                    guid="ep-2",
                    title="Episode 2",
                    summary="Summary 2",
                    audio_filename="ep2.mp3",
                    audio_url="https://example.com/episodes/ep2.mp3",
                    byte_size=2048,
                    duration_seconds=910,
                    published_at=t2,
                    video_ids=["v2", "v3"],
                    source_links=[],
                )
                store.record_episode(ep1)
                store.record_episode(ep2)

                listed = store.list_episodes()
                self.assertEqual([e.guid for e in listed], ["ep-2", "ep-1"])
                self.assertEqual(listed[1].source_links[0]["url"], "https://youtu.be/v1")


if __name__ == "__main__":
    unittest.main()
