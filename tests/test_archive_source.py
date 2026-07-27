"""The Jira migrator's archive adapter: issues, users, files, and numbering."""

from __future__ import annotations

import pytest

from bitbucket_export.model import (
    Archive,
    Asset,
    Attachment,
    Comment,
    Content,
    Issue,
    Repository,
    TrackerDefinition,
    User,
)
from jira_migration.archive_source import ArchiveSource

DIGEST = "c" * 64
ASSET_PATH = f"assets/cc/{DIGEST}.png"
IMAGE_URL = "https://bitbucket.org/x/a.png"


def _archive() -> Archive:
    archive = Archive(repository=Repository(workspace="ws", slug="repo"), title="Sample")
    archive.users = {
        "acc-1": User(key="acc-1", display_name="User One", account_id="acc-1"),
        "legacy": User(key="legacy", display_name="Old Timer", account_id=None),
    }
    archive.issues = [
        Issue(
            id=1,
            title="First issue",
            kind="bug",
            state="open",
            priority="major",
            component="ui",
            reporter="acc-1",
            assignee="legacy",
            created_on="2026-01-01T00:00:00+00:00",
            updated_on="2026-01-02T00:00:00+00:00",
            content=Content(markdown="Body"),
            comments=[
                Comment(id=1, user="acc-1", created_on="2026-01-01T01:00:00+00:00", content=Content(markdown="Real")),
                Comment(id=2, user="acc-1", created_on="2026-01-01T02:00:00+00:00", content=Content(markdown="")),
            ],
            attachments=[
                Attachment(filename="a.png", source_url=IMAGE_URL, asset_id=DIGEST),
                Attachment(filename="lost.bin", source_url="https://bitbucket.org/x/lost.bin", asset_id=None),
            ],
        ),
        Issue(id=3, title="Third issue", kind="task", state="new", priority="trivial"),
    ]
    archive.assets = {DIGEST: Asset(id=DIGEST, path=ASSET_PATH, size=3, sha256=DIGEST, filename="a.png")}
    archive.asset_index = {IMAGE_URL: DIGEST}
    # "backend" is defined on the tracker but used by no issue; it must still exist
    # in Jira after migration.
    archive.components = [TrackerDefinition(name="backend")]
    return archive


def _saved_archive_dir(tmp_path, with_asset_file: bool = True):
    archive_dir = tmp_path / "archive"
    if with_asset_file:
        (archive_dir / ASSET_PATH).parent.mkdir(parents=True)
        (archive_dir / ASSET_PATH).write_bytes(b"png")
    _archive().save(archive_dir)
    return archive_dir


def test_fills_numbering_gaps_with_dummies(tmp_path):
    source = ArchiveSource(_saved_archive_dir(tmp_path))

    assert [issue.id for issue in source.issues] == [1, 2, 3]
    assert source.issues[1].summary == "Dummy issue 2"
    assert source.issues[1].kind == "task"
    assert source.issues[1].state == "invalid"


def test_drops_empty_field_change_comments(tmp_path):
    source = ArchiveSource(_saved_archive_dir(tmp_path))

    comments = source.issues[0].comments
    assert [comment.body for comment in comments] == ["Real"]
    assert comments[0].author == "User One"


def test_users_without_account_ids_keep_names_but_are_unmappable(tmp_path):
    source = ArchiveSource(_saved_archive_dir(tmp_path))
    first = source.issues[0]

    assert first.reporter == "acc-1"
    assert first.reporter_name == "User One"
    assert first.assignee is None
    assert first.assignee_name == "Old Timer"
    assert source.user_ids == {"acc-1"}


def test_attachments_resolve_to_archive_files_and_undownloaded_ones_are_skipped(tmp_path):
    source = ArchiveSource(_saved_archive_dir(tmp_path))

    attachments = source.issues[0].attachments
    assert [attachment.filename for attachment in attachments] == ["a.png"]
    assert attachments[0].path.endswith(f"{DIGEST}.png")


def test_missing_attachment_file_aborts(tmp_path):
    archive_dir = _saved_archive_dir(tmp_path, with_asset_file=False)

    with pytest.raises(RuntimeError, match="Re-run bitbucket_export"):
        ArchiveSource(archive_dir)


def test_component_names_cover_defined_and_used(tmp_path):
    source = ArchiveSource(_saved_archive_dir(tmp_path))

    assert source.component_names == {"backend", "ui"}
    assert source.issues[0].component == "ui"
    assert source.issues[1].component is None  # dummy issues carry no component


def test_inline_image_resolution(tmp_path):
    source = ArchiveSource(_saved_archive_dir(tmp_path))

    resolved = source.inline_image(IMAGE_URL)
    assert resolved is not None
    path, name, asset_id = resolved
    assert path.exists()
    assert name == "a.png"
    assert asset_id == DIGEST

    assert source.inline_image("https://bitbucket.org/x/never-captured.png") is None
