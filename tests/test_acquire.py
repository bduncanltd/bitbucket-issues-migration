"""Incremental fetch decisions and attachment bookkeeping."""

from __future__ import annotations

from pathlib import Path

from bitbucket_export.acquire import (
    AcquireOptions,
    _download_attachments,
    _issues_needing_fetch,
    _resolve_mentioned_users,
)
from bitbucket_export.asset_store import AssetStore
from bitbucket_export.bitbucket_client import BitbucketApiError
from bitbucket_export.model import Attachment, Comment, Content, Issue, Repository, User

TIMESTAMP = "2026-01-01T00:00:00+00:00"


def _options(issue_ids: set[int] | None = None) -> AcquireOptions:
    return AcquireOptions(
        archive_dir=Path("."),
        repository=Repository(workspace="ws", slug="repo"),
        issue_ids=issue_ids or set(),
    )


def test_unchanged_issues_are_reused():
    stored = {42: Issue(id=42, updated_on=TIMESTAMP)}
    raw = [{"id": 42, "updated_on": TIMESTAMP}]

    assert _issues_needing_fetch(raw, stored, None, _options()) == []


def test_changed_and_new_issues_are_fetched():
    stored = {42: Issue(id=42, updated_on=TIMESTAMP)}
    raw = [
        {"id": 42, "updated_on": "2026-02-02T00:00:00+00:00"},
        {"id": 43, "updated_on": TIMESTAMP},
    ]

    fetched = _issues_needing_fetch(raw, stored, None, _options())
    assert [issue["id"] for issue in fetched] == [42, 43]


def test_explicitly_named_issue_ids_are_always_fetched():
    stored = {42: Issue(id=42, updated_on=TIMESTAMP)}
    raw = [{"id": 42, "updated_on": TIMESTAMP}]

    fetched = _issues_needing_fetch(raw, stored, None, _options(issue_ids={42}))
    assert [issue["id"] for issue in fetched] == [42]


class _FailingClient:
    def fetch_bytes(self, url: str) -> bytes:
        raise BitbucketApiError(f"Download {url} failed")


def test_failed_attachment_download_clears_any_stale_asset_id(tmp_path):
    store = AssetStore(archive_dir=tmp_path)
    attachment = Attachment(
        filename="a.png",
        source_url="https://x.example/a.png",
        asset_id="stale-id-from-a-previous-run",
    )

    _download_attachments(_FailingClient(), store, [(attachment, "issue-1")])

    assert attachment.asset_id is None
    assert [entry.url for entry in store.unresolved_list()] == ["https://x.example/a.png"]


class _ByteClient:
    def fetch_bytes(self, url: str) -> bytes:
        return b"png-bytes"


def test_successful_attachment_download_stores_and_links_the_asset(tmp_path):
    store = AssetStore(archive_dir=tmp_path)
    attachment = Attachment(filename="a.png", source_url="https://x.example/a.png")

    _download_attachments(_ByteClient(), store, [(attachment, "issue-1")])

    assert attachment.asset_id is not None
    stored_path = tmp_path / store.assets[attachment.asset_id].path
    assert stored_path.read_bytes() == b"png-bytes"


class _UserClient:
    """Resolves one known account id; every other lookup 404s."""

    def fetch_user(self, account_id: str) -> dict:
        if account_id == "abcd1234":
            return {"account_id": account_id, "display_name": "Mentioned Only", "nickname": "mentioned"}
        raise BitbucketApiError(f"GET users/{account_id} failed: HTTP 404")


def test_mention_only_users_are_resolved_and_interned():
    users = {"participant": User(key="participant", display_name="Already Known", account_id="participant")}
    issues = [
        Issue(
            id=1,
            content=Content(markdown="@{abcd1234} and @{participant} could confirm."),
            comments=[Comment(id=1, content=Content(markdown="cc @{gone-account}"))],
        )
    ]

    _resolve_mentioned_users(_UserClient(), issues, users)

    assert users["abcd1234"].display_name == "Mentioned Only"
    # The deleted account stays absent — its mention will render raw, not crash.
    assert "gone-account" not in users
    assert users["participant"].display_name == "Already Known"
