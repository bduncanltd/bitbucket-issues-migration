from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class BitbucketAttachment:
    filename: str
    path: str  # absolute path to the file within the extract directory


@dataclass
class BitbucketIssue:
    id: int
    type: str
    status: str
    priority: str
    resolution: str
    summary: str
    description: str
    comments: list[dict[str, str]]
    created: str
    updated: str
    attachments: list[BitbucketAttachment]
    reporter: str | None = None
    assignee: str | None = None

    @classmethod
    def from_dict(cls, issue: dict[str, Any]) -> BitbucketIssue:
        return cls(
            id=cls.get_issue_id(issue),
            type=issue["issueType"],
            status=issue["status"],
            priority=issue["priority"],
            resolution=(issue.get("resolution") or "").lower(),
            summary=issue["summary"],
            description=issue.get("description", ""),
            comments=issue.get("comments", []),
            created=issue["created"],
            updated=issue["updated"],
            attachments=[],
            reporter=issue.get("reporter", None),
            assignee=issue.get("assignee", None),
        )

    @staticmethod
    def get_issue_id(issue: dict[str, Any]) -> int:
        issue_id_str: str = issue["externalId"]
        _, issue_id = issue_id_str.split("-")
        return int(issue_id)
