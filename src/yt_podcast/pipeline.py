"""End-to-end orchestrator for YouTube discovery, Chinese digest generation, TTS, and publishing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Awaitable, Callable
import uuid

from yt_podcast.config import AppConfig
from yt_podcast.llm import (
    DigestResult,
    LLMClient,
    StagedPromptReady,
    generate_digest,
    load_staged_metadata_for_script,
    parse_digest_response,
)
from yt_podcast.publisher import LocalPublisher, PublishResult, create_publisher
from yt_podcast.rss import build_index_html, build_rss_feed
from yt_podcast.state import EpisodeRecord, StateStore
from yt_podcast.tts import synthesize_speech
from yt_podcast.youtube import DiscoveredVideo, discover_and_prepare_videos

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PipelineResult:
    status: str
    videos: list[DiscoveredVideo]
    episode: EpisodeRecord | None = None
    mp3_path: Path | None = None
    script_path: Path | None = None
    staged_prompt_path: Path | None = None
    publish_result: PublishResult | None = None


def run_pipeline(
    config: AppConfig,
    dry_run: bool = False,
    from_script: Path | None = None,
    skip_publish: bool = False,
    llm_client: LLMClient | None = None,
    chunk_synthesizer: Callable[[str, str, str, str], Awaitable[bytes]] | None = None,
    videos_override: list[DiscoveredVideo] | None = None,
) -> PipelineResult:
    """Execute the full YouTube -> Chinese podcast digest pipeline."""
    with StateStore(config.paths.state_db) as state:
        videos: list[DiscoveredVideo] = []
        digest: DigestResult

        if from_script is not None:
            script_file = Path(from_script).expanduser().resolve()
            if not script_file.is_file():
                raise FileNotFoundError(f"Script file not found: {script_file}")
            raw_text = script_file.read_text(encoding="utf-8")
            videos = load_staged_metadata_for_script(script_file)
            digest = parse_digest_response(raw_text, videos=videos)
        else:
            videos = (
                videos_override
                if videos_override is not None
                else discover_and_prepare_videos(config.discovery, state=state)
            )
            if not videos:
                logger.info("No new qualifying YouTube videos found.")
                return PipelineResult(status="no_new_videos", videos=[])

            if dry_run:
                return PipelineResult(status="dry_run", videos=videos)

            try:
                digest = generate_digest(
                    videos=videos,
                    llm_config=config.llm,
                    staged_dir=config.paths.staged_dir,
                    client=llm_client,
                )
            except StagedPromptReady as staged_exc:
                return PipelineResult(
                    status="staged",
                    videos=videos,
                    staged_prompt_path=staged_exc.prompt_path,
                )

        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y%m%d_%H%M%S")
        short_id = uuid.uuid4().hex[:6]
        episode_slug = f"digest_{stamp}_{short_id}"
        audio_filename = f"{episode_slug}.mp3"

        config.paths.work_dir.mkdir(parents=True, exist_ok=True)
        script_path = config.paths.work_dir / f"{episode_slug}.txt"
        script_path.write_text(
            f"# {digest.episode_title}\n\n{digest.episode_summary}\n\n---\n\n{digest.spoken_script}\n",
            encoding="utf-8",
        )

        mp3_path = config.paths.work_dir / audio_filename
        duration_seconds = synthesize_speech(
            text=digest.spoken_script,
            output_mp3=mp3_path,
            tts_config=config.tts,
            chunk_synthesizer=chunk_synthesizer,
        )

        publisher = (
            LocalPublisher(config.publishing.output_dir, config.podcast.base_url)
            if skip_publish
            else create_publisher(config)
        )
        audio_url = publisher.audio_url_for(audio_filename)
        byte_size = mp3_path.stat().st_size

        source_links = [
            {
                "video_id": v.video_id,
                "title": v.title,
                "channel_name": v.channel_name,
                "url": v.url,
            }
            for v in videos
        ]

        episode = EpisodeRecord(
            guid=f"yt-podcast-{episode_slug}",
            title=digest.episode_title,
            summary=digest.episode_summary,
            audio_filename=audio_filename,
            audio_url=audio_url,
            byte_size=byte_size,
            duration_seconds=duration_seconds,
            published_at=now,
            video_ids=[v.video_id for v in videos],
            source_links=source_links,
        )

        state.record_episode(episode)
        if videos:
            state.mark_videos_seen(
                [
                    {
                        "video_id": v.video_id,
                        "title": v.title,
                        "channel_name": v.channel_name,
                        "url": v.url,
                        "published_at": v.published_at.isoformat(),
                    }
                    for v in videos
                ]
            )

        episodes = state.list_episodes()
        feed_xml = build_rss_feed(config.podcast, episodes)
        index_html = build_index_html(config.podcast, episodes)

        publish_result = publisher.publish(
            mp3_path=mp3_path,
            audio_filename=audio_filename,
            feed_xml=feed_xml,
            index_html=index_html,
            commit_message=f"Add podcast episode: {episode.title}",
        )

        return PipelineResult(
            status="published",
            videos=videos,
            episode=episode,
            mp3_path=mp3_path,
            script_path=script_path,
            publish_result=publish_result,
        )


def rebuild_feed(config: AppConfig, publish: bool = True) -> PublishResult:
    """Regenerate feed.xml and index.html from recorded episodes in SQLite."""
    publisher = (
        create_publisher(config)
        if publish
        else LocalPublisher(config.publishing.output_dir, config.podcast.base_url)
    )
    with StateStore(config.paths.state_db) as state:
        episodes = state.list_episodes()
        for ep in episodes:
            expected_url = publisher.audio_url_for(ep.audio_filename)
            if ep.audio_url != expected_url:
                ep.audio_url = expected_url
                state.record_episode(ep)

    feed_xml = build_rss_feed(config.podcast, episodes)
    index_html = build_index_html(config.podcast, episodes)
    return publisher.publish(
        mp3_path=None,
        audio_filename=None,
        feed_xml=feed_xml,
        index_html=index_html,
        commit_message="Rebuild podcast RSS feed and index",
    )
