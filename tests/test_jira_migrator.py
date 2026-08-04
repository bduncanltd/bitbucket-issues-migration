"""Mapping Bitbucket-native vocabulary and archived images into Jira shapes."""

from __future__ import annotations

from pathlib import Path

import pytest

from jira_migration.bitbucket_issue import BitbucketComment, BitbucketIssue
from jira_migration.markup import convert_content
from jira_migration.migrator import (
    ISSUE_STATUS_MAP,
    ISSUE_TYPE_MAP,
    PRIORITY_MAP,
    _get_comment_body,
    _get_description,
    _get_issue_status,
    _get_issue_type,
    _inline_images,
    _resolve_mentions,
)

BITBUCKET_KINDS = ("bug", "enhancement", "proposal", "task")
BITBUCKET_STATES = ("new", "open", "on hold", "resolved", "closed", "invalid", "duplicate", "wontfix")
BITBUCKET_PRIORITIES = ("trivial", "minor", "major", "critical", "blocker")


def _issue(**overrides) -> BitbucketIssue:
    values = {
        "id": 7,
        "kind": "bug",
        "state": "open",
        "priority": "major",
        "summary": "Something broke",
        "description": "It **really** broke",
        "created": "2026-01-01T00:00:00+00:00",
        "updated": "2026-01-02T00:00:00+00:00",
        "url": "https://bitbucket.org/ws/repo/issues/7",
        "reporter_name": "User One",
    }
    values.update(overrides)
    return BitbucketIssue(**values)


def test_maps_cover_all_bitbucket_native_values():
    assert set(BITBUCKET_KINDS) <= set(ISSUE_TYPE_MAP)
    assert set(BITBUCKET_STATES) <= set(ISSUE_STATUS_MAP)
    assert set(BITBUCKET_PRIORITIES) <= set(PRIORITY_MAP)


def test_unknown_values_fail_fast():
    with pytest.raises(SystemExit):
        _get_issue_type(_issue(kind="mystery"))
    with pytest.raises(SystemExit):
        _get_issue_status(_issue(state="mystery"))


def test_description_converts_markdown_and_records_provenance():
    description = _get_description(_issue())

    assert "It *really* broke" in description
    assert "state: open" in description
    assert "kind: bug" in description
    assert "Reporter: User One" in description
    assert "[https://bitbucket.org/ws/repo/issues/7]" in description


def test_creole_content_is_wrapped_verbatim_not_converted():
    assert convert_content("**not markdown**", "creole") == "{noformat}\n**not markdown**\n{noformat}"

    body = _get_comment_body(BitbucketComment(author="A", created="t", body="**x**", markup="creole"))
    assert "{noformat}\n**x**\n{noformat}" in body


def test_mentions_become_real_jira_mentions_or_display_names():
    display_names = {"acc-1": "User One", "acc-2": "User Two"}
    jira_user_ids = {"acc-1"}

    resolved = _resolve_mentions("ping @{acc-1}, @{acc-2}, @{acc-3}", display_names, jira_user_ids)

    # In Jira: acc-1 is a real mention, acc-2 a plain name, acc-3 unresolvable and left raw.
    assert resolved == "ping [~accountId:acc-1], @User Two, @{acc-3}"


class _StubSource:
    def __init__(self, mapping):
        self._mapping = mapping

    def inline_image(self, url):
        return self._mapping.get(url)


def test_inline_images_resolve_and_disambiguate_filename_collisions():
    source = _StubSource(
        {
            "https://x/a": (Path("assets/aa/aaa.png"), "shot.png", "asset-a"),
            "https://x/b": (Path("assets/bb/bbb.png"), "shot.png", "asset-b"),
        }
    )
    text = "see !https://x/a! and !https://x/b! and !https://x/unknown!"

    images = _inline_images(source, 7, [text])

    assert images["https://x/a"][1] == "shot.png"
    assert images["https://x/b"][1] == "bbb.png"  # collision falls back to the unique stored name
    assert "https://x/unknown" not in images
