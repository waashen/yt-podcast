"""Edge TTS synthesis, Chinese/English text chunking, and pure-Python MP3 assembly."""

from __future__ import annotations

import asyncio
from pathlib import Path
import re
from typing import Awaitable, Callable

from yt_podcast.config import TTSConfig

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?])|(?<=\.)(?=\s|$)")
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[，、,;；：:])")

# MPEG Audio Layer III tables: bitrates in kbps (indexed by bitrate_index 1..14)
_BITRATES_MPEG1_L3 = (
    0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0
)
_BITRATES_MPEG2_L3 = (
    0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0
)
_SAMPLE_RATES = {
    0b11: (44100, 48000, 32000, 0),   # MPEG 1
    0b10: (22050, 24000, 16000, 0),   # MPEG 2
    0b00: (11025, 12000, 8000, 0),    # MPEG 2.5
}


def split_script_into_chunks(text: str, max_chars: int = 650) -> list[str]:
    """Split a long script into natural TTS-friendly chunks under max_chars."""
    if max_chars < 50:
        raise ValueError("max_chars must be at least 50.")

    normalized = text.replace("\r\n", "\n").strip()
    if not normalized:
        return []

    paragraphs = [p.strip() for p in re.split(r"\n+", normalized) if p.strip()]
    units: list[str] = []

    for para in paragraphs:
        if len(para) <= max_chars:
            units.append(para)
            continue

        sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(para) if s.strip()]
        for sent in sentences:
            if len(sent) <= max_chars:
                units.append(sent)
                continue

            clauses = [c.strip() for c in _CLAUSE_SPLIT_RE.split(sent) if c.strip()]
            for clause in clauses:
                if len(clause) <= max_chars:
                    units.append(clause)
                else:
                    for idx in range(0, len(clause), max_chars):
                        units.append(clause[idx : idx + max_chars])

    chunks: list[str] = []
    current = ""
    for unit in units:
        if not current:
            current = unit
        elif len(current) + 1 + len(unit) <= max_chars:
            sep = "" if _ends_with_cjk_punct(current) else " "
            current = f"{current}{sep}{unit}"
        else:
            chunks.append(current)
            current = unit
    if current:
        chunks.append(current)
    return chunks


def _ends_with_cjk_punct(text: str) -> bool:
    return bool(text) and text[-1] in "。！？，、；：”’）】"


def strip_id3_tags(mp3_bytes: bytes) -> bytes:
    """Strip leading ID3v2 headers and trailing ID3v1 tags from an MP3 byte buffer."""
    data = mp3_bytes
    offset = 0
    while len(data) - offset >= 10 and data[offset : offset + 3] == b"ID3":
        flags = data[offset + 5]
        size = (
            ((data[offset + 6] & 0x7F) << 21)
            | ((data[offset + 7] & 0x7F) << 14)
            | ((data[offset + 8] & 0x7F) << 7)
            | (data[offset + 9] & 0x7F)
        )
        footer = 10 if (flags & 0x10) else 0
        header_total = 10 + size + footer
        if offset + header_total > len(data):
            break
        offset += header_total

    end = len(data)
    if end - offset >= 128 and data[end - 128 : end - 125] == b"TAG":
        end -= 128

    return data[offset:end]


def concatenate_mp3_bytes(chunks: list[bytes]) -> bytes:
    """Concatenate MP3 byte chunks cleanly by stripping ID3 headers/trailers."""
    parts = [strip_id3_tags(chunk) for chunk in chunks if chunk]
    return b"".join(p for p in parts if p)


