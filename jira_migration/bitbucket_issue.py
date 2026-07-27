from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BitbucketAttachment:
    filename: str
    path: str  # absolute path to the stored file inside the archive


@dataclass
class BitbucketComment:
    author: str  # display name; "" when the author is unknown
    created: str
    body: str
    markup: str = "markdown"


@dataclass
class BitbucketIssue:
    """One issue in Bitbucket's own vocabulary, as recorded in the archive.

    ``kind`` (bug/enhancement/proposal/task), ``state`` (new/open/resolved/...), and
    ``priority`` (trivial...blocker) are Bitbucket-native values; the migrator maps
    them to Jira. ``reporter``/``assignee`` are Atlassian account ids (None when the
    archive has no account id for that user), with display names carried separately
    for the description header.
    """

    id: int
    kind: str
    state: str
    priority: str
    summary: str
    description: str
    created: str
    updated: str
    description_markup: str = "markdown"
    comments: list[BitbucketComment] = field(default_factory=list)
    attachments: list[BitbucketAttachment] = field(default_factory=list)
    url: str = ""
    component: str | None = None
    milestone: str | None = None
    version: str | None = None
    reporter: str | None = None
    assignee: str | None = None
    reporter_name: str = ""
    assignee_name: str = ""
