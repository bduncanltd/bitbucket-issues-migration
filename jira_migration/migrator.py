"""Orchestrates the migration from a Bitbucket export to a Jira project."""

from __future__ import annotations

import logging
import sys

from jira import Issue
from jira.exceptions import JIRAError

from jira_migration import markup
from jira_migration.bitbucket_export import BitbucketExport
from jira_migration.bitbucket_issue import BitbucketIssue
from jira_migration.config import JiraMigrationConfig
from jira_migration.jira_import import JiraImport
from jira_migration.jira_issue_details import JiraIssueDetails

# Map Bitbucket issue types to Jira issue types.
ISSUE_TYPE_MAP = {
    "Story": "Task",
    "Task": "Task",
    "Bug": "Bug",
}

# Map Bitbucket issue statuses to Jira issue statuses.
ISSUE_STATUS_MAP = {
    "Done": "Done",
    "Selected For Development": "In Progress",
    "Backlog": "To Do",
}

# When resolution is set, it overrides the status mapping.
RESOLUTION_STATUS_MAP = {
    "invalid": "Invalid",
    "duplicate": "Invalid",
    "wontfix": "Invalid",
    "won't fix": "Invalid",
    "resolved": "Done",
}

# Map Bitbucket issue priorities to Jira priority IDs.
PRIORITY_MAP = {
    "Highest": "1",
    "Medium": "2",
    "Low": "3",
    "High": "4",
    "Lowest": "5",
}


def _get_user_id(valid_user_ids: set[str], bitbucket_user_id: str) -> str | None:
    if bitbucket_user_id in valid_user_ids:
        return bitbucket_user_id
    return None


def _get_issue_type(issue: BitbucketIssue) -> str:
    if issue.type not in ISSUE_TYPE_MAP:
        logging.error(
            "Unknown issue type '%s' for issue %d. Update ISSUE_TYPE_MAP.",
            issue.type,
            issue.id,
        )
        sys.exit()
    return ISSUE_TYPE_MAP[issue.type]


def _get_issue_status(issue: BitbucketIssue) -> str:
    if issue.resolution in RESOLUTION_STATUS_MAP:
        return RESOLUTION_STATUS_MAP[issue.resolution]
    if issue.status not in ISSUE_STATUS_MAP:
        logging.error(
            "Unknown issue status '%s' for issue %d. Update ISSUE_STATUS_MAP.",
            issue.status,
            issue.id,
        )
        sys.exit()
    return ISSUE_STATUS_MAP[issue.status]


def _get_priority_id(priority: str) -> str:
    assert priority in PRIORITY_MAP
    return PRIORITY_MAP[priority]


def _get_description(bb_issue: BitbucketIssue, display_names: dict[str, str]) -> str:
    raw = bb_issue.description
    if raw.startswith("Imported from "):
        raw = raw.split("\n", 1)[1].strip()

    assignee = (
        display_names.get(bb_issue.assignee, bb_issue.assignee)
        if bb_issue.assignee
        else "N/A"
    )
    reporter = (
        display_names.get(bb_issue.reporter, bb_issue.reporter)
        if bb_issue.reporter
        else "N/A"
    )
    header = (
        "_"
        f"Issue imported from Bitbucket. "
        f"Original status: {bb_issue.status}, "
        f"resolution: {bb_issue.resolution or 'N/A'}, "
        f"type: {bb_issue.type}, "
        f"Assignee: {assignee}, "
        f"Reporter: {reporter}, "
        f"created: {bb_issue.created}, "
        f"updated: {bb_issue.updated}"
        "_"
    )
    return f"{markup.convert(raw)}\n\n----\n{header}"


def _get_comment_body(bb_comment: dict[str, str], display_names: dict[str, str]) -> str:
    author_id = bb_comment.get("author", "")
    author = display_names.get(author_id, author_id) or "Unknown"
    created = bb_comment.get("created", "")
    body = bb_comment.get("body", "")
    return f"_Comment migrated from Bitbucket, user: {author}, time: {created}_\n\n{markup.convert(body)}"


class BitbucketJiraMigrator:
    def __init__(
        self, export: BitbucketExport, jira: JiraImport, config: JiraMigrationConfig
    ) -> None:
        self._export = export
        self._jira = jira
        self._config = config

    @property
    def valid_user_ids(self) -> set[str]:
        """User IDs that can be assigned in Jira, after applying the BB→Jira ID map."""
        known = self._jira.known_user_ids
        valid: set[str] = set()
        for bb_id in self._export.user_ids:
            if bb_id in known:
                valid.add(bb_id)
        return valid

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

        issues = self._export.issues[
            from_issue - 1 : from_issue - 1 + limit if limit is not None else None
        ]
        total = len(self._export.issues)
        for i, bb_issue in enumerate(issues, start=from_issue):
            comments = [
                _get_comment_body(comment, self._export.user_display_names)
                for comment in bb_issue.comments
            ]

            jira_issue_details = JiraIssueDetails(
                key=f"{self._config.board_id}-{bb_issue.id}",
                summary=bb_issue.summary,
                description=_get_description(bb_issue, self._export.user_display_names),
                issue_type=_get_issue_type(bb_issue),
                priority_id=_get_priority_id(bb_issue.priority),
                account_id=_get_user_id(self.valid_user_ids, bb_issue.assignee or ""),
                reporter_id=_get_user_id(self.valid_user_ids, bb_issue.reporter or ""),
                status=_get_issue_status(bb_issue),
                comments=comments,
            )

            jira_issue: Issue
            try:
                jira_issue = self._jira.get_issue(jira_issue_details.key)
                logging.info("Updating issue %d/%d: %s", i, total, bb_issue.summary)
            except JIRAError:
                logging.info("Creating issue %d/%d: %s", i, total, bb_issue.summary)
                jira_issue = self._jira.create_issue(jira_issue_details)

            # Upload inline images and replace URLs with filenames before writing
            jira_issue_details.description = self._jira.upload_inline_images(
                jira_issue, jira_issue_details.description
            )
            jira_issue_details.comments = [
                self._jira.upload_inline_images(jira_issue, c)
                for c in jira_issue_details.comments
            ]

            try:
                self._jira.update_issue(jira_issue, jira_issue_details)
            except JIRAError:
                pass  # already logged inside update_issue

            self._jira.sync_comments(jira_issue, jira_issue_details)
            self._jira.sync_attachments(jira_issue, bb_issue.attachments)
