"""Site generation from an archive, including the missing-asset guard."""

from __future__ import annotations

import pytest

from bitbucket_export.model import Archive, Asset, Content, Issue, Repository, User
from static_site.cli import main
from static_site.site import MissingAssetError, _resolve_mentions, render_site

DIGEST = "b" * 64
ASSET_PATH = f"assets/bb/{DIGEST}.png"
IMAGE_URL = "https://x.example/pasted.png"


def _archive() -> Archive:
    archive = Archive(repository=Repository(workspace="ws", slug="repo"), title="Sample")
    archive.issues = [
        Issue(
            id=1,
            title="First issue",
            content=Content(markdown=f"Look: ![shot]({IMAGE_URL})", image_urls=[IMAGE_URL]),
        )
    ]
    archive.assets = {DIGEST: Asset(id=DIGEST, path=ASSET_PATH, size=9, sha256=DIGEST, filename="pasted.png")}
    archive.asset_index = {IMAGE_URL: DIGEST}
    return archive


def test_renders_pages_and_copies_assets(tmp_path):
    archive_dir = tmp_path / "archive"
    (archive_dir / ASSET_PATH).parent.mkdir(parents=True)
    (archive_dir / ASSET_PATH).write_bytes(b"png-bytes")
    output_dir = tmp_path / "site"

    result = render_site(_archive(), output_dir, archive_dir)

    assert result.page_count == 1
    assert result.missing_asset_count == 0
    assert (output_dir / "index.html").exists()
    assert (output_dir / ASSET_PATH).read_bytes() == b"png-bytes"
    assert ASSET_PATH in (output_dir / "1.html").read_text(encoding="utf-8")


def test_missing_asset_file_aborts_before_writing_anything(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    output_dir = tmp_path / "site"

    with pytest.raises(MissingAssetError, match=ASSET_PATH):
        render_site(_archive(), output_dir, archive_dir)

    assert not output_dir.exists()


def test_cli_defaults_output_to_site_dir_mirroring_the_archive(tmp_path, monkeypatch):
    archive_dir = tmp_path / "archive"
    (archive_dir / ASSET_PATH).parent.mkdir(parents=True)
    (archive_dir / ASSET_PATH).write_bytes(b"png-bytes")
    _archive().save(archive_dir)
    monkeypatch.chdir(tmp_path)

    assert main([str(archive_dir)]) == 0
    assert (tmp_path / ".site" / "ws" / "repo" / "index.html").exists()


def test_mentions_resolve_to_display_names():
    archive = Archive(repository=Repository(workspace="ws", slug="repo"))
    archive.users = {"acc-1": User(key="acc-1", display_name="User One", account_id="acc-1")}

    resolved = _resolve_mentions("ask @{acc-1} or @{unknown-id}", archive)

    assert resolved == "ask @User One or @{unknown-id}"


def test_allow_missing_assets_renders_and_reports_the_gap(tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    output_dir = tmp_path / "site"

    result = render_site(_archive(), output_dir, archive_dir, allow_missing_assets=True)

    assert result.missing_asset_count == 1
    assert any("missing" in line for line in result.summary)
    assert (output_dir / "1.html").exists()
