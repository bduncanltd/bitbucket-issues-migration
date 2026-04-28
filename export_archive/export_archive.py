"""Generate a static HTML archive from a Bitbucket issues export."""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from export_archive.migration_logging import (
        append_migration_log,
        default_migration_log_path,
        describe_tool_revision,
    )
    from export_archive.rendering import (
        IssuePageData,
        comment_sort_key,
        derive_url_filename,
        log_sort_key,
        render_index_page,
        render_issue_page,
    )
except ModuleNotFoundError:  # pragma: no cover - allows running this file directly
    from migration_logging import (  # type: ignore
        append_migration_log,
        default_migration_log_path,
        describe_tool_revision,
    )
    from rendering import (  # type: ignore
        IssuePageData,
        comment_sort_key,
        derive_url_filename,
        log_sort_key,
        render_index_page,
        render_issue_page,
    )

HTTP_TIMEOUT_SECONDS = 30
ISSUE_LINK_RE = re.compile(r"https://bitbucket\.org/[^/]+/[^/]+/issues/(\d+)(?:/[^\s)]*)?$")
STYLE_CSS_PATH = Path(__file__).resolve().parent / "assets" / "style.css"


class AssetLocalizer:
    """Copies referenced assets into the output archive."""

    def __init__(
        self,
        output_dir: Path,
        attachment_url_map: dict[str, str],
        inline_image_dirs: Sequence[Path] | None = None,
        download_inline_images: bool = False,
        copy_assets: bool = True,
    ) -> None:
        self.output_dir = output_dir
        self.copy_assets = copy_assets
        self.inline_output_dir = output_dir / "assets" / "inline-images"
        if self.copy_assets:
            self.inline_output_dir.mkdir(parents=True, exist_ok=True)
        self.attachment_url_map = attachment_url_map
        self.inline_image_dirs = list(inline_image_dirs or [])
        self.download_inline_images = download_inline_images
        self.inline_image_map: dict[str, str] = {}
        self.inline_image_source_map = self._build_source_map(self.inline_image_dirs)
        self.missing_inline_images: set[str] = set()
        self.missing_inline_image_records: list[dict[str, str]] = []
        self._missing_inline_image_record_keys: set[tuple[str, str, str, str]] = set()
        self.download_errors: list[str] = []
        self.downloaded_image_count = 0
        self.copied_local_image_count = 0
        self.issue_ids: set[int] = set()

    @staticmethod
    def _build_source_map(source_dirs: Sequence[Path]) -> dict[str, Path]:
        source_map: dict[str, Path] = {}
        for source_dir in source_dirs:
            if not source_dir.exists():
                continue

            for path in iter_files_safe(source_dir):
                if not path.is_file():
                    continue

                source_map.setdefault(path.name, path)

        return source_map

    def set_issue_ids(self, issue_ids: Iterable[int]) -> None:
        self.issue_ids = set(issue_ids)

    def localize_link(self, url: str) -> str:
        if url in self.attachment_url_map:
            return self.attachment_url_map[url]

        issue_match = ISSUE_LINK_RE.match(url)
        if issue_match is not None:
            issue_id = int(issue_match.group(1))
            if issue_id in self.issue_ids:
                return f"{issue_id}.html"

        return url

    def localize_inline_image(self, url: str, context: dict[str, str] | None = None) -> str | None:
        if url in self.inline_image_map:
            return self.inline_image_map[url]

        basename = derive_url_filename(url)
        source_path = self.inline_image_source_map.get(basename)

        if source_path is not None:
            relative_path = self._store_inline_asset(source_path, basename)
            self.inline_image_map[url] = relative_path
            self.copied_local_image_count += 1
            return relative_path

        if self.download_inline_images:
            downloaded_path = self._download_inline_image(url, basename)
            if downloaded_path is not None:
                relative_path = self._copy_inline_asset(downloaded_path, basename, remove_source=True)
                self.inline_image_map[url] = relative_path
                self.downloaded_image_count += 1
                return relative_path

        self.missing_inline_images.add(url)
        self._record_missing_inline_image(url, context)
        return None

    def _store_inline_asset(self, source_path: Path, original_name: str) -> str:
        if not self.copy_assets:
            return f"assets/inline-images/{sanitize_filename(original_name)}"
        return self._copy_inline_asset(source_path, original_name)

    def _record_missing_inline_image(self, url: str, context: dict[str, str] | None) -> None:
        context_data = context or {}
        record = {
            "issue_id": str(context_data.get("issue_id") or ""),
            "issue_title": context_data.get("issue_title") or "",
            "source_type": context_data.get("source_type") or "",
            "comment_id": str(context_data.get("comment_id") or ""),
            "user": context_data.get("user") or "",
            "created_on": context_data.get("created_on") or "",
            "page_path": context_data.get("page_path") or "",
            "original_issue_url": context_data.get("original_issue_url") or "",
            "image_filename": derive_url_filename(url),
            "image_url": url,
        }
        record_key = (
            record["image_url"],
            record["issue_id"],
            record["source_type"],
            record["comment_id"],
        )
        if record_key in self._missing_inline_image_record_keys:
            return

        self._missing_inline_image_record_keys.add(record_key)
        self.missing_inline_image_records.append(record)

    def _copy_inline_asset(self, source_path: Path, original_name: str, remove_source: bool = False) -> str:
        destination = unique_destination(self.inline_output_dir, sanitize_filename(original_name))
        shutil.copyfile(source_path, destination)

        if remove_source:
            source_path.unlink(missing_ok=True)

        return destination.relative_to(self.output_dir).as_posix()

    def _download_inline_image(self, url: str, basename: str) -> Path | None:
        temp_dir = self.output_dir / ".tmp-downloads"
        temp_dir.mkdir(parents=True, exist_ok=True)
        download_target = temp_dir / sanitize_filename(basename)

        headers = {"User-Agent": "bitbucket-issue-archive/1.0"}
        username = os.getenv("BITBUCKET_USERNAME")
        password = os.getenv("BITBUCKET_APP_PASSWORD")
        if username and password:
            auth_bytes = f"{username}:{password}".encode()
            headers["Authorization"] = f"Basic {base64.b64encode(auth_bytes).decode('ascii')}"

        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response, download_target.open("wb") as output_file:
                output_file.write(response.read())
        except (HTTPError, URLError, OSError) as error:
            self.download_errors.append(f"{url} -> {error}")
            return None

        return download_target


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_dir", type=Path, help="Path to the unpacked Bitbucket issues export directory.")
    parser.add_argument("output_dir", type=Path, help="Where to write the generated HTML archive.")
    parser.add_argument(
        "--db-file",
        default="db-1.0.json",
        help="Export database file to use inside input_dir. Default: %(default)s",
    )
    parser.add_argument(
        "--archive-title",
        default=None,
        help="Optional archive title shown in the generated pages. Defaults to the input folder name.",
    )
    parser.add_argument(
        "--repo-slug",
        default=None,
        help="Optional Bitbucket repo slug such as myworkspace/myrepo for original issue links.",
    )
    parser.add_argument(
        "--inline-image-dir",
        type=Path,
        action="append",
        default=[],
        help="Optional folder containing previously saved inline images to localize into the archive. "
        "Pass multiple times to combine sources.",
    )
    parser.add_argument(
        "--download-inline-images",
        action="store_true",
        help="Attempt to download inline images from original Bitbucket URLs using BITBUCKET_USERNAME and "
        "BITBUCKET_APP_PASSWORD if available.",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Scan the Bitbucket export and write missing-inline-images.csv without generating HTML pages or "
        "copying attachments.",
    )
    args = parser.parse_args(argv)
    if args.report_only and args.download_inline_images:
        parser.error("--report-only cannot be combined with --download-inline-images")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    migration_log_path = default_migration_log_path(args.output_dir)
    revision_line = describe_tool_revision()
    archive_title = args.archive_title or args.input_dir.name
    export_path = args.input_dir / args.db_file

    if not export_path.exists():
        message = f"Export database file not found: {export_path}"
        print(message, file=sys.stderr)
        append_migration_log(
            migration_log_path,
            [sys.executable, *sys.argv],
            [revision_line, message],
            status="failed",
        )
        return 1

    data = load_export(export_path)
    issues = sorted(data["issues"], key=lambda issue: issue["id"])
    comments_by_issue = group_by_issue(data["comments"])
    logs_by_issue = group_by_issue(data["logs"])
    attachments_by_issue = group_by_issue(data["attachments"])

    args.output_dir.mkdir(parents=True, exist_ok=True)
    attachment_url_map: dict[str, str] = {}
    if not args.report_only:
        assets_dir = args.output_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(STYLE_CSS_PATH, assets_dir / "style.css")

        attachment_url_map = copy_named_attachments(
            input_dir=args.input_dir,
            output_dir=args.output_dir,
            attachments=data["attachments"],
        )

    localizer = AssetLocalizer(
        output_dir=args.output_dir,
        attachment_url_map=attachment_url_map,
        inline_image_dirs=args.inline_image_dir,
        download_inline_images=args.download_inline_images,
        copy_assets=not args.report_only,
    )
    localizer.set_issue_ids(issue["id"] for issue in issues)

    for issue in issues:
        issue_html = render_issue_page(
            IssuePageData(
                issue=issue,
                archive_title=archive_title,
                repo_slug=args.repo_slug,
                comments=sorted(comments_by_issue[issue["id"]], key=comment_sort_key),
                logs=sorted(logs_by_issue[issue["id"]], key=log_sort_key),
                attachments=attachments_by_issue[issue["id"]],
            ),
            localizer=localizer,
        )
        if not args.report_only:
            issue_path = args.output_dir / f"{issue['id']}.html"
            issue_path.write_text(issue_html, encoding="utf-8")

    if not args.report_only:
        index_html = render_index_page(
            archive_title=archive_title,
            issues=issues,
            comments_by_issue=comments_by_issue,
            attachments_by_issue=attachments_by_issue,
        )
        (args.output_dir / "index.html").write_text(index_html, encoding="utf-8")

    write_report_files(args.output_dir, localizer)

    if args.report_only:
        summary_lines = [
            revision_line,
            f"Scanned {len(issues)} issues in report-only mode into {args.output_dir}",
            f"Resolved {len(localizer.inline_image_map)} inline images from provided local directories",
            f"Missing {len(localizer.missing_inline_images)} inline images",
        ]
    else:
        summary_lines = [
            revision_line,
            f"Generated {len(issues)} issue pages in {args.output_dir}",
            f"Copied {len(attachment_url_map)} named attachments",
            f"Localized {len(localizer.inline_image_map)} inline images",
        ]
    for line in summary_lines:
        print(line)
    if localizer.missing_inline_images and not args.report_only:
        missing_message = f"Missing {len(localizer.missing_inline_images)} inline images"
        summary_lines.append(missing_message)
        print(missing_message)
    if localizer.download_errors:
        error_message = f"Download errors: {len(localizer.download_errors)}"
        summary_lines.append(error_message)
        print(error_message)

    append_migration_log(
        migration_log_path,
        [sys.executable, *sys.argv],
        summary_lines,
        status="success",
    )

    return 0


