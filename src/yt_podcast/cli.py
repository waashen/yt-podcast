"""Command-line interface for yt-podcast."""

from __future__ import annotations

import argparse
import html
import logging
from pathlib import Path
import sys
from typing import Sequence

from yt_podcast.config import AppConfig, load_config
from yt_podcast.llm import clean_spoken_script, parse_digest_response
from yt_podcast.pipeline import rebuild_feed, run_pipeline
from yt_podcast.state import StateStore
from yt_podcast.tts import list_voices, synthesize_speech
from yt_podcast.youtube import discover_and_prepare_videos


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _render_launchd_plist(
    project_root: Path,
    script_path: Path,
    log_dir: Path,
    hour: int,
    minute: int,
) -> str:
    label = "com.ytpodcast.digest"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{html.escape(label)}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>{html.escape(str(script_path))}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>{html.escape(str(project_root))}</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>{hour}</integer>
    <key>Minute</key>
    <integer>{minute}</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>{html.escape(str(log_dir / "launchd.out.log"))}</string>
  <key>StandardErrorPath</key>
  <string>{html.escape(str(log_dir / "launchd.err.log"))}</string>
  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
"""


def _render_runner_script(python_exe: str, config_path: Path) -> str:
    return f"""#!/usr/bin/env zsh
set -euo pipefail

PROJECT_DIR="{config_path.parent}"
cd "$PROJECT_DIR"

