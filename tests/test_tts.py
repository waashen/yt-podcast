from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import tempfile
import unittest

from yt_podcast.config import TTSConfig
from yt_podcast.tts import (
    concatenate_mp3_bytes,
    estimate_mpeg_duration_from_bytes,
    split_script_into_chunks,
    strip_id3_tags,
    synthesize_chunk_minimax,
    synthesize_chunk_volcengine,
    synthesize_speech,
)


def make_synthetic_mpeg1_frame(bitrate_idx: int = 9, sample_idx: int = 0) -> bytes:
    """Create a valid 417-byte MPEG-1 Layer III 128kbps 44.1kHz frame."""
    # 0xFF 0xFB = sync + MPEG1(11) + LayerIII(01) + no CRC(1)
    # 0x90 = bitrate_idx 9 (128kbps) + sample_idx 0 (44100Hz) + padding 0
    header = bytes([0xFF, 0xFB, (bitrate_idx << 4) | (sample_idx << 2), 0x00])
    frame_len = 417  # (144 * 128000 // 44100) + 0 = 417 bytes
    return header + (b"\x00" * (frame_len - 4))


def make_id3v2_header(payload_size: int) -> bytes:
    """Create an ID3v2.3 header with synchsafe size."""
    s0 = (payload_size >> 21) & 0x7F
    s1 = (payload_size >> 14) & 0x7F
    s2 = (payload_size >> 7) & 0x7F
    s3 = payload_size & 0x7F
    return b"ID3\x03\x00\x00" + bytes([s0, s1, s2, s3]) + (b"\x00" * payload_size)


