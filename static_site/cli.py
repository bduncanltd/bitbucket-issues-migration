"""Command line entry point for the static site generator.

Reads an archive produced by ``bitbucket_export`` and writes an HTML site. Never
touches the network, so it can be re-run as often as you like.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from bitbucket_export.model import Archive

from .site import MissingAssetError, render_site

DEFAULT_SITE_ROOT = Path(".site")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="static-site",
        description="Generate a static HTML site from a Bitbucket issue archive.",
    )
    parser.add_argument("archive_dir", type=Path, help="Archive directory containing manifest.json.")
    parser.add_argument(
        "output_dir",
        type=Path,
        nargs="?",
        default=None,
        help="Where to write the site. Defaults to .site/<workspace>/<repo>, mirroring the archive layout.",
    )
    parser.add_argument(
        "--allow-missing-assets",
        action="store_true",
        help="Generate the site even if the manifest lists assets whose files are missing from the archive.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])

    try:
        archive = Archive.load(args.archive_dir)
    except (FileNotFoundError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1

    repository = archive.repository
    output_dir = args.output_dir or DEFAULT_SITE_ROOT / repository.workspace / repository.slug

    try:
        result = render_site(archive, output_dir, args.archive_dir, allow_missing_assets=args.allow_missing_assets)
    except MissingAssetError as error:
        print(str(error), file=sys.stderr)
        return 1

    for line in result.summary:
        print(line)
    return 0
