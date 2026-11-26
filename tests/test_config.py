from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from yt_podcast.config import load_config, load_dotenv


class ConfigTests(unittest.TestCase):
    def test_load_config_and_resolve_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_file = root / "config.toml"
            cfg_file.write_text(
                """
[discovery]
channels = ["@TestChan"]
interests = ["AI agents"]
lookback_hours = 48
max_videos_total = 4

[llm]
provider = "antigravity"
target_minutes = 15
chars_per_minute = 240

[podcast]
title = "Test Show"
base_url = "https://user.github.io/pod/"

[publishing]
backend = "local"
output_dir = "public"

[paths]
state_db = "data/test.db"
staged_dir = "data/staged"
work_dir = "data/work"
""",
                encoding="utf-8",
            )
            env_file = root / ".env"
            env_file.write_text("S3_BUCKET='my-bucket'\n", encoding="utf-8")

            old_val = os.environ.pop("S3_BUCKET", None)
            try:
                cfg = load_config(cfg_file)
                self.assertEqual(os.environ.get("S3_BUCKET"), "my-bucket")
            finally:
                if old_val is not None:
                    os.environ["S3_BUCKET"] = old_val
                else:
                    os.environ.pop("S3_BUCKET", None)

            self.assertEqual(cfg.project_root, root.resolve())
            self.assertEqual(cfg.publishing.output_dir, (root / "public").resolve())
            self.assertEqual(cfg.paths.state_db, (root / "data/test.db").resolve())
            self.assertEqual(cfg.podcast.base_url, "https://user.github.io/pod")
            self.assertEqual(cfg.llm.target_chars, 3600)

    def test_dotenv_does_not_override_existing_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("YT_PODCAST_TEST_VAR=from_file\n", encoding="utf-8")
            os.environ["YT_PODCAST_TEST_VAR"] = "already_set"
            try:
                load_dotenv(env_file)
                self.assertEqual(os.environ["YT_PODCAST_TEST_VAR"], "already_set")
            finally:
                os.environ.pop("YT_PODCAST_TEST_VAR", None)

    def test_invalid_provider_raises_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "config.toml"
            cfg_file.write_text('[llm]\nprovider = "invalid_llm"\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(cfg_file)


if __name__ == "__main__":
    unittest.main()
