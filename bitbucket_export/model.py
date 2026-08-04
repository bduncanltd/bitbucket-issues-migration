"""The archive schema: a self-contained, reusable record of a Bitbucket issue tracker.

An archive is a directory containing ``manifest.json``, one JSON file per issue under
``issues/``, and an ``assets/`` tree of content-addressed blobs. The schema is
deliberately *not* Bitbucket's export shape; it is normalised for consumers:

* One file per issue, named by id, so anyone analysing an archive can read, grep, or
  load a single issue without parsing the whole tracker.
* The manifest holds only what is genuinely cross-cutting — users, assets, and the
  URL index — since those are lookup tables shared by every issue.
* Issues nest their own comments, history, and attachments — no joining by ``issue`` id.
* Every binary (named attachment or inline image) is stored once under its sha256 and
  described in the ``assets`` table, whatever it was referenced by.
* ``asset_index`` maps every original Bitbucket URL to the asset holding its bytes.
  This is the bridge between the markdown, which keeps its original URLs verbatim, and
  the files on disk. A consumer rewrites a URL by looking it up here.
* Users are interned in a ``users`` table so account ids survive, and records refer to
  them by key.

Markdown is preserved exactly as authored. Nothing in this schema presumes HTML, so a
renderer, a Jira importer, or a full-text indexer can all read the same archive.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3
# Older archives still load and are rewritten in the current form on the next save:
#   1 — every issue inline in manifest.json, before the split into issues/
#   2 — before votes, watches, content markup, comment deleted flags, and the
#       repository's component/milestone/version definitions were captured
# Issues stored under an older version are re-fetched, since they predate fields that
# cannot be filled in from what is on disk.
SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2, 3})
MANIFEST_FILENAME = "manifest.json"
ASSETS_DIRNAME = "assets"
ISSUES_DIRNAME = "issues"

ORIGIN_ATTACHMENT = "attachment"
ORIGIN_INLINE_IMAGE = "inline-image"

# Bitbucket's raw user-mention syntax in markdown: ``@{atlassian-account-id}``. Part of
# the published schema surface — markdown is stored verbatim, so consumers that want to
# show names (or real mentions) match this and look the id up in the ``users`` table.
MENTION_RE = re.compile(r"@\{([^}\s]+)\}")


@dataclass
class User:
    """A Bitbucket account, interned so records can refer to it by ``key``."""

    key: str
    display_name: str = ""
    account_id: str | None = None
    nickname: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "display_name": self.display_name,
            "account_id": self.account_id,
            "nickname": self.nickname,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> User:
        return cls(
            key=data["key"],
            display_name=data.get("display_name") or "",
            account_id=data.get("account_id"),
            nickname=data.get("nickname"),
        )


@dataclass
class Asset:
    """One stored binary, addressed by the sha256 of its content."""

    id: str
    path: str
    size: int
    sha256: str
    content_type: str | None = None
    filename: str = ""
    origins: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "content_type": self.content_type,
            "filename": self.filename,
            "origins": sorted(self.origins),
            "source_urls": sorted(self.source_urls),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Asset:
        return cls(
            id=data["id"],
            path=data["path"],
            size=data.get("size", 0),
            sha256=data.get("sha256") or data["id"],
            content_type=data.get("content_type"),
            filename=data.get("filename") or "",
            origins=list(data.get("origins") or []),
            source_urls=list(data.get("source_urls") or []),
        )


@dataclass
class UnresolvedAsset:
    """A referenced binary that could not be stored, kept so gaps stay auditable."""

    url: str
    reason: str
    origin: str = ORIGIN_INLINE_IMAGE
    filename: str = ""
    referenced_by: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "reason": self.reason,
            "origin": self.origin,
            "filename": self.filename,
            "referenced_by": sorted(self.referenced_by),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UnresolvedAsset:
        return cls(
            url=data["url"],
            reason=data.get("reason") or "",
            origin=data.get("origin") or ORIGIN_INLINE_IMAGE,
            filename=data.get("filename") or "",
            referenced_by=list(data.get("referenced_by") or []),
        )


@dataclass
class Content:
    """Authored source text plus the inline images it references.

    ``markup`` records which syntax the text is written in. Bitbucket has supported
    markdown, creole, and plaintext over the years, so a consumer cannot safely assume
    markdown without being told.
    """

    markdown: str = ""
    markup: str = "markdown"
    image_urls: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"markdown": self.markdown, "markup": self.markup, "image_urls": list(self.image_urls)}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Content:
        if not data:
            return cls()
        return cls(
            markdown=data.get("markdown") or "",
            markup=data.get("markup") or "markdown",
            image_urls=list(data.get("image_urls") or []),
        )


@dataclass
class Comment:
    """A comment. An empty body is normal — Bitbucket attaches a text-less comment to
    every field change — so ``deleted`` is what distinguishes removed text from that."""

    id: int
    user: str | None = None
    created_on: str | None = None
    updated_on: str | None = None
    deleted: bool = False
    content: Content = field(default_factory=Content)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user": self.user,
            "created_on": self.created_on,
            "updated_on": self.updated_on,
            "deleted": self.deleted,
            "content": self.content.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Comment:
        return cls(
            id=data["id"],
            user=data.get("user"),
            created_on=data.get("created_on"),
            updated_on=data.get("updated_on"),
            deleted=bool(data.get("deleted")),
            content=Content.from_dict(data.get("content")),
        )


@dataclass
class HistoryEntry:
    """One field change. Bitbucket packs several per API record; we split them out."""

    field_name: str
    old: str | None = None
    new: str | None = None
    user: str | None = None
    created_on: str | None = None
    comment_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field_name,
            "old": self.old,
            "new": self.new,
            "user": self.user,
            "created_on": self.created_on,
            "comment_id": self.comment_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HistoryEntry:
        return cls(
            field_name=data.get("field") or "",
            old=data.get("old"),
            new=data.get("new"),
            user=data.get("user"),
            created_on=data.get("created_on"),
            comment_id=data.get("comment_id"),
        )


@dataclass
class Attachment:
    """A named file attached to an issue. ``asset_id`` is None if it could not be stored."""

    filename: str
    source_url: str = ""
    asset_id: str | None = None
    user: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "source_url": self.source_url,
            "asset_id": self.asset_id,
            "user": self.user,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Attachment:
        return cls(
            filename=data.get("filename") or "",
            source_url=data.get("source_url") or "",
            asset_id=data.get("asset_id"),
            user=data.get("user"),
        )


@dataclass
class Issue:
    id: int
    title: str = ""
    state: str | None = None
    kind: str | None = None
    priority: str | None = None
    reporter: str | None = None
    assignee: str | None = None
    created_on: str | None = None
    updated_on: str | None = None
    edited_on: str | None = None
    milestone: str | None = None
    component: str | None = None
    version: str | None = None
    url: str = ""
    # Bitbucket exposes counts only; it has no endpoint listing who voted or watched.
    votes: int = 0
    watches: int = 0
    content: Content = field(default_factory=Content)
    comments: list[Comment] = field(default_factory=list)
    history: list[HistoryEntry] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "state": self.state,
            "kind": self.kind,
            "priority": self.priority,
            "reporter": self.reporter,
            "assignee": self.assignee,
            "created_on": self.created_on,
            "updated_on": self.updated_on,
            "edited_on": self.edited_on,
            "milestone": self.milestone,
            "component": self.component,
            "version": self.version,
            "url": self.url,
            "votes": self.votes,
            "watches": self.watches,
            "content": self.content.to_dict(),
            "comments": [comment.to_dict() for comment in self.comments],
            "history": [entry.to_dict() for entry in self.history],
            "attachments": [attachment.to_dict() for attachment in self.attachments],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Issue:
        return cls(
            id=data["id"],
            title=data.get("title") or "",
            state=data.get("state"),
            kind=data.get("kind"),
            priority=data.get("priority"),
            reporter=data.get("reporter"),
            assignee=data.get("assignee"),
            created_on=data.get("created_on"),
            updated_on=data.get("updated_on"),
            edited_on=data.get("edited_on"),
            milestone=data.get("milestone"),
            component=data.get("component"),
            version=data.get("version"),
            url=data.get("url") or "",
            votes=data.get("votes") or 0,
            watches=data.get("watches") or 0,
            content=Content.from_dict(data.get("content")),
            comments=[Comment.from_dict(row) for row in data.get("comments") or []],
            history=[HistoryEntry.from_dict(row) for row in data.get("history") or []],
            attachments=[Attachment.from_dict(row) for row in data.get("attachments") or []],
        )


@dataclass
class TrackerDefinition:
    """A component, milestone, or version as *defined on the repository*.

    Captured separately from the values issues happen to use, because a tracker can
    define one nobody has used yet and that is still part of its structure. Anything
    recreating the tracker elsewhere needs the definitions, not just the references.
    """

    name: str
    id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrackerDefinition:
        return cls(name=data.get("name") or "", id=data.get("id"))


@dataclass
class Repository:
    workspace: str
    slug: str

    @property
    def full_name(self) -> str:
        return f"{self.workspace}/{self.slug}"

    @property
    def url(self) -> str:
        return f"https://bitbucket.org/{self.full_name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "slug": self.slug,
            "full_name": self.full_name,
            "url": self.url,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Repository:
        return cls(workspace=data["workspace"], slug=data["slug"])


@dataclass
class Archive:
    """The whole archive: metadata, issues, and the asset tables that bind them."""

    repository: Repository
    title: str = ""
    generated_at: str = ""
    schema_version: int = SCHEMA_VERSION
    issues: list[Issue] = field(default_factory=list)
    users: dict[str, User] = field(default_factory=dict)
    components: list[TrackerDefinition] = field(default_factory=list)
    milestones: list[TrackerDefinition] = field(default_factory=list)
    versions: list[TrackerDefinition] = field(default_factory=list)
    assets: dict[str, Asset] = field(default_factory=dict)
    asset_index: dict[str, str] = field(default_factory=dict)
    unresolved_assets: list[UnresolvedAsset] = field(default_factory=list)

    # ---- asset lookup ------------------------------------------------------

    def asset_for_url(self, url: str) -> Asset | None:
        """Resolve an original Bitbucket URL to its stored asset, if we have the bytes."""

        asset_id = self.asset_index.get(url)
        return self.assets.get(asset_id) if asset_id else None

    def path_for_url(self, url: str) -> str | None:
        """Resolve an original Bitbucket URL to an archive-relative file path."""

        asset = self.asset_for_url(url)
        return asset.path if asset else None

    def display_name(self, user_key: str | None) -> str:
        if not user_key:
            return ""
        user = self.users.get(user_key)
        return user.display_name if user else user_key

    def issue_ids(self) -> set[int]:
        return {issue.id for issue in self.issues}

    # ---- persistence -------------------------------------------------------

    def manifest_dict(self) -> dict[str, Any]:
        """The cross-cutting tables. Issues live in their own files alongside this."""

        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": self.generated_at,
            "title": self.title,
            "repository": self.repository.to_dict(),
            "stats": {
                "issues": len(self.issues),
                "comments": sum(len(issue.comments) for issue in self.issues),
                "attachments": sum(len(issue.attachments) for issue in self.issues),
                "assets": len(self.assets),
                "asset_bytes": sum(asset.size for asset in self.assets.values()),
                "unresolved_assets": len(self.unresolved_assets),
            },
            "users": {key: user.to_dict() for key, user in sorted(self.users.items())},
            "components": [entry.to_dict() for entry in self.components],
            "milestones": [entry.to_dict() for entry in self.milestones],
            "versions": [entry.to_dict() for entry in self.versions],
            "assets": {key: asset.to_dict() for key, asset in sorted(self.assets.items())},
            "asset_index": dict(sorted(self.asset_index.items())),
            "unresolved_assets": [entry.to_dict() for entry in self.unresolved_assets],
        }

    def to_dict(self) -> dict[str, Any]:
        """The whole archive as one object, for callers that want a single blob."""

        return {
            **self.manifest_dict(),
            "issues": [issue.to_dict() for issue in sorted(self.issues, key=lambda issue: issue.id)],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], issues: list[Issue] | None = None) -> Archive:
        version = data.get("schema_version")
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(
                f"Unsupported archive schema_version {version!r}; this tool reads {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
            )
        if issues is None:
            # Version 1 kept every issue inline in the manifest.
            issues = [Issue.from_dict(row) for row in data.get("issues") or []]
        return cls(
            repository=Repository.from_dict(data["repository"]),
            title=data.get("title") or "",
            generated_at=data.get("generated_at") or "",
            # Keep the version this archive was written with, so the exporter can tell
            # that its issues predate fields it now captures. Saving writes the current
            # version regardless.
            schema_version=version,
            issues=issues,
            users={key: User.from_dict(row) for key, row in (data.get("users") or {}).items()},
            components=[TrackerDefinition.from_dict(row) for row in data.get("components") or []],
            milestones=[TrackerDefinition.from_dict(row) for row in data.get("milestones") or []],
            versions=[TrackerDefinition.from_dict(row) for row in data.get("versions") or []],
            assets={key: Asset.from_dict(row) for key, row in (data.get("assets") or {}).items()},
            asset_index=dict(data.get("asset_index") or {}),
            unresolved_assets=[UnresolvedAsset.from_dict(row) for row in data.get("unresolved_assets") or []],
        )

    def save(self, archive_dir: Path) -> Path:
        """Write ``manifest.json`` plus one file per issue under ``issues/``."""

        archive_dir.mkdir(parents=True, exist_ok=True)
        self.generated_at = self.generated_at or datetime.now(UTC).isoformat()

        issues_dir = archive_dir / ISSUES_DIRNAME
        issues_dir.mkdir(parents=True, exist_ok=True)

        written: set[str] = set()
        for issue in sorted(self.issues, key=lambda issue: issue.id):
            issue_path = issues_dir / f"{issue.id}.json"
            _write_json(issue_path, issue.to_dict())
            written.add(issue_path.name)

        # Drop issue files that are no longer part of the archive, which happens after
        # a --refresh against a repository whose issues were deleted.
        for stale in issues_dir.glob("*.json"):
            if stale.name not in written:
                stale.unlink()

        manifest_path = archive_dir / MANIFEST_FILENAME
        _write_json(manifest_path, self.manifest_dict())
        return manifest_path

    @classmethod
    def load(cls, archive_dir: Path) -> Archive:
        manifest_path = archive_dir / MANIFEST_FILENAME
        if not manifest_path.exists():
            raise FileNotFoundError(f"No {MANIFEST_FILENAME} in {archive_dir}. Export the repository first.")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if "issues" in manifest:
            # Version 1 layout: issues inline. Read them, and the next save splits them.
            return cls.from_dict(manifest)

        issues_dir = archive_dir / ISSUES_DIRNAME
        issues = [
            Issue.from_dict(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(issues_dir.glob("*.json"), key=_issue_file_sort_key)
        ]
        return cls.from_dict(manifest, issues=issues)


def _issue_file_sort_key(path: Path) -> tuple[int, str]:
    """Sort issue files numerically, tolerating any that are not plain numbers."""

    return (int(path.stem), "") if path.stem.isdigit() else (1 << 62, path.stem)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically, so an interrupted run cannot leave a truncated file."""

    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)
