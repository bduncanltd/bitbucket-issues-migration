"""Command line entry point for the exporter.

This tool does one thing: pull a Bitbucket repository's issue tracker into an archive
directory. It has no idea what will read that archive afterwards — turning it into a
website, a Jira import, or a search index is somebody else's job.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlparse

from .acquire import AcquireOptions, acquire
from .bitbucket_client import BitbucketApiError, BitbucketClient, read_token
from .inline_images import (
    DEFAULT_AUTH_STATE,
    DEFAULT_CONCURRENCY,
    DEFAULT_LOGIN_URL,
    DEFAULT_REQUEST_TIMEOUT_MS,
    InlineImageSessionError,
    prepare_auth,
)
from .migration_logging import (
    append_migration_log,
    default_migration_log_path,
    describe_tool_revision,
)
from .model import Repository

DEFAULT_TOKEN_FILENAME = "BITBUCKET_API_TOKEN"
DEFAULT_ARCHIVE_ROOT = Path(".archive")
MIN_REPO_PARTS = 2


def configure_logging() -> None:
    """Send log output to stdout as bare lines, like a normal CLI tool."""

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)


EPILOG = """
examples:
  # once, to authorise inline-image downloads
  python -m bitbucket_export --prepare-auth

  # export into .archive/workspace/repo
  python -m bitbucket_export workspace/repo --email you@example.com

Set $BITBUCKET_EMAIL to drop --email too.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bitbucket-export",
        description="Export a Bitbucket issue tracker into a reusable archive.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "repository",
        nargs="?",
        help="Repository as workspace/repo, or a full https://bitbucket.org/workspace/repo URL.",
    )
    parser.add_argument(
        "--prepare-auth",
        action="store_true",
        help="Open a browser to save a Bitbucket session, then exit. Needed once before inline images can be fetched.",
    )
    parser.add_argument(
        "--email",
        default=os.getenv("BITBUCKET_EMAIL"),
        help="Atlassian account email for API-token auth. Defaults to $BITBUCKET_EMAIL.",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("BITBUCKET_API_TOKEN"),
        help="API token. Defaults to $BITBUCKET_API_TOKEN, else --token-file.",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=None,
        help=f"File holding the API token. Defaults to ./{DEFAULT_TOKEN_FILENAME} if present.",
    )
    parser.add_argument("--title", default=None, help="Archive title. Defaults to the repo full name.")
    parser.add_argument(
        "--auth-state",
        type=Path,
        default=DEFAULT_AUTH_STATE,
        help="Saved browser session used for inline images.",
    )
    parser.add_argument("--login-url", default=DEFAULT_LOGIN_URL, help="Login page opened by --prepare-auth.")
    parser.add_argument("--channel", default="chromium", help="Browser channel used by --prepare-auth.")
    parser.add_argument(
        "--skip-inline-images",
        action="store_true",
        help="Do not fetch inline images. They are still recorded as unresolved in the manifest.",
    )
    parser.add_argument(
        "--allow-missing-images",
        action="store_true",
        help="Exit 0 even when some assets could not be downloaded.",
    )
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--request-timeout-ms", type=int, default=DEFAULT_REQUEST_TIMEOUT_MS)
    parser.add_argument("--max-issues", type=int, default=None, help="Cap issues fetched, lowest ids first.")
    parser.add_argument(
        "--issue-id",
        type=int,
        action="append",
        default=[],
        help="Only fetch these issue ids. Repeatable.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore any existing manifest and rebuild it. Stored assets are still reused.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if args.prepare_auth:
        try:
            prepare_auth(auth_state_path=args.auth_state, login_url=args.login_url, channel=args.channel)
        except InlineImageSessionError as error:
            logging.error(str(error))
            return 1
        return 0

    if not args.repository:
        parser.error("a repository is required (or pass --prepare-auth)")

    if args.refresh and (args.issue_id or args.max_issues is not None):
        # --refresh discards the previous manifest, so a run limited to a subset would
        # save an archive containing only that subset and delete every other issue file.
        parser.error(
            "--refresh rebuilds the archive from exactly what this run fetches, so combining it "
            "with --issue-id or --max-issues would drop every other issue from the archive. "
            "To force-refetch specific issues, use --issue-id without --refresh."
        )

    return _run_export(args)


def default_archive_dir(repository: Repository) -> Path:
    """Where a repository's archive lives when no directory is given."""

    return DEFAULT_ARCHIVE_ROOT / repository.workspace / repository.slug


def _run_export(args: argparse.Namespace) -> int:
    revision_line = describe_tool_revision()

    # Credentials first: the archive path depends on the parsed repository, and there
    # is nowhere sensible to log a failure until we know it.
    try:
        repository, token = _resolve_credentials(args)
    except (ValueError, OSError) as error:
        logging.error(str(error))
        return 1

    archive_dir = default_archive_dir(repository)
    log_path = default_migration_log_path(archive_dir)
    logging.info(f"Archiving {repository.full_name} into {archive_dir}")

    options = AcquireOptions(
        archive_dir=archive_dir,
        repository=repository,
        title=args.title or "",
        auth_state_path=args.auth_state,
        skip_inline_images=args.skip_inline_images,
        concurrency=args.concurrency,
        request_timeout_ms=args.request_timeout_ms,
        max_issues=args.max_issues,
        issue_ids=set(args.issue_id),
        refresh=args.refresh,
    )
    client = BitbucketClient(repository.workspace, repository.slug, args.email, token)

    try:
        result = acquire(client, options)
    except (InlineImageSessionError, BitbucketApiError) as error:
        return _fail(log_path, revision_line, str(error))

    # The summary is the run's actual output, not narration about it — print, so it
    # stays even if a host application reconfigures logging.
    summary = [revision_line, *result.summary]
    for line in summary:
        print(line)
    append_migration_log(log_path, [sys.executable, *sys.argv], summary, status="success")

    incomplete = result.failed_images + result.failed_attachments
    if incomplete and not args.allow_missing_images:
        logging.error(
            f"{incomplete} asset(s) could not be downloaded; see unresolved_assets in the manifest.\n"
            "Re-run to retry just those, or pass --allow-missing-images to accept the archive as-is."
        )
        return 1
    return 0


def parse_repository(repository: str) -> tuple[str, str]:
    """Accept 'workspace/repo' or a full Bitbucket URL and return (workspace, repo)."""

    candidate = repository.strip()
    if candidate.startswith(("http://", "https://")):
        candidate = urlparse(candidate).path
    parts = [part for part in candidate.strip("/").split("/") if part]
    if len(parts) < MIN_REPO_PARTS:
        raise ValueError(f"Could not parse workspace/repo from: {repository!r}")
    return parts[0], parts[1]


def _resolve_credentials(args: argparse.Namespace) -> tuple[Repository, str]:
    """Validate every credential up front so a bad run stops before any fetching."""

    workspace, slug = parse_repository(args.repository)
    if not args.email:
        raise ValueError("No email provided. Pass --email or set $BITBUCKET_EMAIL.")

    token_file = args.token_file
    if not args.token and token_file is None:
        default_file = Path.cwd() / DEFAULT_TOKEN_FILENAME
        if default_file.exists():
            token_file = default_file

    try:
        token = read_token(args.token, token_file)
    except BitbucketApiError as error:
        raise ValueError(str(error)) from error

    return Repository(workspace=workspace, slug=slug), token


def _fail(log_path: Path | None, revision_line: str, message: str) -> int:
    logging.error(message)
    append_migration_log(log_path, [sys.executable, *sys.argv], [revision_line, message], status="failed")
    return 1