def estimate_mpeg_duration_from_bytes(mp3_bytes: bytes) -> int:
    """Estimate MP3 duration in whole seconds by walking MPEG Layer III frames."""
    data = strip_id3_tags(mp3_bytes)
    n = len(data)
    pos = 0
    total_seconds = 0.0

    while pos + 4 <= n:
        if data[pos] != 0xFF or (data[pos + 1] & 0xE0) != 0xE0:
            pos += 1
            continue

        b1 = data[pos + 1]
        b2 = data[pos + 2]
        version_bits = (b1 >> 3) & 0x03
        layer_bits = (b1 >> 1) & 0x03
        bitrate_idx = (b2 >> 4) & 0x0F
        sample_idx = (b2 >> 2) & 0x03
        padding = (b2 >> 1) & 0x01

        if (
            version_bits == 0b01
            or layer_bits != 0b01
            or bitrate_idx in (0, 15)
            or sample_idx == 3
        ):
            pos += 1
            continue

        sample_rate = _SAMPLE_RATES[version_bits][sample_idx]
        if version_bits == 0b11:  # MPEG 1
            bitrate = _BITRATES_MPEG1_L3[bitrate_idx] * 1000
            samples_per_frame = 1152
            frame_len = (144 * bitrate // sample_rate) + padding
        else:  # MPEG 2 / 2.5
            bitrate = _BITRATES_MPEG2_L3[bitrate_idx] * 1000
            samples_per_frame = 576
            frame_len = (72 * bitrate // sample_rate) + padding

        if frame_len < 4 or pos + frame_len > n:
            break

        total_seconds += samples_per_frame / sample_rate
        pos += frame_len

    return int(round(total_seconds))


def get_mp3_duration_seconds(mp3_path: Path) -> int:
    """Return MP3 duration in seconds using mutagen when available, with frame fallback."""
    try:
        from mutagen.mp3 import MP3

        info = MP3(str(mp3_path)).info
        if info and getattr(info, "length", 0) > 0:
            return int(round(float(info.length)))
    except Exception:
        pass

    return estimate_mpeg_duration_from_bytes(mp3_path.read_bytes())


async def _synthesize_chunk_edge_tts(
    text: str, voice: str, rate: str, pitch: str
) -> bytes:
    try:
        import edge_tts
    except ImportError as exc:
        raise RuntimeError(
            "edge-tts is required for audio synthesis. Install it with: pip install edge-tts"
        ) from exc

    communicate = edge_tts.Communicate(text, voice=voice, rate=rate, pitch=pitch)
    audio_parts: list[bytes] = []
    async for msg in communicate.stream():
        if msg.get("type") == "audio":
            audio_parts.append(msg["data"])
    return b"".join(audio_parts)


async def _synthesize_all_chunks_async(
    chunks: list[str],
    tts_config: TTSConfig,
    chunk_fn: Callable[[str, str, str, str], Awaitable[bytes]],
) -> list[bytes]:
    results: list[bytes] = []
    for chunk in chunks:
        audio = await chunk_fn(
            chunk, tts_config.voice, tts_config.rate, tts_config.pitch
        )
        if not audio:
            raise RuntimeError(f"TTS returned empty audio for chunk: {chunk[:40]}...")
        results.append(audio)
    return results


def synthesize_speech(
    text: str,
    output_mp3: Path,
    tts_config: TTSConfig,
    chunk_synthesizer: Callable[[str, str, str, str], Awaitable[bytes]] | None = None,
) -> int:
    """Synthesize spoken script to an MP3 file and return duration in seconds."""
    chunks = split_script_into_chunks(text, max_chars=tts_config.max_chunk_chars)
    if not chunks:
        raise ValueError("Cannot synthesize empty script text.")

    synth_fn = chunk_synthesizer or _synthesize_chunk_edge_tts
    audio_chunks = asyncio.run(
        _synthesize_all_chunks_async(chunks, tts_config, synth_fn)
    )
    combined = concatenate_mp3_bytes(audio_chunks)
    if not combined:
        raise RuntimeError("Concatenated MP3 output is empty.")

    output_mp3 = Path(output_mp3)
    output_mp3.parent.mkdir(parents=True, exist_ok=True)
    output_mp3.write_bytes(combined)
    return get_mp3_duration_seconds(output_mp3)


async def _list_voices_async(locale_prefix: str = "zh") -> list[dict[str, str]]:
    try:
        import edge_tts
    except ImportError as exc:
        raise RuntimeError(
            "edge-tts is required to list voices. Install it with: pip install edge-tts"
        ) from exc

    voices = await edge_tts.list_voices()
    filtered = [
        {
            "ShortName": str(v.get("ShortName", "")),
            "Gender": str(v.get("Gender", "")),
            "Locale": str(v.get("Locale", "")),
        }
        for v in voices
        if not locale_prefix or str(v.get("Locale", "")).lower().startswith(locale_prefix.lower())
    ]
    filtered.sort(key=lambda item: item["ShortName"])
    return filtered


def list_voices(locale_prefix: str = "zh") -> list[dict[str, str]]:
    """Return available Edge TTS voices filtered by locale prefix."""
    return asyncio.run(_list_voices_async(locale_prefix=locale_prefix))
