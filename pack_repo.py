#!/usr/bin/env python3
"""Build the repository pack to attach to a Gemini Gem conversation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from minicodeassist.clipboard import find_downloads_dir
from minicodeassist.config import Config, ConfigError, load_config
from minicodeassist.packer import SecretFound, build_pack, pack_summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="target repository")
    parser.add_argument("--canary", action="store_true", help="insert three integrity markers")
    parser.add_argument("--mask", action="store_true", help="mask suspected secrets instead of refusing")
    parser.add_argument("--out", type=Path, help="custom output path")
    parser.add_argument("--ext", action="append", default=[], help="extra file extension to include")
    parser.add_argument("--no-downloads-copy", action="store_true", help="do not copy to Windows Downloads")
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    try:
        config: Config = load_config(repo)
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 2
    downloads = None if args.no_downloads_copy else find_downloads_dir()
    try:
        result = build_pack(
            repo,
            config,
            canary=args.canary,
            mask=args.mask,
            extra_extensions=tuple(args.ext),
            out=args.out,
            copy_to_downloads=not args.no_downloads_copy,
            downloads_dir=downloads,
        )
    except (RuntimeError, SecretFound) as error:
        print(str(error), file=sys.stderr)
        return 2

    print(pack_summary(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
