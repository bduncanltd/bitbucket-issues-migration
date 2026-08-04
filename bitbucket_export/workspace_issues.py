"""Table of every repository in a workspace whose issue tracker has issues.

Answers "which repositories are worth exporting?" before any per-repository export
runs: one row per repository with issues, listed alphabetically by repository.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .bitbucket_client import API_ROOT, BitbucketApiError, BitbucketClient, read_token
from .cli import DEFAULT_TOKEN_FILENAME


@dataclass(frozen=True)
class RepoIssueCount:
    project_key: str
    project_name: str
    repo_slug: str
    issue_count: int


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bitbucket-workspace-issues",
        description="List every repository in a workspace that has issues, with issue counts.",
    )
    parser.add_argument("workspace", help="Bitbucket workspace id, e.g. myworkspace.")
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
    return parser


def fetch_issue_counts(client: BitbucketClient, workspace: str) -> list[RepoIssueCount]:
    """Count issues in every repository of ``workspace`` whose tracker holds any."""

    counts = []
    for repo in client.paginate_url(f"{API_ROOT}/repositories/{workspace}?pagelen=100"):
        if not repo["has_issues"]:
            continue
        slug = repo["slug"]
        # pagelen=1 keeps the response tiny; only the collection's total size matters.
        issues = client.get_json(f"{API_ROOT}/repositories/{workspace}/{slug}/issues?pagelen=1")
        if issues["size"] == 0:
            continue
        counts.append(
            RepoIssueCount(
                project_key=repo["project"]["key"],
                project_name=repo["project"]["name"],
                repo_slug=slug,
                issue_count=issues["size"],
            )
        )
    return sorted(counts, key=lambda count: count.repo_slug.lower())


def render_table(counts: list[RepoIssueCount]) -> str:
    headers = ("Project", "Project name", "Repository", "Issues")
    rows = [(count.project_key, count.project_name, count.repo_slug, str(count.issue_count)) for count in counts]
    widths = [max(len(cell) for cell in column) for column in zip(headers, *rows, strict=True)]

    def line(cells: tuple[str, str, str, str]) -> str:
        # The count column is right-aligned; every other column is left-aligned.
        aligned = [cell.ljust(width) for cell, width in zip(cells[:-1], widths[:-1], strict=True)]
        aligned.append(cells[-1].rjust(widths[-1]))
        return "  ".join(aligned).rstrip()

    total_issues = sum(count.issue_count for count in counts)
    rule = "-" * (sum(widths) + 2 * (len(widths) - 1))
    summary = f"{len(counts)} repositories, {total_issues} issues"
    return "\n".join([line(headers), *[line(row) for row in rows], rule, summary])


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if not args.email:
        print("No email provided. Pass --email or set $BITBUCKET_EMAIL.", file=sys.stderr)
        return 1

    token_file = args.token_file
    if not args.token and token_file is None:
        default_file = Path.cwd() / DEFAULT_TOKEN_FILENAME
        if default_file.exists():
            token_file = default_file

    try:
        token = read_token(args.token, token_file)
    except (BitbucketApiError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1

    # Only workspace-level absolute URLs are used, so the repo slug is never part of a request.
    client = BitbucketClient(args.workspace, "", args.email, token)
    try:
        counts = fetch_issue_counts(client, args.workspace)
    except BitbucketApiError as error:
        print(str(error), file=sys.stderr)
        return 1

    if not counts:
        print(f"No repositories with issues in workspace '{args.workspace}'.")
        return 0

    print(render_table(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
