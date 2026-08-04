from __future__ import annotations

from dataclasses import dataclass


@dataclass
class JiraIssueDetails:
    key: str
    summary: str
    description: str
    issue_type: str
    priority_id: str
    account_id: str | None
    reporter_id: str | None
    status: str
    comments: list[str]
    component: str | None = None
