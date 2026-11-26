from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from yt_podcast.config import LLMConfig
from yt_podcast.llm import (
    StagedPromptReady,
    clean_spoken_script,
    generate_digest,
    parse_digest_response,
)
from yt_podcast.youtube import DiscoveredVideo


class FakeLLMClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if len(self.calls) == 1:
            return "- 要点一：KV Cache 显存压缩提升 3 倍吞吐。"
        return (
            "===EPISODE_TITLE===\n"
            "AI 推理优化新突破\n"
            "===SHOW_NOTES===\n"
            "本期聚焦大模型推理优化。\n"
            "===SPOKEN_SCRIPT===\n"
            "## 开场\n"
            "大家好，欢迎收听今天的**科技播客**！详情见 https://example.com 。"
        )


def _sample_video() -> DiscoveredVideo:
    return DiscoveredVideo(
        video_id="vid1",
        title="Fast LLM Serving",
        channel_name="AI Systems",
        channel_id="UC123",
        url="https://www.youtube.com/watch?v=vid1",
        published_at=datetime(2026, 11, 22, 9, 0, tzinfo=timezone.utc),
        duration_seconds=800,
        transcript="Today we look at quantized KV caches for 3x throughput.",
        transcript_language="en",
    )


class LLMTests(unittest.TestCase):
    def test_clean_spoken_script_strips_markdown_and_urls(self) -> None:
        raw = "# Title\n- **重要**：点击[这里](https://example.com)查看 `code` 和 https://foo.bar"
        cleaned = clean_spoken_script(raw)
        self.assertNotIn("#", cleaned)
        self.assertNotIn("**", cleaned)
        self.assertNotIn("https://", cleaned)
        self.assertNotIn("`", cleaned)
        self.assertIn("这里", cleaned)

    def test_parse_digest_response_tagged_and_fallback(self) -> None:
        v = _sample_video()
        res = parse_digest_response(
            "===EPISODE_TITLE===\n中文标题\n===SHOW_NOTES===\n简介\n===SPOKEN_SCRIPT===\n大家好。",
            videos=[v],
        )
        self.assertEqual(res.episode_title, "中文标题")
        self.assertEqual(res.episode_summary, "简介")
        self.assertEqual(res.spoken_script, "大家好。")

        fallback = parse_digest_response("大家好，这是直接输出的纯口播稿。", videos=[v])
        self.assertEqual(fallback.spoken_script, "大家好，这是直接输出的纯口播稿。")
        self.assertIn("Fast LLM Serving", fallback.episode_summary)

    def test_generate_digest_two_pass_and_staged(self) -> None:
        v = _sample_video()
        with tempfile.TemporaryDirectory() as tmp:
            staged_dir = Path(tmp) / "staged"
            client = FakeLLMClient()
            cfg = LLMConfig(provider="antigravity", target_minutes=15, chars_per_minute=240)
            result = generate_digest([v], cfg, staged_dir=staged_dir, client=client)

            self.assertEqual(len(client.calls), 2)
            self.assertIn("3600", client.calls[1][1])
            self.assertEqual(result.episode_title, "AI 推理优化新突破")
            self.assertNotIn("**", result.spoken_script)
            self.assertNotIn("https://", result.spoken_script)

            staged_cfg = LLMConfig(provider="staged")
            with self.assertRaises(StagedPromptReady) as ctx:
                generate_digest([v], staged_cfg, staged_dir=staged_dir, client=None)
            self.assertTrue(ctx.exception.prompt_path.is_file())
            prompt_content = ctx.exception.prompt_path.read_text(encoding="utf-8")
            self.assertIn("Fast LLM Serving", prompt_content)


if __name__ == "__main__":
    unittest.main()