class TTSTests(unittest.TestCase):
    def test_split_script_into_chunks_respects_max_chars(self) -> None:
        text = (
            "大家好，欢迎收听本期节目。今天我们来看三个 AI 工程领域的重要进展。"
            "第一个话题是推理引擎优化，它能够显著降低延迟，同时减少显存占用，非常值得关注！\n\n"
            "第二个话题关于智能体架构。很多团队正在从单一提示词转向多步骤编排。"
        )
        chunks = split_script_into_chunks(text, max_chars=60)
        self.assertGreater(len(chunks), 1)
        for ch in chunks:
            self.assertLessEqual(len(ch), 60)
        rejoined = "".join(chunks)
        self.assertIn("推理引擎优化", rejoined)
        self.assertIn("多步骤编排", rejoined)

    def test_strip_id3_tags_and_concatenate(self) -> None:
        frame = make_synthetic_mpeg1_frame()
        id3v2 = make_id3v2_header(32)
        id3v1 = b"TAG" + (b"\x00" * 125)

        chunk1 = id3v2 + (frame * 5) + id3v1
        chunk2 = id3v2 + (frame * 5)
        stripped = strip_id3_tags(chunk1)
        self.assertEqual(len(stripped), len(frame) * 5)

        combined = concatenate_mp3_bytes([chunk1, chunk2])
        self.assertEqual(len(combined), len(frame) * 10)
        self.assertFalse(combined.startswith(b"ID3"))

    def test_estimate_mpeg_duration(self) -> None:
        frame = make_synthetic_mpeg1_frame()
        # 383 frames * 1152 / 44100 = 10.004 seconds -> rounds to 10
        audio = make_id3v2_header(20) + (frame * 383)
        self.assertEqual(estimate_mpeg_duration_from_bytes(audio), 10)

    def test_synthesize_speech_with_mock_synthesizer(self) -> None:
        frame = make_synthetic_mpeg1_frame()
        calls: list[str] = []

        async def fake_chunk_synth(
            text: str, voice: str, rate: str, pitch: str
        ) -> bytes:
            calls.append(text)
            return make_id3v2_header(16) + (frame * 192)  # ~5.015s per chunk

        with tempfile.TemporaryDirectory() as tmp:
            out_mp3 = Path(tmp) / "out.mp3"
            cfg = TTSConfig(max_chunk_chars=60)
            script = (
                "第一段测试文本，确保这一句话本身有足够的长度，这样加上后面的内容后一定会超过单次合成字符上限。"
                "第二段测试文本，继续补充更多技术总结内容，以便稳妥触发分块逻辑并验证多个片段的拼接结果。"
            )
            dur = synthesize_speech(
                script, out_mp3, tts_config=cfg, chunk_synthesizer=fake_chunk_synth
            )
            self.assertEqual(len(calls), 2)
            self.assertTrue(out_mp3.is_file())
            self.assertEqual(dur, 10)

    def test_volcengine_chunk_synthesis_and_errors(self) -> None:
        frame = make_synthetic_mpeg1_frame()
        raw_audio = frame * 10
        captured: dict[str, object] = {}

        def fake_post(url: str, headers: dict[str, str], body: bytes) -> bytes:
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = json.loads(body.decode("utf-8"))
            return json.dumps(
                {"code": 3000, "message": "Success", "data": base64.b64encode(raw_audio).decode("ascii")}
            ).encode("utf-8")

        audio = synthesize_chunk_volcengine(
            text="你好，欢迎收听播客。",
            voice="zh_male_yangguangqingnian_moon_bigtts",
            speed_ratio=1.05,
            app_id="app-123",
            access_token="tok-xyz",
            cluster="volcano_tts",
            http_post=fake_post,
        )
        self.assertEqual(audio, raw_audio)
        self.assertEqual(captured["headers"]["Authorization"], "Bearer;tok-xyz")
        payload = captured["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["app"]["appid"], "app-123")
        self.assertEqual(payload["audio"]["voice_type"], "zh_male_yangguangqingnian_moon_bigtts")
        self.assertEqual(payload["audio"]["encoding"], "mp3")
        self.assertEqual(payload["audio"]["speed_ratio"], 1.05)

        def fail_post(url: str, headers: dict[str, str], body: bytes) -> bytes:
            return json.dumps({"code": 3001, "message": "Invalid token"}).encode("utf-8")

        with self.assertRaises(RuntimeError) as err:
            synthesize_chunk_volcengine(
                text="测试失败",
                voice="v",
                speed_ratio=1.0,
                app_id="app",
                access_token="bad",
                http_post=fail_post,
            )
        self.assertIn("3001", str(err.exception))

    def test_volcengine_provider_caps_chunks_and_checks_env(self) -> None:
        frame = make_synthetic_mpeg1_frame()
        seen_chunks: list[str] = []

        async def fake_synth(text: str, voice: str, rate: str, pitch: str) -> bytes:
            seen_chunks.append(text)
            return frame * 10

        with tempfile.TemporaryDirectory() as tmp:
            out_mp3 = Path(tmp) / "volc.mp3"
            cfg = TTSConfig(provider="volcengine", voice="zh_male_test", max_chunk_chars=650)
            long_text = ("这是一个用于测试火山引擎分块长度限制的中文句子。" * 25).strip()
            synthesize_speech(long_text, out_mp3, tts_config=cfg, chunk_synthesizer=fake_synth)
            self.assertGreater(len(seen_chunks), 1)
            for ch in seen_chunks:
                self.assertLessEqual(len(ch), 280)

            old_app = os.environ.pop("VOLCENGINE_APP_ID", None)
            old_tok = os.environ.pop("VOLCENGINE_ACCESS_TOKEN", None)
            try:
                with self.assertRaises(RuntimeError) as err:
                    synthesize_speech("你好世界", out_mp3, tts_config=cfg)
                self.assertIn("VOLCENGINE_APP_ID", str(err.exception))
            finally:
                if old_app is not None:
                    os.environ["VOLCENGINE_APP_ID"] = old_app
                if old_tok is not None:
                    os.environ["VOLCENGINE_ACCESS_TOKEN"] = old_tok

    def test_minimax_chunk_synthesis_and_errors(self) -> None:
        frame = make_synthetic_mpeg1_frame()
        raw_audio = frame * 10
        captured: dict[str, object] = {}

        def fake_post(url: str, headers: dict[str, str], body: bytes) -> bytes:
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = json.loads(body.decode("utf-8"))
            return json.dumps(
                {
                    "data": {"audio": raw_audio.hex()},
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                }
            ).encode("utf-8")

        audio = synthesize_chunk_minimax(
            text="欢迎收听中文科技播客。",
            voice="Chinese (Mandarin)_Radio_Host",
            speed_ratio=1.0,
            api_key="mm-key-123",
            model="speech-02-hd",
            group_id="grp-99",
            http_post=fake_post,
        )
        self.assertEqual(audio, raw_audio)
        self.assertEqual(captured["url"], "https://api.minimax.io/v1/t2a_v2?GroupId=grp-99")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer mm-key-123")
        payload = captured["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["model"], "speech-02-hd")
        self.assertEqual(payload["voice_setting"]["voice_id"], "Chinese (Mandarin)_Radio_Host")
        self.assertEqual(payload["audio_setting"]["format"], "mp3")

        def fail_post(url: str, headers: dict[str, str], body: bytes) -> bytes:
            return json.dumps(
                {"base_resp": {"status_code": 1004, "status_msg": "Invalid key"}}
            ).encode("utf-8")

        with self.assertRaises(RuntimeError) as err:
            synthesize_chunk_minimax(
                text="失败测试",
                voice="v",
                speed_ratio=1.0,
                api_key="bad",
                http_post=fail_post,
            )
        self.assertIn("1004", str(err.exception))

    def test_minimax_provider_checks_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_mp3 = Path(tmp) / "mm.mp3"
            cfg = TTSConfig(provider="minimax", voice="Chinese (Mandarin)_Radio_Host")
            old_key = os.environ.pop("MINIMAX_API_KEY", None)
            try:
                with self.assertRaises(RuntimeError) as err:
                    synthesize_speech("你好世界", out_mp3, tts_config=cfg)
                self.assertIn("MINIMAX_API_KEY", str(err.exception))
            finally:
                if old_key is not None:
                    os.environ["MINIMAX_API_KEY"] = old_key


if __name__ == "__main__":
    unittest.main()
