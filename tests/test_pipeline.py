from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from yt_podcast.config import (
    AppConfig,
    DiscoveryConfig,
    LLMConfig,
    PathsConfig,
    PodcastConfig,
    PublishingConfig,
    TTSConfig,
)
from yt_podcast.pipeline import rebuild_feed, run_pipeline
from yt_podcast.state import StateStore
from yt_podcast.youtube import DiscoveredVideo


def _synthetic_frame() -> bytes:
    header = bytes([0xFF, 0xFB, 0x90, 0x00])
    return header + (b"\x00" * 413)


class PipelineFakeLLM:
    def generate(self, system_prompt: str, user_prompt: str) -> str:
        if "Analyze this video" in user_prompt:
            return "- 核心观点：分布式推理调度显著降低首字延迟。"
        return (
            "===EPISODE_TITLE===\n"
            "本周 AI 工程热点速览\n"
            "===SHOW_NOTES===\n"
            "整理自订阅频道的最新工程分享。\n"
            "===SPOKEN_SCRIPT===\n"
            "大家好，欢迎收听今天的科技精选播客。今天我们聊一聊大模型分布式推理的最新实践。"
        )


class PipelineTests(unittest.TestCase):
    def _make_config(self, root: Path) -> AppConfig:
        return AppConfig(
            project_root=root,
            config_path=root / "config.toml",
            discovery=DiscoveryConfig(channels=["@Demo"], interests=[]),
            llm=LLMConfig(provider="antigravity", target_minutes=15),
            tts=TTSConfig(max_chunk_chars=500),
            podcast=PodcastConfig(
                title="Test Podcast",
                base_url="https://example.github.io/pod",
            ),
            publishing=PublishingConfig(backend="local", output_dir=root / "docs"),
            paths=PathsConfig(
                state_db=root / "data/state.db",
                staged_dir=root / "data/staged",
                work_dir=root / "data/work",
            ),
        )

    def test_end_to_end_run_dry_run_and_from_script(self) -> None:
        frame = _synthetic_frame()

        async def fake_synth(text: str, voice: str, rate: str, pitch: str) -> bytes:
            return frame * 100

        video = DiscoveredVideo(
            video_id="vid_e2e_1",
            title="Distributed Inference",
            channel_name="Systems Channel",
            channel_id="UC_SYS",
            url="https://www.youtube.com/watch?v=vid_e2e_1",
            published_at=datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc),
            duration_seconds=600,
            transcript="We discuss distributed prefill-decode disaggregation.",
            transcript_language="en",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._make_config(root)

            # 1. Dry run does not mark seen or create RSS
            dry = run_pipeline(cfg, dry_run=True, videos_override=[video])
            self.assertEqual(dry.status, "dry_run")
            self.assertEqual(len(dry.videos), 1)
            self.assertFalse((root / "docs/feed.xml").exists())

            # 2. Full run publishes MP3, feed.xml, index.html, and updates state.db
            res = run_pipeline(
                cfg,
                llm_client=PipelineFakeLLM(),
                chunk_synthesizer=fake_synth,
                videos_override=[video],
            )
            self.assertEqual(res.status, "published")
            assert res.episode is not None
            self.assertTrue((root / "docs/feed.xml").is_file())
            self.assertTrue((root / "docs/index.html").is_file())
            self.assertTrue((root / "docs/episodes" / res.episode.audio_filename).is_file())
            self.assertIn("本周 AI 工程热点速览", (root / "docs/feed.xml").read_text("utf-8"))

            with StateStore(cfg.paths.state_db) as state:
                self.assertTrue(state.is_video_seen("vid_e2e_1"))
                self.assertEqual(len(state.list_episodes()), 1)

            # 3. Resume from pre-written script file
            custom_script = root / "manual_script.txt"
            custom_script.write_text(
                "===EPISODE_TITLE===\n手动脚本一期\n===SHOW_NOTES===\n说明\n===SPOKEN_SCRIPT===\n这是手动脚本内容。",
                encoding="utf-8",
            )
            res2 = run_pipeline(
                cfg,
                from_script=custom_script,
                chunk_synthesizer=fake_synth,
            )
            self.assertEqual(res2.status, "published")
            with StateStore(cfg.paths.state_db) as state:
                self.assertEqual(len(state.list_episodes()), 2)

            # 4. Rebuild feed with updated base_url refreshes enclosure URLs
            cfg.podcast.base_url = "https://waashen.github.io/yt-podcast"
            rebuilt = rebuild_feed(cfg)
            self.assertTrue(rebuilt.feed_url.endswith("/feed.xml"))
            feed_text = (root / "docs/feed.xml").read_text("utf-8")
            self.assertIn("手动脚本一期", feed_text)
            self.assertIn("本周 AI 工程热点速览", feed_text)
            self.assertIn(
                f'url="https://waashen.github.io/yt-podcast/episodes/{res.episode.audio_filename}"',
                feed_text,
            )

    def test_staged_mode_writes_metadata_and_resumes_seen_state(self) -> None:
        frame = _synthetic_frame()

        async def fake_synth(text: str, voice: str, rate: str, pitch: str) -> bytes:
            return frame * 80

        video = DiscoveredVideo(
            video_id="vid_staged_42",
            title="Agent Automation Patterns",
            channel_name="Dev Channel",
            channel_id="UC_DEV",
            url="https://www.youtube.com/watch?v=vid_staged_42",
            published_at=datetime(2026, 11, 22, 14, 0, tzinfo=timezone.utc),
            duration_seconds=550,
            transcript="Discussion of scheduled agents.",
            transcript_language="en",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._make_config(root)
            cfg.llm.provider = "staged"

            staged = run_pipeline(cfg, videos_override=[video])
            self.assertEqual(staged.status, "staged")
            assert staged.staged_prompt_path is not None
            self.assertTrue(staged.staged_prompt_path.is_file())

            stamp = staged.staged_prompt_path.stem.removeprefix("prompt_")
            meta_file = staged.staged_prompt_path.parent / f"prompt_{stamp}.meta.json"
            self.assertTrue(meta_file.is_file())

            script_file = staged.staged_prompt_path.parent / f"script_{stamp}.txt"
            script_file.write_text(
                "===EPISODE_TITLE===\n自动化播客一期\n"
                "===SHOW_NOTES===\n本期介绍自动化。\n"
                "===SPOKEN_SCRIPT===\n大家好，今天聊聊自动化智能体。",
                encoding="utf-8",
            )

            resumed = run_pipeline(
                cfg,
                from_script=script_file,
                chunk_synthesizer=fake_synth,
            )
            self.assertEqual(resumed.status, "published")
            assert resumed.episode is not None
            self.assertEqual(resumed.episode.video_ids, ["vid_staged_42"])
            self.assertEqual(len(resumed.episode.source_links), 1)
            self.assertEqual(
                resumed.episode.source_links[0]["url"],
                "https://www.youtube.com/watch?v=vid_staged_42",
            )
            with StateStore(cfg.paths.state_db) as state:
                self.assertTrue(state.is_video_seen("vid_staged_42"))


if __name__ == "__main__":
    unittest.main()
