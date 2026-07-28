"""Adapts a ``bitbucket_export`` archive into the shapes the Jira migrator consumes.

One consumer of the published archive schema — the same position ``static_site``
occupies. Everything is read through ``bitbucket_export.model.Archive``; nothing here
parses archive JSON by hand, and nothing touches the network.
"""

from __future__ import annotations

import logging
from pathlib import Path

from bitbucket_export.model import Archive, Issue
from jira_migration.bitbucket_issue import BitbucketAttachment, BitbucketComment, BitbucketIssue

DUMMY_TIMESTAMP = "2024-08-22T03:33:15.522982+00:00"


class ArchiveSource:
    """Loads an archive and presents migrator-ready issues, users, and files."""

    def __init__(self, archive_dir: Path) -> None:
        self._archive = Archive.load(archive_dir)
        self._archive_dir = archive_dir
        self.issues = [self._adapt_issue(issue) for issue in self._archive.issues]
        logging.info("Found %d issues in %s.", len(self.issues), archive_dir)
        self._insert_missing_issues()
        self._assert_ids_are_in_order()

    @property
    def component_names(self) -> set[str]:
        """Every component the tracker defines or an issue uses.

        The manifest keeps definitions even when no issue references them; the union
        also covers an issue value whose definition the exporter could not list.
        """
        defined = {definition.name for definition in self._archive.components}
        used = {issue.component for issue in self.issues if issue.component}
        return defined | used

    @property
    def display_names(self) -> dict[str, str]:
        """Display name by Atlassian account id, for resolving ``@{id}`` mentions.

        Includes users the exporter captured from mentions alone, not just issue
        participants.
        """
        return {
            user.account_id: user.display_name
            for user in self._archive.users.values()
            if user.account_id and user.display_name
        }

    @property
    def user_ids(self) -> set[str]:
        """All reporter and assignee account ids found across all issues."""
        ids: set[str] = set()
        for issue in self.issues:
            if issue.reporter:
                ids.add(issue.reporter)
            if issue.assignee:
                ids.add(issue.assignee)
        return ids

    def inline_image(self, url: str) -> tuple[Path, str, str] | None:
        """Resolve an inline-image URL to (stored file, preferred filename, asset id).

        Returns None when the archive never captured the URL (it is listed in the
        manifest's ``unresolved_assets``). A URL the manifest *does* claim but whose
        file is gone means the archive is damaged, so that aborts instead.
        """
        asset = self._archive.asset_for_url(url)
        if asset is None:
            return None
        path = self._archive_dir / asset.path
        if not path.exists():
            raise RuntimeError(f"Inline image missing from archive: {path}. Re-run bitbucket_export to restore it.")
        return path, asset.filename or path.name, asset.id

    # ---- adaptation ---------------------------------------------------------

    def _adapt_issue(self, issue: Issue) -> BitbucketIssue:
        return BitbucketIssue(
            id=issue.id,
            kind=issue.kind or "task",
            state=issue.state or "new",
            priority=issue.priority or "trivial",
            summary=issue.title,
            description=issue.content.markdown,
            description_markup=issue.content.markup,
            created=issue.created_on or "",
            updated=issue.updated_on or "",
            url=issue.url,
            component=issue.component,
            milestone=issue.milestone,
            version=issue.version,
            reporter=self._account_id(issue.reporter),
            assignee=self._account_id(issue.assignee),
            reporter_name=self._archive.display_name(issue.reporter),
            assignee_name=self._archive.display_name(issue.assignee),
            comments=[
                BitbucketComment(
                    author=self._archive.display_name(comment.user),
                    created=comment.created_on or "",
                    body=comment.content.markdown,
                    markup=comment.content.markup,
                )
                for comment in issue.comments
                # Bitbucket attaches a text-less comment to every field change;
                # migrating those would flood Jira with empty comments.
                if comment.content.markdown.strip()
            ],
            attachments=self._adapt_attachments(issue),
        )

    def _account_id(self, user_key: str | None) -> str | None:
        user = self._archive.users.get(user_key) if user_key else None
        return user.account_id if user else None

    def _adapt_attachments(self, issue: Issue) -> list[BitbucketAttachment]:
        result: list[BitbucketAttachment] = []
        for attachment in issue.attachments:
            if attachment.asset_id is None:
                logging.warning(
                    "Issue %d attachment %r was never downloaded; skipping. Re-run bitbucket_export to capture it.",
                    issue.id,
                    attachment.filename,
                )
                continue
            asset = self._archive.assets[attachment.asset_id]
            path = self._archive_dir / asset.path
            if not path.exists():
                raise RuntimeError(f"Attachment file missing from archive: {path}. Re-run bitbucket_export.")
            result.append(BitbucketAttachment(filename=attachment.filename, path=str(path)))
        return result

    # ---- numbering ----------------------------------------------------------

    def _insert_missing_issues(self) -> None:
        """Fill gaps left by deleted Bitbucket issues with dummy placeholders.

        Jira keys are derived from Bitbucket ids, so the numbering must stay aligned.
        """
        expected = 1
        missing: list[int] = []
        for issue in self.issues:
            while expected < issue.id:
                missing.append(expected)
                expected += 1
            expected += 1

        for n in missing:
            logging.warning("Missing issue with ID %d", n)

        for n in missing:
            self.issues.insert(
                n - 1,
                BitbucketIssue(
                    id=n,
                    kind="task",
                    state="invalid",
                    priority="trivial",
                    summary=f"Dummy issue {n}",
                    description="Dummy issue to preserve issue numbering during Bitbucket to Jira migration.",
                    created=DUMMY_TIMESTAMP,
                    updated=DUMMY_TIMESTAMP,
                ),
            )
        logging.info("There are now %d issues.", len(self.issues))

    def _assert_ids_are_in_order(self) -> None:
        expected = 1
        for issue in self.issues:
            assert issue.id == expected
            expected += 1