def load_export(export_path: Path) -> dict:
    data = json.loads(export_path.read_text(encoding="utf-8"))
    required_sections = {"issues", "attachments", "comments", "logs"}
    missing_sections = required_sections.difference(data)
    if missing_sections:
        raise ValueError(f"Export file is missing required sections: {sorted(missing_sections)}")
    return data


def group_by_issue(rows: Iterable[dict]) -> defaultdict[int, list[dict]]:
    grouped: defaultdict[int, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["issue"]].append(row)
    return grouped


def copy_named_attachments(input_dir: Path, output_dir: Path, attachments: Sequence[dict]) -> dict[str, str]:
    url_map: dict[str, str] = {}

    for attachment in attachments:
        source_path = input_dir / attachment["path"]
        if not source_path.exists():
            continue

        destination_dir = output_dir / "assets" / "attachments" / f"issue-{attachment['issue']}"
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = unique_destination(destination_dir, sanitize_filename(attachment["filename"]))
        shutil.copyfile(source_path, destination)
        url_map[attachment["url"]] = destination.relative_to(output_dir).as_posix()

    return url_map


def write_report_files(output_dir: Path, localizer: AssetLocalizer) -> None:
    legacy_missing_path = output_dir / "missing-inline-images.txt"
    if legacy_missing_path.exists():
        legacy_missing_path.unlink()

    missing_csv_path = output_dir / "missing-inline-images.csv"
    if localizer.missing_inline_image_records:
        fieldnames = [
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
        with missing_csv_path.open("w", encoding="utf-8", newline="") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()
            for record in sorted(
                localizer.missing_inline_image_records,
                key=lambda row: (
                    int(row["issue_id"]) if row["issue_id"] else 0,
                    row["source_type"],
                    int(row["comment_id"]) if row["comment_id"] else 0,
                    row["image_filename"],
                ),
            ):
                writer.writerow(record)
    elif missing_csv_path.exists():
        missing_csv_path.unlink()

    if localizer.download_errors:
        errors_path = output_dir / "inline-image-download-errors.txt"
        errors_path.write_text("\n".join(localizer.download_errors) + "\n", encoding="utf-8")


def sanitize_filename(filename: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._ -]", "_", filename).strip(" .")
    return sanitized or "asset"


def unique_destination(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 1

    while candidate.exists():
        candidate = directory / f"{stem}-{counter}{suffix}"
        counter += 1

    return candidate


def iter_files_safe(directory: Path) -> Iterator[Path]:
    try:
        entries = list(directory.iterdir())
    except OSError:
        return

    for entry in entries:
        try:
            is_dir = entry.is_dir()
            is_file = entry.is_file()
        except OSError:
            continue

        if is_file:
            yield entry
        elif is_dir:
            yield from iter_files_safe(entry)


if __name__ == "__main__":
    raise SystemExit(main())