export PYTHONPATH="$PROJECT_DIR/src${{PYTHONPATH:+:$PYTHONPATH}}"
"{python_exe}" -m yt_podcast.cli run --config "{config_path}"
"""


def _cmd_discover(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    with StateStore(cfg.paths.state_db) as state:
        videos = discover_and_prepare_videos(cfg.discovery, state=state)
    if not videos:
        print("No new qualifying YouTube videos found.")
        return 0

    print(f"Discovered {len(videos)} video(s):")
    for idx, v in enumerate(videos, start=1):
        dur = f"{v.duration_seconds}s" if v.duration_seconds is not None else "unknown"
        print(
            f"  {idx}. [{v.source_type}] {v.title}\n"
            f"     Channel: {v.channel_name} | Duration: {dur} | "
            f"Published: {v.published_at.strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"     URL: {v.url}\n"
            f"     Transcript: {v.transcript_language} ({len(v.transcript)} chars)"
        )
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    from_script = Path(args.from_script) if args.from_script else None
    result = run_pipeline(
        cfg,
        dry_run=args.dry_run,
        from_script=from_script,
        skip_publish=args.skip_publish,
    )

    if result.status == "no_new_videos":
        print("No new qualifying YouTube videos found. Nothing to publish.")
        return 0

    if result.status == "dry_run":
        print(f"Dry run complete. Found {len(result.videos)} qualifying video(s):")
        for idx, v in enumerate(result.videos, start=1):
            print(f"  {idx}. {v.title} ({v.channel_name}) - {v.url}")
        return 0

    if result.status == "staged":
        print(f"Staged prompt saved to: {result.staged_prompt_path}")
        print(
            f"After generating the script, run:\n  yt-podcast run --from-script <script_file>"
        )
        return 0

    if result.episode and result.publish_result:
        print(f"Episode published: {result.episode.title}")
        print(f"  Audio file : {result.mp3_path}")
        print(f"  Script file: {result.script_path}")
        print(f"  Duration   : {result.episode.duration_seconds}s")
        print(f"  Feed URL   : {result.publish_result.feed_url}")
        print(f"  Audio URL  : {result.publish_result.audio_url}")
        print(f"  Status     : {result.publish_result.details}")
    return 0


def _cmd_voices(args: argparse.Namespace) -> int:
    voices = list_voices(locale_prefix=args.locale)
    for v in voices:
        print(f"{v['ShortName']:28s} {v['Gender']:8s} {v['Locale']}")
    return 0


def _cmd_tts(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    input_path = Path(args.input).expanduser().resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"Input transcript file not found: {input_path}")

    tts_cfg = cfg.tts
    if args.provider:
        if args.provider not in {"edge_tts", "volcengine"}:
            raise ValueError("Unsupported --provider. Expected: edge_tts or volcengine.")
        tts_cfg.provider = args.provider
    if args.voice:
        tts_cfg.voice = args.voice

    raw_text = input_path.read_text(encoding="utf-8")
    if "===SPOKEN_SCRIPT===" in raw_text:
        spoken_text = parse_digest_response(raw_text, videos=[]).spoken_script
    else:
        spoken_text = clean_spoken_script(raw_text)

    out_path = Path(args.output).expanduser().resolve()
    duration = synthesize_speech(spoken_text, out_path, tts_config=tts_cfg)
    size_bytes = out_path.stat().st_size
    print(f"MP3 generated: {out_path}")
    print(f"  Provider   : {tts_cfg.provider} ({tts_cfg.voice})")
    print(f"  Duration   : {duration}s")
    print(f"  Size       : {size_bytes} bytes")
    return 0


def _cmd_rebuild_feed(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    res = rebuild_feed(cfg, publish=not args.no_publish)
    print(f"Feed rebuilt: {res.feed_url}")
    print(f"Details     : {res.details}")
    return 0


def _cmd_install_schedule(args: argparse.Namespace) -> int:
    cfg: AppConfig = load_config(args.config)
    if not (0 <= args.hour <= 23) or not (0 <= args.minute <= 59):
        raise ValueError("--hour must be 0..23 and --minute must be 0..59.")

    scripts_dir = cfg.project_root / "scripts"
    log_dir = cfg.project_root / "logs"
    runner_path = scripts_dir / "run_digest.sh"
    plist_path = scripts_dir / "com.ytpodcast.digest.plist"

    runner_content = _render_runner_script(sys.executable, cfg.config_path)
    plist_content = _render_launchd_plist(
        project_root=cfg.project_root,
        script_path=runner_path,
        log_dir=log_dir,
        hour=args.hour,
        minute=args.minute,
    )

    if args.dry_run:
        print(f"[dry-run] Would write {runner_path}")
        print(f"[dry-run] Would write {plist_path}")
        print(plist_content)
        return 0

    scripts_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    runner_path.write_text(runner_content, encoding="utf-8")
    runner_path.chmod(0o755)
    plist_path.write_text(plist_content, encoding="utf-8")

    target_agent = Path.home() / "Library" / "LaunchAgents" / "com.ytpodcast.digest.plist"
    print(f"Generated runner : {runner_path}")
    print(f"Generated plist  : {plist_path}")
    print("\nTo enable daily macOS launchd schedule, run:")
    print(f'  mkdir -p "{target_agent.parent}"')
    print(f'  cp "{plist_path}" "{target_agent}"')
    print(f'  launchctl unload "{target_agent}" 2>/dev/null || true')
    print(f'  launchctl load "{target_agent}"')
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yt-podcast",
        description="YouTube channel & interest digest to Chinese podcast RSS pipeline.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run the full digest & podcast publish pipeline.")
    p_run.add_argument("--config", default="config.toml", help="Path to config.toml")
    p_run.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover videos and transcripts without calling LLM/TTS or marking seen.",
    )
    p_run.add_argument(
        "--from-script",
        default=None,
        help="Synthesize and publish from an existing script file instead of calling LLM.",
    )
    p_run.add_argument(
        "--skip-publish",
        action="store_true",
        help="Write local output_dir files only without git commit/push or S3 upload.",
    )
    p_run.add_argument("-v", "--verbose", action="store_true", help="Enable debug logs.")
    p_run.set_defaults(func=_cmd_run)

    p_disc = sub.add_parser("discover", help="Preview matching YouTube videos.")
    p_disc.add_argument("--config", default="config.toml", help="Path to config.toml")
    p_disc.add_argument("-v", "--verbose", action="store_true", help="Enable debug logs.")
    p_disc.set_defaults(func=_cmd_discover)

    p_voices = sub.add_parser("voices", help="List available Edge TTS voices.")
    p_voices.add_argument(
        "--locale", default="zh", help="Filter by locale prefix (default: zh)"
    )
    p_voices.add_argument("-v", "--verbose", action="store_true", help="Enable debug logs.")
    p_voices.set_defaults(func=_cmd_voices)

    p_rebuild = sub.add_parser(
        "rebuild-feed", help="Rebuild feed.xml and index.html from state.db."
    )
    p_rebuild.add_argument("--config", default="config.toml", help="Path to config.toml")
    p_rebuild.add_argument(
        "--no-publish",
        action="store_true",
        help="Write locally without committing/pushing or uploading.",
    )
    p_rebuild.add_argument("-v", "--verbose", action="store_true", help="Enable debug logs.")
    p_rebuild.set_defaults(func=_cmd_rebuild_feed)

    p_sched = sub.add_parser(
        "install-schedule",
        help="Generate macOS launchd plist and runner script for scheduled digests.",
    )
    p_sched.add_argument("--config", default="config.toml", help="Path to config.toml")
    p_sched.add_argument("--hour", type=int, default=8, help="Daily run hour (0-23)")
    p_sched.add_argument("--minute", type=int, default=0, help="Daily run minute (0-59)")
    p_sched.add_argument(
        "--dry-run", action="store_true", help="Print generated plist without writing files."
    )
    p_sched.add_argument("-v", "--verbose", action="store_true", help="Enable debug logs.")
    p_sched.set_defaults(func=_cmd_install_schedule)

    p_tts = sub.add_parser(
        "tts", help="Synthesize an MP3 directly from a Chinese script/transcript file."
    )
    p_tts.add_argument("--config", default="config.toml", help="Path to config.toml")
    p_tts.add_argument("--input", required=True, help="Path to input script/transcript file")
    p_tts.add_argument("--output", required=True, help="Path to output MP3 file")
    p_tts.add_argument(
        "--provider",
        choices=["edge_tts", "volcengine"],
        default=None,
        help="Override tts.provider from config.toml",
    )
    p_tts.add_argument("--voice", default=None, help="Override tts.voice from config.toml")
    p_tts.add_argument("-v", "--verbose", action="store_true", help="Enable debug logs.")
    p_tts.set_defaults(func=_cmd_tts)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(getattr(args, "verbose", False))
    try:
        return int(args.func(args))
    except Exception as exc:
        logging.getLogger("yt_podcast").error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
