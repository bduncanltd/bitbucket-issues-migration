"""Render a static HTML site from an archive. Reads the manifest only — no network.

This is one consumer of the archive schema, not a privileged part of it. It resolves
markdown URLs through ``asset_index`` and copies referenced assets into the output, so
the generated site is self-contained and can be published anywhere. Because it never
calls Bitbucket, regenerating a site after a style or template change takes seconds
and can be repeated indefinitely.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from bitbucket_export.model import Archive, Issue

from .rendering import (
    IssuePageData,
    render_index_page,
    render_issue_page,
)

STYLE_CSS_PATH = Path(__file__).resolve().parent / "assets" / "style.css"
ISSUE_LINK_RE = re.compile(r"https://bitbucket\.org/[^/]+/[^/]+/issues/(\d+)(?:/[^\s)]*)?$")


MISSING_ASSETS_SHOWN = 10


class MissingAssetError(RuntimeError):
    """Raised when the manifest lists assets whose files are absent from the archive."""


@dataclass
class RenderResult:
    output_dir: Path
    page_count: int
    asset_count: int
    unresolved_count: int
    missing_asset_count: int = 0

    @property
    def summary(self) -> list[str]:
        lines = [
            f"Generated {self.page_count} issue pages in {self.output_dir}",
            f"Copied {self.asset_count} assets",
        ]
        if self.missing_asset_count:
            lines.append(f"Assets listed in the manifest but missing from the archive: {self.missing_asset_count}")
        if self.unresolved_count:
            lines.append(f"References with no stored asset: {self.unresolved_count}")
        return lines


class ArchiveLocalizer:
    """Resolves original Bitbucket URLs to paths in the generated site."""

    def __init__(self, archive: Archive) -> None:
        self.archive = archive
        self.issue_ids = archive.issue_ids()
        self.unresolved: set[str] = set()
        self.attachment_url_map = {
            attachment.source_url: archive.assets[attachment.asset_id].path
            for issue in archive.issues
            for attachment in issue.attachments
            if attachment.asset_id and attachment.asset_id in archive.assets and attachment.source_url
        }

    def localize_link(self, url: str) -> str:
        stored = self.archive.path_for_url(url)
        if stored:
            return stored

        issue_match = ISSUE_LINK_RE.match(url)
        if issue_match is not None and int(issue_match.group(1)) in self.issue_ids:
            return f"{issue_match.group(1)}.html"

        return url

    def localize_inline_image(self, url: str, context: dict[str, str] | None = None) -> str | None:
        stored = self.archive.path_for_url(url)
        if stored is None:
            self.unresolved.add(url)
        return stored


def render_site(
    archive: Archive,
    output_dir: Path,
    archive_dir: Path,
    allow_missing_assets: bool = False,
) -> RenderResult:
    """Write ``index.html``, one page per issue, and every referenced asset.

    Raises :class:`MissingAssetError` before writing anything if the manifest lists
    assets whose files are not in the archive, unless ``allow_missing_assets`` accepts
    the gaps deliberately — silently publishing broken links is never the default.
    """

    missing = [asset.path for asset in archive.assets.values() if not (archive_dir / asset.path).exists()]
    if missing and not allow_missing_assets:
        shown = "\n".join(f"  {path}" for path in missing[:MISSING_ASSETS_SHOWN])
        more = len(missing) - MISSING_ASSETS_SHOWN
        if more > 0:
            shown += f"\n  ... and {more} more"
        raise MissingAssetError(
            f"The manifest lists {len(missing)} asset(s) whose files are missing from {archive_dir}:\n"
            f"{shown}\n"
            "The archive is damaged or incomplete. Re-run the exporter — it re-fetches only\n"
            "what is missing — then regenerate the site. Or pass --allow-missing-assets to\n"
            "publish without them."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "assets").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(STYLE_CSS_PATH, output_dir / "assets" / "style.css")

    asset_count = _copy_assets(archive.assets.values(), archive_dir, output_dir)

    localizer = ArchiveLocalizer(archive)
    issues = sorted(archive.issues, key=lambda issue: issue.id)
    issue_dicts = [_issue_dict(issue, archive) for issue in issues]
    comments_by_issue = {issue.id: _comment_dicts(issue, archive) for issue in issues}
    attachments_by_issue = {issue.id: _attachment_dicts(issue, archive) for issue in issues}

    for issue, issue_dict in zip(issues, issue_dicts, strict=True):
        page_html = render_issue_page(
            IssuePageData(
                issue=issue_dict,
                archive_title=archive.title or archive.repository.full_name,
                repo_slug=archive.repository.full_name,
                comments=comments_by_issue[issue.id],
                logs=_history_dicts(issue, archive),
                attachments=attachments_by_issue[issue.id],
            ),
            localizer=localizer,
        )
        (output_dir / f"{issue.id}.html").write_text(page_html, encoding="utf-8")

    index_html = render_index_page(
        archive_title=archive.title or archive.repository.full_name,
        issues=issue_dicts,
        comments_by_issue=comments_by_issue,
        attachments_by_issue=attachments_by_issue,
    )
    (output_dir / "index.html").write_text(index_html, encoding="utf-8")

    return RenderResult(
        output_dir=output_dir,
        page_count=len(issues),
        asset_count=asset_count,
        unresolved_count=len(localizer.unresolved),
        missing_asset_count=len(missing),
    )


def _copy_assets(assets, archive_dir: Path, output_dir: Path) -> int:
    """Mirror stored assets into the site, hardlinking so large archives stay cheap."""

    copied = 0
    for asset in assets:
        source = archive_dir / asset.path
        if not source.exists():
            continue
        destination = output_dir / asset.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination.unlink()
        try:
            os.link(source, destination)
        except (OSError, NotImplementedError):
            # Different volume, or a filesystem without hardlinks: fall back to a copy.
            shutil.copyfile(source, destination)
        copied += 1
    return copied


# ---- adapters to the renderer's flat dict shape ------------------------------


def _issue_dict(issue: Issue, archive: Archive) -> dict:
    return {
        "id": issue.id,
        "title": issue.title,
        "content": issue.content.markdown,
        "content_markup": issue.content.markup,
        "reporter": archive.display_name(issue.reporter),
        "assignee": archive.display_name(issue.assignee),
        "created_on": issue.created_on,
        "updated_on": issue.updated_on,
        "edited_on": issue.edited_on,
        "content_updated_on": None,
        "kind": issue.kind,
        "status": issue.state,
        "priority": issue.priority,
        "milestone": issue.milestone,
        "component": issue.component,
        "version": issue.version,
        # The REST API reports watch/vote counts, not member lists, so these stay empty.
        "watchers": [],
        "voters": [],
    }


def _comment_dicts(issue: Issue, archive: Archive) -> list[dict]:
    return [
        {
            "id": comment.id,
            "issue": issue.id,
            "content": comment.content.markdown,
            "content_markup": comment.content.markup,
            "user": archive.display_name(comment.user),
            "created_on": comment.created_on,
        }
        for comment in issue.comments
    ]


def _history_dicts(issue: Issue, archive: Archive) -> list[dict]:
    return [
        {
            "issue": issue.id,
            "field": entry.field_name,
            "changed_from": entry.old,
            "changed_to": entry.new,
            "user": archive.display_name(entry.user),
            "created_on": entry.created_on,
            "comment": entry.comment_id,
        }
        for entry in issue.history
    ]


def _attachment_dicts(issue: Issue, archive: Archive) -> list[dict]:
    return [
        {
            "issue": issue.id,
            "filename": attachment.filename,
            "url": attachment.source_url,
            "user": archive.display_name(attachment.user),
        }
        for attachment in issue.attachments
    ]
