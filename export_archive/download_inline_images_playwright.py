"""Download missing inline images using Playwright-authenticated requests.

Typical workflow:

1. Save authenticated browser state once:
   python misc/bitbucket_issue_exporter/download_inline_images_playwright.py \
       --prepare-auth \
       --auth-state ~/.cache/bitbucket/playwright-auth.json

2. Download the missing inline images listed by the archive exporter:
   python misc/bitbucket_issue_exporter/download_inline_images_playwright.py \
       --csv ~/bb_projects/<repo-slug>/bitbucket_issue_archive_initial/missing-inline-images.csv \
       --auth-state ~/.cache/bitbucket/playwright-auth.json \
       --output-dir ~/bb_projects/<repo-slug>/missing_aws_images \
       --issue-id 62

3. Re-run the archive exporter with:
   --inline-image-dir ~/bb_projects/<repo-slug>/missing_aws_images
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import sys
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

try:
    from export_archive.migration_logging import (
        append_migration_log,
        default_migration_log_path,
        describe_tool_revision,
    )
except ModuleNotFoundError:  # pragma: no cover - allows running this file directly
    from migration_logging import (  # type: ignore
        append_migration_log,
        default_migration_log_path,
        describe_tool_revision,
    )

DEFAULT_AUTH_STATE = Path.home() / ".cache" / "bitbucket-playwright" / "auth-state.json"
DEFAULT_LOGIN_URL = "https://bitbucket.org/account/signin/"
DEFAULT_CONCURRENCY = 2
DEFAULT_REQUEST_TIMEOUT_MS = 120_000
MISSING_IMAGE_CSV_FIELDNAMES = [
    "issue_id",
    "issue_title",
    "source_type",
    "comment_id",
    "user",
    "created_on",
    "page_path",
    "original_issue_url",
    "image_filename",
    "image_url",
]
DOWNLOAD_RESULTS_EXTRA_FIELDNAMES = [
    "status",
    "output_path",
    "note",
]


@dataclass(frozen=True)
class MissingImageRow:
    issue_id: int | None
    issue_title: str
    source_type: str
    comment_id: str
    user: str
    created_on: str
    page_path: str
    original_issue_url: str
    image_filename: str
    image_url: str


@dataclass
class IssueDownloadTarget:
    issue_id: int | None
    issue_title: str
    issue_url: str
    rows: list[MissingImageRow]


@dataclass
class DownloadResult:
    row: MissingImageRow
    status: str
    output_path: str
    note: str


@dataclass(frozen=True)
class DownloadSettings:
    output_dir: Path
    auth_state_path: Path
    overwrite: bool
    request_timeout_ms: int
    lock_registry: PathLockRegistry


class PathLockRegistry:
    """Provides one shared lock per output path to avoid concurrent file clashes."""

    def __init__(self) -> None:
        self._registry_lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def lock_for(self, output_path: Path) -> threading.Lock:
        output_key = str(output_path)
        with self._registry_lock:
            lock = self._locks.get(output_key)
            if lock is None:
                lock = threading.Lock()
                self._locks[output_key] = lock
            return lock


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv",
        type=Path,
        help="Path to missing-inline-images.csv produced by export_archive.py.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory where downloaded images will be written.",
    )
    parser.add_argument(
        "--auth-state",
        type=Path,
        default=DEFAULT_AUTH_STATE,
        help=f"Playwright storage state JSON path. Default: {DEFAULT_AUTH_STATE}",
    )
    parser.add_argument(
        "--prepare-auth",
        action="store_true",
        help="Open a headed browser so you can log in manually, then save auth state to --auth-state.",
    )
    parser.add_argument(
        "--login-url",
        default=DEFAULT_LOGIN_URL,
        help=f"Login page to open in --prepare-auth mode. Default: {DEFAULT_LOGIN_URL}",
    )
    parser.add_argument(
        "--issue-id",
        type=int,
        action="append",
        default=[],
        help="Optional issue id filter. Can be passed multiple times.",
    )
    parser.add_argument(
        "--max-issues",
        type=int,
        default=None,
        help="Optional limit for how many distinct issues to process.",
    )
    parser.add_argument(
        "--channel",
        default="chrome",
        help="Chromium channel to use, e.g. chrome or msedge. Default: chrome",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite files in --output-dir if they already exist with different contents.",
    )
    parser.add_argument(
        "--results-csv",
        type=Path,
        default=None,
        help="Optional explicit path for the download results CSV. Defaults to <output-dir>/download-results.csv.",
    )
    parser.add_argument(
        "--request-timeout-ms",
        type=int,
        default=DEFAULT_REQUEST_TIMEOUT_MS,
        help=f"Per-image request timeout in milliseconds. Default: {DEFAULT_REQUEST_TIMEOUT_MS}",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"How many download workers to run in parallel. Default: {DEFAULT_CONCURRENCY}",
    )
    parser.add_argument(
        "--retry-failed-only",
        action="store_true",
        help="When reading a previous download-results.csv file, only retry rows where status=download_failed.",
    )
    args = parser.parse_args(argv)

    if not args.prepare_auth and args.csv is None:
        parser.error("--csv is required unless --prepare-auth is used")
    if not args.prepare_auth and args.output_dir is None:
        parser.error("--output-dir is required unless --prepare-auth is used")

    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    migration_log_path = default_migration_log_path(args.output_dir)
    revision_line = describe_tool_revision()

    if args.prepare_auth:
        return prepare_auth_state(args.auth_state, args.login_url, args.channel)

    try:
        rows = load_missing_inline_image_csv(args.csv, retry_failed_only=args.retry_failed_only)
    except ValueError as error:
        message = str(error)
        print(message, file=sys.stderr)
        append_migration_log(
            migration_log_path,
            [sys.executable, *sys.argv],
            [revision_line, message],
            status="failed",
        )
        return 1
    targets = build_issue_targets(rows, issue_ids=set(args.issue_id), max_issues=args.max_issues)
    if not targets:
        message = "No matching issue rows found in the CSV."
        print(message)
        append_migration_log(
            migration_log_path,
            [sys.executable, *sys.argv],
            [revision_line, message],
            status="success",
        )
        return 0

    missing_context_count = sum(1 for row in rows if row.issue_id is None)
    missing_context_warning = None
    if missing_context_count:
        missing_context_warning = (
            f"Warning: {missing_context_count} image rows had missing issue context in the input CSV. "
            "They will still download, but the archive should be re-exported so the missing-image report keeps "
            "full ticket context."
        )
        print(missing_context_warning)

    if not args.auth_state.exists():
        summary_lines = [
            revision_line,
            f"Auth state file not found: {args.auth_state}",
            "Run with --prepare-auth first to save a logged-in browser state.",
        ]
        print("\n".join(summary_lines), file=sys.stderr)
        append_migration_log(
            migration_log_path,
            [sys.executable, *sys.argv],
            summary_lines,
            status="failed",
        )
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    results = download_issue_images(
        targets=targets,
        settings=DownloadSettings(
            output_dir=args.output_dir,
            auth_state_path=args.auth_state,
            overwrite=args.overwrite,
            request_timeout_ms=args.request_timeout_ms,
            lock_registry=PathLockRegistry(),
        ),
        concurrency=args.concurrency,
    )

    results_csv = args.results_csv or args.output_dir / "download-results.csv"
    write_results_csv(results_csv, results)

    successful = sum(result.status in {"downloaded", "already_exists"} for result in results)
    missing = sum(result.status == "download_failed" for result in results)
    failed = len(results) - successful - missing

    summary_lines = [
        revision_line,
        f"Processed {len(targets)} issues",
        f"Handled {len(results)} image rows",
        f"Successful or already present: {successful}",
        f"Still missing or failed: {missing}",
        f"Other failures: {failed}",
        f"Results CSV: {results_csv}",
    ]
    if missing_context_warning is not None:
        summary_lines.append(missing_context_warning)
    for line in summary_lines:
        print(line)
    append_migration_log(
        migration_log_path,
        [sys.executable, *sys.argv],
        summary_lines,
        status="success",
    )
    return 0


def prepare_auth_state(auth_state_path: Path, login_url: str, channel: str) -> int:
    sync_playwright = import_sync_playwright()
    auth_state_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel, headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(login_url, wait_until="load")
        print()
        print(f"Opened login page: {login_url}")
        print("Sign in to Bitbucket in the browser window.")
        print("When you are fully logged in, press Enter here to save auth state.")
        input()
        context.storage_state(path=str(auth_state_path))
        context.close()
        browser.close()

    print(f"Saved auth state to {auth_state_path}")
    return 0


def download_issue_images(
    targets: list[IssueDownloadTarget],
    settings: DownloadSettings,
    concurrency: int,
) -> list[DownloadResult]:
    worker_count = max(1, concurrency)
    target_batches = split_targets_for_workers(targets, worker_count)

    if len(target_batches) == 1:
        results = download_target_batch(
            targets=target_batches[0],
            settings=settings,
        )
    else:
        results = []
        with ThreadPoolExecutor(max_workers=len(target_batches), thread_name_prefix="bb-inline-image") as executor:
            futures = [
                executor.submit(
                    download_target_batch,
                    targets=batch,
                    settings=settings,
                )
                for batch in target_batches
            ]
            for future in futures:
                results.extend(future.result())

    return sorted(
        results,
        key=lambda result: (
            result.row.issue_id is None,
            result.row.issue_id or -1,
            result.row.image_filename,
            result.row.image_url,
        ),
    )


def split_targets_for_workers(
    targets: list[IssueDownloadTarget],
    concurrency: int,
) -> list[list[IssueDownloadTarget]]:
    worker_count = max(1, concurrency)
    batches: list[list[IssueDownloadTarget]] = [[] for _ in range(worker_count)]
    for index, target in enumerate(targets):
        batches[index % worker_count].append(target)
    return [batch for batch in batches if batch]


def download_target_batch(
    targets: list[IssueDownloadTarget],
    settings: DownloadSettings,
) -> list[DownloadResult]:
    sync_playwright = import_sync_playwright()
    results: list[DownloadResult] = []
    worker_name = threading.current_thread().name

    with sync_playwright() as playwright:
        request_context = playwright.request.new_context(storage_state=str(settings.auth_state_path))
        try:
            for target in targets:
                if not target.rows:
                    continue

                print(
                    f"[{worker_name}] Downloading images for {describe_target(target)}",
                    flush=True,
                )

                for row in target.rows:
                    output_path = settings.output_dir / sanitize_filename(row.image_filename)
                    output_lock = settings.lock_registry.lock_for(output_path)
                    with output_lock:
                        status, note = save_url_to_path(
                            request_context=request_context,
                            source_url=row.image_url,
                            output_path=output_path,
                            overwrite=settings.overwrite,
                            request_timeout_ms=settings.request_timeout_ms,
                        )
                    results.append(
                        DownloadResult(
                            row=row,
                            status=status,
                            output_path=str(output_path),
                            note=note,
                        )
                    )
        finally:
            request_context.dispose()

    return results


def load_missing_inline_image_csv(csv_path: Path, retry_failed_only: bool = False) -> list[MissingImageRow]:
    with csv_path.open(encoding="utf-8", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        fieldnames = set(reader.fieldnames or [])
        if retry_failed_only and "status" not in fieldnames:
            raise ValueError("--retry-failed-only requires a CSV with a status column, such as download-results.csv.")
        rows = []
        for raw_row in reader:
            if retry_failed_only and raw_row.get("status") != "download_failed":
                continue
            rows.append(
                MissingImageRow(
                    issue_id=parse_optional_int(raw_row.get("issue_id")),
                    issue_title=raw_row.get("issue_title", ""),
                    source_type=raw_row.get("source_type", "retry"),
                    comment_id=raw_row.get("comment_id", ""),
                    user=raw_row.get("user", ""),
                    created_on=raw_row.get("created_on", ""),
                    page_path=raw_row.get("page_path", ""),
                    original_issue_url=raw_row.get("original_issue_url") or raw_row.get("issue_url", ""),
                    image_filename=raw_row["image_filename"],
                    image_url=raw_row.get("image_url") or raw_row.get("source_url", ""),
                )
            )
    return rows


def parse_optional_int(value: str | None) -> int | None:
    if not value:
        return None
    return int(value)


def build_issue_targets(
    rows: list[MissingImageRow],
    issue_ids: set[int] | None = None,
    max_issues: int | None = None,
) -> list[IssueDownloadTarget]:
    grouped: dict[tuple[int | None, str], list[MissingImageRow]] = {}
    for row in rows:
        if issue_ids and row.issue_id not in issue_ids:
            continue
        key = (row.issue_id, row.original_issue_url)
        grouped.setdefault(key, []).append(row)

    ordered_targets = [
        IssueDownloadTarget(
            issue_id=issue_id,
            issue_title=issue_rows[0].issue_title,
            issue_url=issue_url,
            rows=sorted(issue_rows, key=lambda item: (item.source_type, item.comment_id, item.image_filename)),
        )
        for (issue_id, issue_url), issue_rows in sorted(
            grouped.items(),
            key=lambda item: (item[0][0] is None, item[0][0] or -1, item[0][1]),
        )
    ]

    if max_issues is not None:
        return ordered_targets[:max_issues]
    return ordered_targets


def import_sync_playwright():
    try:
        sync_api = importlib.import_module("playwright.sync_api")
    except ImportError as error:
        raise SystemExit(
            "Playwright is not installed.\n"
            "Install it with:\n"
            "  pip install playwright\n"
            "  python -m playwright install chrome"
        ) from error

    return sync_api.sync_playwright


def describe_target(target: IssueDownloadTarget) -> str:
    if target.issue_id is None:
        if target.issue_url:
            return f"image rows with missing issue context (URL: {target.issue_url})"
        return "image rows with missing issue context"
    return f"issue #{target.issue_id}: {target.issue_url}"


def save_url_to_path(
    request_context,
    source_url: str,
    output_path: Path,
    overwrite: bool,
    request_timeout_ms: int,
) -> tuple[str, str]:
    try:
        response = request_context.get(source_url, fail_on_status_code=False, timeout=request_timeout_ms)
    except Exception as error:  # pragma: no cover - depends on runtime/network failures
        return "download_failed", str(error)

    if not response.ok:
        return "download_failed", f"HTTP {response.status}"

    content_type = (response.headers.get("content-type") or "").lower()
    if content_type and not (content_type.startswith("image/") or content_type.startswith("application/octet-stream")):
        return "download_failed", f"Unexpected content type: {content_type}"

    body = response.body()
    if output_path.exists():
        existing_hash = sha256_file(output_path)
        new_hash = hashlib.sha256(body).hexdigest()
        if existing_hash == new_hash:
            return "already_exists", "Matching file already present"
        if not overwrite:
            return "conflict_existing_file", "Existing file differs and --overwrite was not set"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(body)
    return "downloaded", f"Saved {len(body)} bytes"


def write_results_csv(csv_path: Path, results: Sequence[DownloadResult]) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = MISSING_IMAGE_CSV_FIELDNAMES + DOWNLOAD_RESULTS_EXTRA_FIELDNAMES
    with csv_path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        for result in results:
            writer.writerow(
                {
                    "issue_id": "" if result.row.issue_id is None else result.row.issue_id,
                    "issue_title": result.row.issue_title,
                    "source_type": result.row.source_type,
                    "comment_id": result.row.comment_id,
                    "user": result.row.user,
                    "created_on": result.row.created_on,
                    "page_path": result.row.page_path,
                    "original_issue_url": result.row.original_issue_url,
                    "image_filename": result.row.image_filename,
                    "image_url": result.row.image_url,
                    "status": result.status,
                    "output_path": result.output_path,
                    "note": result.note,
                }
            )


def derive_url_filename(url: str) -> str:
    parsed = urlparse(url)
    return unquote(Path(parsed.path).name)


def sanitize_filename(filename: str) -> str:
    sanitized = "".join(char if char not in '\\/:*?"<>|' else "_" for char in filename).strip(" .")
    return sanitized or "downloaded-asset"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
