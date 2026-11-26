"""Configuration loader and dataclasses for yt-podcast."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import tomllib
from typing import Any


@dataclass(slots=True)
class DiscoveryConfig:
    channels: list[str] = field(default_factory=list)
    interests: list[str] = field(default_factory=list)
    lookback_hours: int = 72
    max_results_per_interest: int = 5
    max_results_per_channel: int = 3
    max_videos_total: int = 6
    min_duration_seconds: int = 180
    max_duration_seconds: int = 7200
    transcript_languages: list[str] = field(
        default_factory=lambda: ["zh-Hans", "zh-CN", "zh", "en", "en-US"]
    )
    max_transcript_chars: int = 12000


@dataclass(slots=True)
class LLMConfig:
    provider: str = "antigravity"
    model: str = "gemini-2.5-flash"
    target_minutes: int = 15
    language: str = "zh-CN"
    chars_per_minute: int = 240
    style_notes: str = (
        "自然口语化、逻辑清晰、像一位资深科技播客主理人在与听众聊最新技术动态，"
        "避免生硬书面翻译腔。"
    )

    @property
    def target_chars(self) -> int:
        return max(300, self.target_minutes * self.chars_per_minute)


@dataclass(slots=True)
class TTSConfig:
    voice: str = "zh-CN-YunxiNeural"
    rate: str = "+0%"
    pitch: str = "+0Hz"
    max_chunk_chars: int = 650


@dataclass(slots=True)
class PodcastConfig:
    title: str = "每日科技精选播客"
    subtitle: str = "YouTube 频道与前沿技术自动摘要（中文版）"
    description: str = (
        "自动追踪关注的 YouTube 频道与技术兴趣主题，"
        "提炼核心洞察并翻译生成约 15 分钟的中文音频播客。"
    )
    author: str = "AI Podcast Digest"
    email: str = "podcast@example.com"
    language: str = "zh-cn"
    category: str = "Technology"
    explicit: bool = False
    base_url: str = "https://example.github.io/yt-podcast"
    image_url: str = ""


@dataclass(slots=True)
class PublishingConfig:
    backend: str = "local"
    output_dir: Path = Path("docs")
    auto_push: bool = False
    git_remote: str = "origin"
    git_branch: str = "main"


@dataclass(slots=True)
class PathsConfig:
    state_db: Path = Path("data/state.db")
    staged_dir: Path = Path("data/staged")
    work_dir: Path = Path("data/work")


@dataclass(slots=True)
class AppConfig:
    project_root: Path
    config_path: Path
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    podcast: PodcastConfig = field(default_factory=PodcastConfig)
    publishing: PublishingConfig = field(default_factory=PublishingConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)


def load_dotenv(env_path: Path) -> None:
    """Load key=value pairs from a .env file without overriding existing env vars."""
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and (
            (value[0] == value[-1] == '"') or (value[0] == value[-1] == "'")
        ):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _resolve_path(base_dir: Path, raw_path: str | Path) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def _build_section(cls: type[Any], data: dict[str, Any] | None) -> Any:
    if not data:
        return cls()
    valid_fields = set(cls.__dataclass_fields__.keys())
    filtered = {k: v for k, v in data.items() if k in valid_fields}
    return cls(**filtered)


def load_config(config_path: str | Path = "config.toml") -> AppConfig:
    """Load AppConfig from a TOML file and optional sibling .env file."""
    cfg_path = Path(config_path).expanduser().resolve()
    if not cfg_path.is_file():
        raise FileNotFoundError(
            f"Configuration file not found: {cfg_path}. "
            "Copy config.example.toml to config.toml and customize it."
        )

    project_root = cfg_path.parent
    load_dotenv(project_root / ".env")

    with cfg_path.open("rb") as fh:
        raw: dict[str, Any] = tomllib.load(fh)

    discovery = _build_section(DiscoveryConfig, raw.get("discovery"))
    llm = _build_section(LLMConfig, raw.get("llm"))
    tts = _build_section(TTSConfig, raw.get("tts"))
    podcast = _build_section(PodcastConfig, raw.get("podcast"))
    podcast.base_url = podcast.base_url.rstrip("/")

    pub_raw = dict(raw.get("publishing") or {})
    if "output_dir" in pub_raw:
        pub_raw["output_dir"] = _resolve_path(project_root, pub_raw["output_dir"])
    publishing = _build_section(PublishingConfig, pub_raw)
    if not publishing.output_dir.is_absolute():
        publishing.output_dir = _resolve_path(project_root, publishing.output_dir)

    paths_raw = dict(raw.get("paths") or {})
    for key in ("state_db", "staged_dir", "work_dir"):
        if key in paths_raw:
            paths_raw[key] = _resolve_path(project_root, paths_raw[key])
    paths = _build_section(PathsConfig, paths_raw)
    paths.state_db = (
        paths.state_db
        if paths.state_db.is_absolute()
        else _resolve_path(project_root, paths.state_db)
    )
    paths.staged_dir = (
        paths.staged_dir
        if paths.staged_dir.is_absolute()
        else _resolve_path(project_root, paths.staged_dir)
    )
    paths.work_dir = (
        paths.work_dir
        if paths.work_dir.is_absolute()
        else _resolve_path(project_root, paths.work_dir)
    )

    _validate_config(discovery, llm, publishing)

    return AppConfig(
        project_root=project_root,
        config_path=cfg_path,
        discovery=discovery,
        llm=llm,
        tts=tts,
        podcast=podcast,
        publishing=publishing,
        paths=paths,
    )


def _validate_config(
    discovery: DiscoveryConfig,
    llm: LLMConfig,
    publishing: PublishingConfig,
) -> None:
    if llm.provider not in {"antigravity", "gemini", "staged"}:
        raise ValueError(
            f"Unsupported llm.provider '{llm.provider}'. "
            "Expected one of: antigravity, gemini, staged."
        )
    if publishing.backend not in {"local", "github_pages", "s3"}:
        raise ValueError(
            f"Unsupported publishing.backend '{publishing.backend}'. "
            "Expected one of: local, github_pages, s3."
        )
    if discovery.max_videos_total < 1:
        raise ValueError("discovery.max_videos_total must be >= 1.")
    if llm.target_minutes < 1:
        raise ValueError("llm.target_minutes must be >= 1.")
