"""Per-repository configuration, read from .mca.ini."""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_FILE_KB = 200
DEFAULT_TEST_DIR = "tests"


@dataclass
class Config:
    """Runtime settings for the packer and the loop."""

    repo: Path
    max_file_bytes: int = DEFAULT_MAX_FILE_KB * 1024
    downloads_dir: Path | None = None
    extra_extensions: tuple[str, ...] = ()
    test_dir: str = DEFAULT_TEST_DIR


def load_config(repo: Path) -> Config:
    """Read .mca.ini from the repository root, ignoring unknown keys."""
    config = Config(repo=Path(repo))
    ini = config.repo / ".mca.ini"
    if not ini.is_file():
        return config

    parser = configparser.ConfigParser()
    parser.read(ini, encoding="utf-8")
    if not parser.has_section("mca"):
        return config

    section = parser["mca"]
    if "max_file_kb" in section:
        config.max_file_bytes = int(section["max_file_kb"]) * 1024
    if "downloads_dir" in section and section["downloads_dir"].strip():
        config.downloads_dir = Path(section["downloads_dir"].strip()).expanduser()
    if "extra_extensions" in section:
        config.extra_extensions = tuple(
            ext.strip().lstrip(".")
            for ext in section["extra_extensions"].replace("\n", ",").split(",")
            if ext.strip()
        )
    if "test_dir" in section:
        config.test_dir = section["test_dir"].strip() or DEFAULT_TEST_DIR
    return config
