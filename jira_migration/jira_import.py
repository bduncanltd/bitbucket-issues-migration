"""Wraps the Jira client and handles writing issues into the target project."""

from __future__ import annotations

import logging
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from jira import JIRA
from jira.exceptions import JIRAError
from jira.resources import Comment, Issue, User
from playwright._impl._errors import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from jira_migration.config import JiraMigrationConfig
from jira_migration.jira_issue_details import JiraIssueDetails

DEFAULT_AUTH_STATE = Path.home() / ".cache" / "bitbucket-playwright" / "auth-state.json"

MAX_CREATE_RETRIES = 5
RESPONSE_404 = 404


class JiraImport:
    def __init__(
        self, config: JiraMigrationConfig, auth_state: Path | None = None
    ) -> None:
        self._config = config
        self._auth_state = auth_state
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

    def upload_inline_images(self, jira_issue: Issue, text: str) -> str:
        """Download Bitbucket-hosted images in wiki markup text, upload to Jira, replace URLs with filenames."""
        if self._auth_state is None:
            return text

        image_dir = Path(".migration/images")
        image_dir.mkdir(parents=True, exist_ok=True)

        existing_names = {
            a.filename for a in self._client.issue(jira_issue.key).fields.attachment
        }

        with sync_playwright() as playwright:
            request_context = playwright.request.new_context(
                storage_state=str(self._auth_state)
            )
            try:

                def replace(m: re.Match[str]) -> str:
                    url = m.group(1)
                    filename = Path(urlparse(url).path).name
                    local_path = image_dir / filename

                    if not local_path.exists():
                        logging.info("  Downloading image: %s", filename)

                        try:
                            response = request_context.get(
                                url, fail_on_status_code=False, timeout=30_000
                            )
                        except PlaywrightError as e:
                            logging.warning(
                                "  TLS/connection failed for %s: %s", url, e
                            )
                            return m.group(0)  # leave original !url!
                        except Exception as e:
                            logging.warning("  Unexpected error for %s: %s", url, e)
                            return m.group(0)

                        if not response or not response.ok:
                            logging.warning(
                                "  Failed to download image %s (HTTP %s)",
                                url,
                                getattr(response, "status", "unknown"),
                            )
                            return m.group(0)

                        local_path.write_bytes(response.body())

                    if filename not in existing_names:
                        logging.info("  Uploading image: %s", filename)
                        with open(local_path, "rb") as f:
                            self._client.add_attachment(
                                issue=jira_issue, attachment=f, filename=filename
                            )
                        existing_names.add(filename)

                    return f"!{filename}!"

                return re.sub(r"!(https?://[^!]+)!", replace, text)
            finally:
                request_context.dispose()

    def sync_attachments(self, jira_issue: Issue, attachments: list) -> None:
        """Upload attachments that aren't already on the Jira issue (matched by filename)."""
        existing_names = {
            a.filename for a in self._client.issue(jira_issue.key).fields.attachment
        }
        for attachment in attachments:
            if attachment.filename in existing_names:
                logging.info("  Attachment already exists: %s", attachment.filename)
                continue
            logging.info("  Uploading attachment: %s", attachment.filename)
            with open(attachment.path, "rb") as f:
                self._client.add_attachment(
                    issue=jira_issue, attachment=f, filename=attachment.filename
                )

    def update_issue(self, existing: Issue, details: JiraIssueDetails) -> None:
        try:
            fields = {
                "summary": details.summary,
                "description": details.description,
                "issuetype": {"name": details.issue_type},
                "priority": {"id": details.priority_id},
            }
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
