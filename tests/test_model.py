"""Archive persistence: what is saved must load back identically."""

from __future__ import annotations

from bitbucket_export.model import (
    SCHEMA_VERSION,
    Archive,
    Asset,
    Comment,
    Content,
    Issue,
    Repository,
    User,
)


def _sample_archive() -> Archive:
    archive = Archive(
        repository=Repository(workspace="ws", slug="repo"),
        title="Sample",
        generated_at="2026-07-27T00:00:00+00:00",
    )
    archive.issues = [
        Issue(
            id=1,
            title="First issue",
            state="open",
            content=Content(markdown="Hello ![a](https://x.example/a.png)", markup="markdown"),
            comments=[Comment(id=5, user="u1", content=Content(markdown="A comment"))],
        ),
        Issue(id=3, title="Third issue", content=Content(markdown="Legacy body", markup="creole")),
    ]
    archive.users = {"u1": User(key="u1", display_name="User One", account_id="u1")}
    digest = "a" * 64
    archive.assets = {digest: Asset(id=digest, path=f"assets/aa/{digest}.png", size=3, sha256=digest, filename="a.png")}
    archive.asset_index = {"https://x.example/a.png": digest}
    return archive


def test_save_and_load_round_trip(tmp_path):
    original = _sample_archive()
    original.save(tmp_path)

    loaded = Archive.load(tmp_path)

    assert loaded.schema_version == SCHEMA_VERSION
    assert loaded.repository.full_name == "ws/repo"
    assert [issue.id for issue in loaded.issues] == [1, 3]
    assert loaded.issues[0].comments[0].content.markdown == "A comment"
    assert loaded.issues[1].content.markup == "creole"
    assert loaded.users["u1"].display_name == "User One"
    assert loaded.path_for_url("https://x.example/a.png") == f"assets/aa/{'a' * 64}.png"
    assert loaded.display_name("u1") == "User One"


def test_save_writes_one_file_per_issue(tmp_path):
    _sample_archive().save(tmp_path)

    assert (tmp_path / "manifest.json").exists()
    assert sorted(path.name for path in (tmp_path / "issues").glob("*.json")) == ["1.json", "3.json"]


def test_save_drops_issue_files_no_longer_in_the_archive(tmp_path):
    archive = _sample_archive()
    archive.save(tmp_path)

    archive.issues = [issue for issue in archive.issues if issue.id != 3]
    archive.save(tmp_path)

    assert sorted(path.name for path in (tmp_path / "issues").glob("*.json")) == ["1.json"]
