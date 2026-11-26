"""Publishing backends for podcast MP3 audio, RSS feed.xml, and index.html."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
from typing import Protocol

from yt_podcast.config import AppConfig


@dataclass(slots=True)
class PublishResult:
    feed_url: str
    page_url: str
    audio_url: str
    details: str


class Publisher(Protocol):
    def audio_url_for(self, audio_filename: str) -> str:
        """Compute the public enclosure URL for a generated MP3 file."""

    def publish(
        self,
        mp3_path: Path | None,
        audio_filename: str | None,
        feed_xml: str,
        index_html: str,
        commit_message: str,
    ) -> PublishResult:
        """Publish audio + feed.xml + index.html to the target backend."""


class LocalPublisher:
    """Writes episodes/*.mp3, feed.xml, and index.html to output_dir."""

    def __init__(self, output_dir: Path, base_url: str) -> None:
        self.output_dir = Path(output_dir)
        self.base_url = base_url.rstrip("/")

    def audio_url_for(self, audio_filename: str) -> str:
        return f"{self.base_url}/episodes/{audio_filename}"

    def _write_files(
        self,
        mp3_path: Path | None,
        audio_filename: str | None,
        feed_xml: str,
        index_html: str,
    ) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        episodes_dir = self.output_dir / "episodes"
        episodes_dir.mkdir(parents=True, exist_ok=True)

        if mp3_path is not None and audio_filename:
            dest_mp3 = episodes_dir / audio_filename
            if mp3_path.resolve() != dest_mp3.resolve():
                shutil.copy2(mp3_path, dest_mp3)

        (self.output_dir / "feed.xml").write_text(feed_xml, encoding="utf-8")
        (self.output_dir / "index.html").write_text(index_html, encoding="utf-8")

    def publish(
        self,
        mp3_path: Path | None,
        audio_filename: str | None,
        feed_xml: str,
        index_html: str,
        commit_message: str,
    ) -> PublishResult:
        self._write_files(mp3_path, audio_filename, feed_xml, index_html)
        audio_url = self.audio_url_for(audio_filename) if audio_filename else ""
        return PublishResult(
            feed_url=f"{self.base_url}/feed.xml",
            page_url=f"{self.base_url}/",
            audio_url=audio_url,
            details=f"Wrote static podcast site to {self.output_dir}",
        )


class GitHubPagesPublisher(LocalPublisher):
    """Writes static site files into output_dir and commits/pushes via git."""

    def __init__(
        self,
        repo_root: Path,
        output_dir: Path,
        base_url: str,
        auto_push: bool = False,
        git_remote: str = "origin",
        git_branch: str = "main",
    ) -> None:
        super().__init__(output_dir=output_dir, base_url=base_url)
        self.repo_root = Path(repo_root)
        self.auto_push = auto_push
        self.git_remote = git_remote
        self.git_branch = git_branch

    def _run_git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(self.repo_root),
            check=True,
            capture_output=True,
            text=True,
        )

    def publish(
        self,
        mp3_path: Path | None,
        audio_filename: str | None,
        feed_xml: str,
        index_html: str,
        commit_message: str,
    ) -> PublishResult:
        self._write_files(mp3_path, audio_filename, feed_xml, index_html)

        self._run_git("add", str(self.output_dir))
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(self.repo_root),
            check=False,
        )
        if diff.returncode != 0:
            self._run_git("commit", "-m", commit_message)
            status_note = f"Committed to git ({self.output_dir})"
        else:
            status_note = "No git changes to commit"

        if self.auto_push:
            self._run_git("push", self.git_remote, self.git_branch)
            status_note += f" and pushed to {self.git_remote}/{self.git_branch}"

        audio_url = self.audio_url_for(audio_filename) if audio_filename else ""
        return PublishResult(
            feed_url=f"{self.base_url}/feed.xml",
            page_url=f"{self.base_url}/",
            audio_url=audio_url,
            details=status_note,
        )


class S3Publisher(LocalPublisher):
    """Uploads MP3, feed.xml, and index.html to S3 / Cloudflare R2 via boto3."""

    def __init__(
        self,
        output_dir: Path,
        base_url: str,
        bucket: str,
        prefix: str = "",
        endpoint_url: str | None = None,
        region_name: str = "auto",
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
    ) -> None:
        super().__init__(output_dir=output_dir, base_url=base_url)
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.endpoint_url = endpoint_url or None
        self.region_name = region_name
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key

    def _s3_key(self, rel_path: str) -> str:
        clean = rel_path.lstrip("/")
        return f"{self.prefix}/{clean}" if self.prefix else clean

    def publish(
        self,
        mp3_path: Path | None,
        audio_filename: str | None,
        feed_xml: str,
        index_html: str,
        commit_message: str,
    ) -> PublishResult:
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError(
                "boto3 is required for S3/R2 publishing. Install it with: pip install boto3"
            ) from exc

        # Keep local output_dir synchronized as well
        self._write_files(mp3_path, audio_filename, feed_xml, index_html)

        client_kwargs: dict[str, object] = {"region_name": self.region_name}
        if self.endpoint_url:
            client_kwargs["endpoint_url"] = self.endpoint_url
        if self.access_key_id and self.secret_access_key:
            client_kwargs["aws_access_key_id"] = self.access_key_id
            client_kwargs["aws_secret_access_key"] = self.secret_access_key

        client = boto3.client("s3", **client_kwargs)

        if mp3_path is not None and audio_filename:
            client.upload_file(
                str(mp3_path),
                self.bucket,
                self._s3_key(f"episodes/{audio_filename}"),
                ExtraArgs={"ContentType": "audio/mpeg"},
            )

        client.put_object(
            Bucket=self.bucket,
            Key=self._s3_key("feed.xml"),
            Body=feed_xml.encode("utf-8"),
            ContentType="application/rss+xml; charset=utf-8",
        )
        client.put_object(
            Bucket=self.bucket,
            Key=self._s3_key("index.html"),
            Body=index_html.encode("utf-8"),
            ContentType="text/html; charset=utf-8",
        )

        audio_url = self.audio_url_for(audio_filename) if audio_filename else ""
        return PublishResult(
            feed_url=f"{self.base_url}/feed.xml",
            page_url=f"{self.base_url}/",
            audio_url=audio_url,
            details=f"Uploaded podcast artifacts to s3://{self.bucket}/{self.prefix}".rstrip("/"),
        )


def create_publisher(config: AppConfig) -> Publisher:
    """Build the configured publishing backend."""
    pub = config.publishing
    base_url = config.podcast.base_url
    if pub.backend == "local":
        return LocalPublisher(output_dir=pub.output_dir, base_url=base_url)
    if pub.backend == "github_pages":
        return GitHubPagesPublisher(
            repo_root=config.project_root,
            output_dir=pub.output_dir,
            base_url=base_url,
            auto_push=pub.auto_push,
            git_remote=pub.git_remote,
            git_branch=pub.git_branch,
        )
    if pub.backend == "s3":
        bucket = os.environ.get("S3_BUCKET", "").strip()
        if not bucket:
            raise RuntimeError(
                "S3_BUCKET environment variable must be set when publishing.backend = 's3'."
            )
        return S3Publisher(
            output_dir=pub.output_dir,
            base_url=base_url,
            bucket=bucket,
            prefix=os.environ.get("S3_PREFIX", ""),
            endpoint_url=os.environ.get("S3_ENDPOINT_URL"),
            region_name=os.environ.get("S3_REGION", "auto"),
            access_key_id=os.environ.get("S3_ACCESS_KEY_ID"),
            secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY"),
        )
    raise ValueError(f"Unsupported publishing backend: {pub.backend}")
