from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from yt_podcast.config import TTSConfig
from yt_podcast.tts import (
    concatenate_mp3_bytes,
    estimate_mpeg_duration_from_bytes,
    split_script_into_chunks,
    strip_id3_tags,
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


if __name__ == "__main__":
    unittest.main()
