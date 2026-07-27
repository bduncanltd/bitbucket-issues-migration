"""Orchestrates the migration from a Bitbucket issue archive to a Jira project."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable
from pathlib import Path

from jira import Issue
from jira.exceptions import JIRAError

from jira_migration import markup
from jira_migration.archive_source import ArchiveSource
from jira_migration.bitbucket_issue import BitbucketComment, BitbucketIssue
from jira_migration.config import JiraMigrationConfig
from jira_migration.jira_import import JiraImport
from jira_migration.jira_issue_details import JiraIssueDetails

# Map Bitbucket issue kinds to Jira issue types.
ISSUE_TYPE_MAP = {
    "bug": "Bug",
    "enhancement": "Task",
    "proposal": "Task",
    "task": "Task",
}

# Map Bitbucket issue states to Jira issue statuses.
ISSUE_STATUS_MAP = {
    "new": "To Do",
    "open": "In Progress",
    "on hold": "To Do",
    "resolved": "Done",
    "closed": "Done",
    "invalid": "Invalid",
    "duplicate": "Invalid",
    "wontfix": "Invalid",
}

# Map Bitbucket priorities to Jira priority IDs.
PRIORITY_MAP = {
    "blocker": "1",
    "critical": "4",
    "major": "2",
    "minor": "3",
    "trivial": "5",
}

INLINE_IMAGE_RE = re.compile(r"!(https?://[^!]+)!")


def _get_user_id(valid_user_ids: set[str], account_id: str | None) -> str | None:
    if account_id and account_id in valid_user_ids:
        return account_id
    return None


def _get_issue_type(issue: BitbucketIssue) -> str:
    if issue.kind not in ISSUE_TYPE_MAP:
        logging.error("Unknown issue kind '%s' for issue %d. Update ISSUE_TYPE_MAP.", issue.kind, issue.id)
        sys.exit()
    return ISSUE_TYPE_MAP[issue.kind]


def _get_issue_status(issue: BitbucketIssue) -> str:
    if issue.state not in ISSUE_STATUS_MAP:
        logging.error("Unknown issue state '%s' for issue %d. Update ISSUE_STATUS_MAP.", issue.state, issue.id)
        sys.exit()
    return ISSUE_STATUS_MAP[issue.state]


def _get_priority_id(issue: BitbucketIssue) -> str:
    if issue.priority not in PRIORITY_MAP:
        logging.error("Unknown priority '%s' for issue %d. Update PRIORITY_MAP.", issue.priority, issue.id)
        sys.exit()
    return PRIORITY_MAP[issue.priority]


def _get_description(bb_issue: BitbucketIssue) -> str:
    raw = bb_issue.description
    if raw.startswith("Imported from "):
        _, separator, imported_body = raw.partition("\n")
        if separator:
            raw = imported_body.strip()

    parts = [
        f"Original: [{bb_issue.url}]" if bb_issue.url else "Issue imported from Bitbucket",
        f"state: {bb_issue.state}",
        f"kind: {bb_issue.kind}",
        f"priority: {bb_issue.priority}",
        f"Assignee: {bb_issue.assignee_name or 'N/A'}",
        f"Reporter: {bb_issue.reporter_name or 'N/A'}",
    ]
    for label, value in (
        ("component", bb_issue.component),
        ("milestone", bb_issue.milestone),
        ("version", bb_issue.version),
    ):
        if value:
            parts.append(f"{label}: {value}")
    parts.append(f"created: {bb_issue.created}")
    parts.append(f"updated: {bb_issue.updated}")

    header = "_" + ", ".join(parts) + "_"
    return f"{markup.convert_content(raw, bb_issue.description_markup)}\n\n----\n{header}"


def _get_comment_body(comment: BitbucketComment) -> str:
    author = comment.author or "Unknown"
    return (
        f"_Comment migrated from Bitbucket, user: {author}, time: {comment.created}_\n\n"
        f"{markup.convert_content(comment.body, comment.markup)}"
    )


def _inline_images(source: ArchiveSource, issue_id: int, texts: Iterable[str]) -> dict[str, tuple[Path, str]]:
    """Map each archived inline-image URL in ``texts`` to (stored file, upload name).

    Upload names are the assets' original filenames; when two *different* assets on
    the same issue share a filename, the later one falls back to its content-addressed
    file name, which is unique.
    """
    result: dict[str, tuple[Path, str]] = {}
    name_owners: dict[str, str] = {}
    for text in texts:
        for url in INLINE_IMAGE_RE.findall(text):
            if url in result:
                continue
            resolved = source.inline_image(url)
            if resolved is None:
                logging.warning("Issue %d references an image the archive does not hold: %s", issue_id, url)
                continue
            path, name, asset_id = resolved
            if name_owners.setdefault(name, asset_id) != asset_id:
                name = path.name
            result[url] = (path, name)
    return result


class BitbucketJiraMigrator:
    def __init__(self, export: ArchiveSource, jira: JiraImport, config: JiraMigrationConfig) -> None:
        self._export = export
        self._jira = jira
        self._config = config

    @property
    def valid_user_ids(self) -> set[str]:
        """Account ids that can be assigned in Jira."""
        known = self._jira.known_user_ids
        return {account_id for account_id in self._export.user_ids if account_id in known}

    def _log_unmapped_users(self) -> None:
        known = self._jira.known_user_ids
        for bb_id in self._export.user_ids:
            if bb_id in known:
                continue
            reporter_ids = [i.id for i in self._export.issues if i.reporter == bb_id]
            assignee_ids = [i.id for i in self._export.issues if i.assignee == bb_id]
            logging.warning(
                "Bitbucket user ID %s is not found in Jira. "
                "They are reporter of %d issues (e.g. %s) and assignee of %d issues (e.g. %s).",
                bb_id,
                len(reporter_ids),
                f"#{reporter_ids[-1]}" if reporter_ids else "N/A",
                len(assignee_ids),
                f"#{assignee_ids[-1]}" if assignee_ids else "N/A",
            )

    def migrate(self, limit: int | None = None, from_issue: int = 1) -> None:
        self._jira.assert_project_exists()
        self._log_unmapped_users()
        self._jira.log_user_details(self.valid_user_ids)
        # Components exist before any issue is created, so each issue's component can
        # ride along in the create payload instead of a follow-up update (and email).
        self._jira.ensure_components(self._export.component_names)

        issues = self._export.issues[from_issue - 1 : from_issue - 1 + limit if limit is not None else None]
        total = len(self._export.issues)
        for i, bb_issue in enumerate(issues, start=from_issue):
            jira_issue_details = JiraIssueDetails(
                key=f"{self._config.board_id}-{bb_issue.id}",
                summary=bb_issue.summary,
                description=_get_description(bb_issue),
                issue_type=_get_issue_type(bb_issue),
                priority_id=_get_priority_id(bb_issue),
                account_id=_get_user_id(self.valid_user_ids, bb_issue.assignee),
                reporter_id=_get_user_id(self.valid_user_ids, bb_issue.reporter),
                status=_get_issue_status(bb_issue),
                comments=[_get_comment_body(comment) for comment in bb_issue.comments],
                component=bb_issue.component,
            )

            jira_issue: Issue
            try:
                jira_issue = self._jira.get_issue(jira_issue_details.key)
                logging.info("Updating issue %d/%d: %s", i, total, bb_issue.summary)
            except JIRAError:
                logging.info("Creating issue %d/%d: %s", i, total, bb_issue.summary)
                jira_issue = self._jira.create_issue(jira_issue_details)

            # Attach archived inline images and point the markup at the attachments.
            images = _inline_images(
                self._export, bb_issue.id, [jira_issue_details.description, *jira_issue_details.comments]
            )
            jira_issue_details.description = self._jira.upload_inline_images(
                jira_issue, jira_issue_details.description, images
            )
            jira_issue_details.comments = [
                self._jira.upload_inline_images(jira_issue, c, images) for c in jira_issue_details.comments
            ]

            try:
                self._jira.update_issue(jira_issue, jira_issue_details)
            except JIRAError:
                pass  # already logged inside update_issue

            self._jira.sync_comments(jira_issue, jira_issue_details)
            self._jira.sync_attachments(jira_issue, bb_issue.attachments)
