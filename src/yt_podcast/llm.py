"""LLM highlight extraction, Chinese translation, and 15-minute podcast script generation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Protocol

from yt_podcast.config import LLMConfig
from yt_podcast.youtube import DiscoveredVideo


@dataclass(slots=True)
class VideoHighlight:
    video: DiscoveredVideo
    highlights_text: str


@dataclass(slots=True)
class DigestResult:
    episode_title: str
    episode_summary: str
    spoken_script: str
    highlights: list[VideoHighlight]


class StagedPromptReady(Exception):
    """Raised when provider='staged' writes a prompt file for external completion."""

    def __init__(self, prompt_path: Path, video_ids: list[str]) -> None:
        self.prompt_path = prompt_path
        self.video_ids = video_ids
        super().__init__(
            f"Staged prompt written to {prompt_path}. "
            "Generate the script and resume with: "
            f"yt-podcast run --from-script {prompt_path}"
        )


class LLMClient(Protocol):
    def generate(self, system_prompt: str, user_prompt: str) -> str:
        """Generate text from system + user prompts."""


class AntigravityLLMClient:
    """Uses the Antigravity Python SDK (`google-antigravity`) directly."""

    def __init__(self) -> None:
        pass

    async def _generate_async(self, system_prompt: str, user_prompt: str) -> str:
        try:
            from google.antigravity import Agent, LocalAgentConfig
        except ImportError as exc:
            raise RuntimeError(
                "google-antigravity SDK is not installed in this Python environment. "
                "Install it with: pip install google-antigravity "
                "(or switch llm.provider in config.toml to 'gemini' or 'staged')."
            ) from exc

        config = LocalAgentConfig(system_instructions=system_prompt)
        chunks: list[str] = []
        async with Agent(config) as agent:
            response = await agent.chat(user_prompt)
            async for token in response:
                chunks.append(str(token))
        return "".join(chunks).strip()

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        return asyncio.run(self._generate_async(system_prompt, user_prompt))


class GeminiLLMClient:
    """Uses the Google Gemini API (`google-genai`) via GEMINI_API_KEY."""

    def __init__(self, model: str = "gemini-2.5-flash") -> None:
        self.model = model

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise RuntimeError(
                "google-genai is not installed. Install with: pip install google-genai"
            ) from exc

        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Add it to .env or your shell environment."
            )

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=self.model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.7,
            ),
        )
        return (response.text or "").strip()


def create_llm_client(config: LLMConfig) -> LLMClient:
    """Instantiate the configured LLM client."""
    if config.provider == "antigravity":
        return AntigravityLLMClient()
    if config.provider == "gemini":
        return GeminiLLMClient(model=config.model)
    raise ValueError(
        f"Provider '{config.provider}' does not create an online LLMClient."
    )


def build_video_highlight_prompts(
    video: DiscoveredVideo, target_language: str
) -> tuple[str, str]:
    """Build pass-1 system and user prompts to extract key highlights from one video."""
    system_prompt = (
        "You are an expert technical editor and bilingual analyst. "
        "Extract the most valuable, concrete insights from a YouTube video transcript "
        "or description, removing sponsor reads, fluff, and filler."
    )
    source_kind = "Transcript" if video.transcript_language != "description" else "Description"
    user_prompt = (
        f"Analyze this video and extract 4 to 7 high-signal highlights in {target_language} "
        f"(translating cleanly if the source is in another language).\n\n"
        f"Title: {video.title}\n"
        f"Channel: {video.channel_name}\n"
        f"Published: {video.published_at.strftime('%Y-%m-%d')}\n"
        f"URL: {video.url}\n"
        f"Source Type: {source_kind} ({video.transcript_language})\n\n"
        "Include:\n"
        "1. Core thesis / main news or engineering takeaway\n"
        "2. Concrete numbers, benchmarks, architecture details, or actionable advice\n"
        "3. Why it matters for practitioners\n\n"
        f"Content:\n{video.transcript or video.description or video.title}"
    )
    return system_prompt, user_prompt


def build_podcast_script_prompts(
    highlights: list[VideoHighlight], llm_config: LLMConfig
) -> tuple[str, str]:
    """Build pass-2 system and user prompts to synthesize a ~15-minute Chinese podcast script."""
    target_chars = llm_config.target_chars
    min_chars = int(target_chars * 0.9)
    max_chars = int(target_chars * 1.15)
    today_str = datetime.now(timezone.utc).strftime("%Y年%m月%d日")

    system_prompt = (
        "You are the host and chief writer of a popular Mandarin Chinese technology podcast. "
        "You turn multi-source video highlights into an engaging, deeply informative, natural "
        "spoken audio monologue. You MUST output the exact tagged sections requested."
    )

    sections: list[str] = []
    for idx, item in enumerate(highlights, start=1):
        v = item.video
        sections.append(
            f"### Source {idx}\n"
            f"- Title: {v.title}\n"
            f"- Channel: {v.channel_name}\n"
            f"- URL: {v.url}\n"
            f"- Highlights:\n{item.highlights_text}\n"
        )

    user_prompt = (
        f"Create a single-host spoken podcast digest in {llm_config.language} dated {today_str}.\n\n"
        f"Style guidance: {llm_config.style_notes}\n\n"
        "Requirements for the spoken script:\n"
        f"1. Target duration is {llm_config.target_minutes} minutes, which requires roughly "
        f"{min_chars} to {max_chars} Chinese characters (around {target_chars} characters).\n"
        "2. Translate and localize all English or non-Chinese terminology naturally into spoken Chinese "
        "(you may keep widely known acronyms like AI, LLM, GPU, API when natural to pronounce).\n"
        "3. Structure the episode smoothly:\n"
        "   - Warm opening & today's overview (~200 chars)\n"
        "   - Deep dive into each video topic with smooth transitions, clear explanations, examples, "
        "and why it matters (~500-900 chars per topic depending on number of videos)\n"
        "   - Synthesis of cross-cutting themes & concise closing sign-off (~200 chars)\n"
        "4. CRITICAL TTS RULES for `===SPOKEN_SCRIPT===`: Write pure spoken prose only. "
        "Do NOT include Markdown (`#`, `*`, `**`, `-`, backticks), bullet points, emojis, stage "
        "directions like `[音乐]` or `(笑)`, or raw `https://` URLs.\n\n"
        "Output EXACTLY these three tagged blocks in order:\n"
        "===EPISODE_TITLE===\n"
        "<concise engaging Chinese episode title, 1 line>\n"
        "===SHOW_NOTES===\n"
        "<concise Chinese episode summary + bullet list of source videos with URLs>\n"
        "===SPOKEN_SCRIPT===\n"
        "<full spoken script ready for TTS>\n\n"
        "Source Highlights:\n" + "\n".join(sections)
    )
    return system_prompt, user_prompt


def clean_spoken_script(text: str) -> str:
    """Strip markdown artifacts, stage directions, and URLs before TTS synthesis."""
    cleaned = text.replace("\r\n", "\n")
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    cleaned = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", cleaned)
    cleaned = re.sub(r"^#{1,6}\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^\s*[-*•]\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\*{1,3}|_{1,3}|`+", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def format_default_show_notes(videos: list[DiscoveredVideo]) -> str:
    """Build fallback show notes with clickable video attribution."""
    lines = ["本期节目涵盖以下 YouTube 视频精选内容：", ""]
    for idx, v in enumerate(videos, start=1):
        lines.append(f"{idx}. {v.title} ({v.channel_name}) - {v.url}")
    return "\n".join(lines)


def parse_digest_response(
    raw_text: str,
    videos: list[DiscoveredVideo],
    highlights: list[VideoHighlight] | None = None,
) -> DigestResult:
    """Parse tagged LLM output (or plain script fallback) into DigestResult."""
    title_match = re.search(
        r"===EPISODE_TITLE===\s*(.*?)\s*(?====SHOW_NOTES===|===SPOKEN_SCRIPT===|\Z)",
        raw_text,
        flags=re.DOTALL,
    )
    notes_match = re.search(
        r"===SHOW_NOTES===\s*(.*?)\s*(?====SPOKEN_SCRIPT===|\Z)",
        raw_text,
        flags=re.DOTALL,
    )
    script_match = re.search(
        r"===SPOKEN_SCRIPT===\s*(.*)\Z",
        raw_text,
        flags=re.DOTALL,
    )

    today_label = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    default_title = f"科技前沿精选播客 ({today_label})"

    if script_match:
        raw_script = script_match.group(1)
        episode_title = (
            title_match.group(1).strip().splitlines()[0].strip()
            if title_match and title_match.group(1).strip()
            else default_title
        )
        episode_summary = (
            notes_match.group(1).strip()
            if notes_match and notes_match.group(1).strip()
            else format_default_show_notes(videos)
        )
    else:
        raw_script = raw_text
        episode_title = default_title
        episode_summary = format_default_show_notes(videos)

    spoken_script = clean_spoken_script(raw_script)
    if not spoken_script:
        raise ValueError("Generated spoken script is empty.")

    return DigestResult(
        episode_title=episode_title,
        episode_summary=spoken_script[:160] + "..."
        if not episode_summary
        else episode_summary,
        spoken_script=spoken_script,
        highlights=highlights or [],
    )


def write_staged_prompt_file(
    videos: list[DiscoveredVideo],
    llm_config: LLMConfig,
    staged_dir: Path,
) -> Path:
    """Write a complete combined prompt file for manual/Antigravity chat execution."""
    staged_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    prompt_path = staged_dir / f"prompt_{stamp}.md"

    dummy_highlights = [
        VideoHighlight(
            video=v,
            highlights_text=v.transcript or v.description or v.title,
        )
        for v in videos
    ]
    system_prompt, user_prompt = build_podcast_script_prompts(
        dummy_highlights, llm_config
    )
    content = (
        "# yt-podcast Staged Prompt\n\n"
        "Ask Antigravity (or any LLM) to follow the instructions below and save the output "
        f"to a file (e.g. `{staged_dir / f'script_{stamp}.txt'}`), then run:\n\n"
        "```bash\n"
        f"yt-podcast run --from-script {staged_dir / f'script_{stamp}.txt'}\n"
        "```\n\n"
        "## System Instructions\n\n"
        f"{system_prompt}\n\n"
        "## Prompt\n\n"
        f"{user_prompt}\n"
    )
    prompt_path.write_text(content, encoding="utf-8")

    meta_path = staged_dir / f"prompt_{stamp}.meta.json"
    meta_records = [
        {
            "video_id": v.video_id,
            "title": v.title,
            "channel_name": v.channel_name,
            "channel_id": v.channel_id,
            "url": v.url,
            "published_at": v.published_at.astimezone(timezone.utc).isoformat(),
        }
        for v in videos
    ]
    meta_path.write_text(
        json.dumps(meta_records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return prompt_path


def load_staged_metadata_for_script(script_path: Path) -> list[DiscoveredVideo]:
    """Load companion video metadata JSON for a staged script if present."""
    candidates: list[Path] = []
    match = re.match(r"^script_(.+?)\.[^.]+$", script_path.name)
    if match:
        stamp = match.group(1)
        candidates.append(script_path.parent / f"prompt_{stamp}.meta.json")
    candidates.append(script_path.with_suffix(".meta.json"))

    meta_file = next((p for p in candidates if p.is_file()), None)
    if meta_file is None:
        return []

    raw_items = json.loads(meta_file.read_text(encoding="utf-8"))
    if not isinstance(raw_items, list):
        return []

    videos: list[DiscoveredVideo] = []
    for item in raw_items:
        if not isinstance(item, dict) or not item.get("video_id"):
            continue
        pub_raw = str(item.get("published_at") or "")
        try:
            pub_dt = datetime.fromisoformat(pub_raw.replace("Z", "+00:00"))
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pub_dt = datetime.now(timezone.utc)

        videos.append(
            DiscoveredVideo(
                video_id=str(item["video_id"]),
                title=str(item.get("title") or item["video_id"]),
                channel_name=str(item.get("channel_name") or ""),
                channel_id=str(item.get("channel_id") or ""),
                url=str(item.get("url") or f"https://www.youtube.com/watch?v={item['video_id']}"),
                published_at=pub_dt,
            )
        )
    return videos


def generate_digest(
    videos: list[DiscoveredVideo],
    llm_config: LLMConfig,
    staged_dir: Path,
    client: LLMClient | None = None,
) -> DigestResult:
    """Run two-pass highlight extraction + Chinese podcast script synthesis."""
    if not videos:
        raise ValueError("Cannot generate digest with zero videos.")

    if llm_config.provider == "staged" and client is None:
        prompt_path = write_staged_prompt_file(videos, llm_config, staged_dir)
        raise StagedPromptReady(prompt_path, [v.video_id for v in videos])

    active_client = client or create_llm_client(llm_config)

    highlights: list[VideoHighlight] = []
    for video in videos:
        sys_p, usr_p = build_video_highlight_prompts(video, llm_config.language)
        summary_text = active_client.generate(sys_p, usr_p)
        highlights.append(VideoHighlight(video=video, highlights_text=summary_text))

    sys_script, usr_script = build_podcast_script_prompts(highlights, llm_config)
    raw_output = active_client.generate(sys_script, usr_script)
    return parse_digest_response(raw_output, videos=videos, highlights=highlights)
