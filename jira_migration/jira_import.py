"""Wraps the Jira client and handles writing issues into the target project."""

from __future__ import annotations

import logging
import re
import sys
import time
from pathlib import Path

from jira import JIRA
from jira.exceptions import JIRAError
from jira.resources import Comment, Issue, User

from jira_migration.config import JiraMigrationConfig
from jira_migration.jira_issue_details import JiraIssueDetails

MAX_CREATE_RETRIES = 5
RESPONSE_404 = 404


class JiraImport:
    def __init__(self, config: JiraMigrationConfig) -> None:
        self._config = config
        self._client = JIRA(
            server=config.jira_url,
            basic_auth=(config.jira_email, config.jira_api_token),
        )

        logging.info("Fetching Jira users...")
        self.known_user_ids: set[str] = set()
        for group in self._client.groups():
            for user in self._client.group_members(group):
                self.known_user_ids.add(user)
        logging.info("Found %d known Jira users.", len(self.known_user_ids))

    def assert_project_exists(self) -> None:
        try:
            self._client.project(self._config.board_id)
            logging.info("Project %s exists in Jira.", self._config.board_id)
        except JIRAError:
            logging.error("Project %s does not exist in Jira", self._config.board_id)
            sys.exit()

    def log_user_details(self, valid_user_ids: set[str]) -> None:
        users: list[User] = [self._client.user(uid) for uid in valid_user_ids]
        for user in users:
            logging.info("User %s (%s) found in Jira", user.displayName, user.accountId)

    def get_issue(self, issue_key: str) -> Issue:
        return self._client.issue(issue_key)

    def ensure_components(self, names: set[str]) -> None:
        """Create any project components that do not exist yet.

        Done once up front so components can ride along in the create payload rather
        than being patched on afterwards, which would send an extra notification per
        issue.
        """
        if not names:
            return
        existing = {component.name for component in self._client.project_components(self._config.board_id)}
        for name in sorted(names - existing):
            logging.info("Creating component: %s", name)
            self._client.create_component(name, self._config.board_id)

    def sync_comments(self, jira_issue: Issue, details: JiraIssueDetails) -> None:
        """Sync comments by position: edit existing, add extras, delete leftovers."""
        jira_comments: list[Comment] = self._client.comments(jira_issue)

        for i, comment in enumerate(details.comments):
            if i < len(jira_comments):
                jira_comments[i].update(body=comment)
            else:
                self._client.add_comment(jira_issue, comment)

        for leftover in jira_comments[len(details.comments) :]:
            leftover.delete()

    def create_issue(self, details: JiraIssueDetails) -> Issue:
        issue_dict = {
            "project": {"key": self._config.board_id},
            "summary": details.summary,
            "description": details.description,
            "issuetype": {"name": details.issue_type},
            "priority": {"id": details.priority_id},
        }
        if details.component:
            issue_dict["components"] = [{"name": details.component}]
        if details.account_id:
            issue_dict["assignee"] = {"accountId": details.account_id}
        if details.reporter_id:
            issue_dict["reporter"] = {"accountId": details.reporter_id}
        try:
            jira_issue = self._create_with_retry(issue_dict)
        except JIRAError as e:
            if "cannot be assigned" in str(e):
                logging.warning(
                    "  Assignee %s cannot be assigned to project %s, creating without assignee.",
                    details.account_id,
                    self._config.board_id,
                )
                issue_dict.pop("assignee", None)
                jira_issue = self._create_with_retry(issue_dict)
            else:
                raise
        if jira_issue.fields.status.name != details.status:
            self._client.transition_issue(jira_issue, details.status)
        return jira_issue

    def _create_with_retry(self, issue_dict: dict) -> Issue:
        """Create an issue using prefetch=False then retry the GET to handle Jira Cloud propagation delay."""
        stub = self._client.create_issue(fields=issue_dict, prefetch=False)
        key = stub.raw["key"]
        for attempt in range(MAX_CREATE_RETRIES):
            try:
                return self._client.issue(key)
            except JIRAError as e:
                if e.status_code == RESPONSE_404 and attempt < (MAX_CREATE_RETRIES - 1):
                    delay = 2**attempt
                    logging.warning(
                        "  Created %s but it isn't visible yet, retrying in %ds...",
                        key,
                        delay,
                    )
                    time.sleep(delay)
                else:
                    raise
        raise RuntimeError(f"Issue {key} still not accessible after retries")

    def upload_inline_images(self, jira_issue: Issue, text: str, images: dict[str, tuple[Path, str]]) -> str:
        """Attach archived images referenced in wiki markup and point the markup at them.

        ``images`` maps each original Bitbucket URL to (stored file, upload name); the
        files come straight from the archive, so no network access is involved. URLs
        not in the map are left untouched.
        """
        if not images:
            return text

        existing_names = {a.filename for a in self._client.issue(jira_issue.key).fields.attachment}

        def replace(m: re.Match[str]) -> str:
            resolved = images.get(m.group(1))
            if resolved is None:
                return m.group(0)  # leave original !url!
            path, name = resolved
            if name not in existing_names:
                logging.info("  Uploading image: %s", name)
                with open(path, "rb") as f:
                    self._client.add_attachment(issue=jira_issue, attachment=f, filename=name)
                existing_names.add(name)
            return f"!{name}!"

        return re.sub(r"!(https?://[^!]+)!", replace, text)

    def sync_attachments(self, jira_issue: Issue, attachments: list) -> None:
        """Upload attachments that aren't already on the Jira issue (matched by filename)."""
        existing_names = {a.filename for a in self._client.issue(jira_issue.key).fields.attachment}
        for attachment in attachments:
            if attachment.filename in existing_names:
                logging.info("  Attachment already exists: %s", attachment.filename)
                continue
            logging.info("  Uploading attachment: %s", attachment.filename)
            with open(attachment.path, "rb") as f:
                self._client.add_attachment(issue=jira_issue, attachment=f, filename=attachment.filename)

    def update_issue(self, existing: Issue, details: JiraIssueDetails) -> None:
        try:
            fields = {
                "summary": details.summary,
                "description": details.description,
                "issuetype": {"name": details.issue_type},
                "priority": {"id": details.priority_id},
            }
            if details.component:
                fields["components"] = [{"name": details.component}]
            if details.account_id:
                fields["assignee"] = {"accountId": details.account_id}
            if details.reporter_id:
                fields["reporter"] = {"accountId": details.reporter_id}
            try:
                existing.update(fields=fields)
            except JIRAError as e:
                if "cannot be assigned" in str(e):
                    logging.warning(
                        "  Assignee %s cannot be assigned to project %s, updating without assignee.",
                        details.account_id,
                        self._config.board_id,
                    )
                    fields.pop("assignee", None)
                    existing.update(fields=fields)
                else:
                    raise
            if existing.fields.status.name != details.status:
                self._client.transition_issue(existing, details.status)
        except JIRAError as e:
            logging.error("Failed to update issue %s: %s", details.key, e)
