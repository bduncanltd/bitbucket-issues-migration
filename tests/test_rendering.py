"""Rendering dispatch: markdown is rendered, other markups are shown verbatim."""

from __future__ import annotations

from static_site.rendering import render_content


class _PassThroughLocalizer:
    def __init__(self) -> None:
        self.attachment_url_map: dict[str, str] = {}
        self.issue_ids: set[int] = set()

    def localize_link(self, url: str) -> str:
        return url

    def localize_inline_image(self, url: str, context: dict[str, str] | None = None) -> str | None:
        return url


def test_markdown_is_rendered():
    rendered = render_content("**bold**", "markdown", _PassThroughLocalizer())
    assert "<strong>bold</strong>" in rendered


def test_missing_markup_defaults_to_markdown():
    rendered = render_content("**bold**", None, _PassThroughLocalizer())
    assert "<strong>bold</strong>" in rendered


def test_creole_is_shown_verbatim_not_parsed_as_markdown():
    rendered = render_content("**creole is not markdown** <tag>", "creole", _PassThroughLocalizer())
    assert rendered.startswith('<pre data-markup="creole">')
    assert "**creole is not markdown**" in rendered
    assert "&lt;tag&gt;" in rendered
    assert "<strong>" not in rendered


def test_plaintext_is_escaped():
    rendered = render_content("a < b & c", "plaintext", _PassThroughLocalizer())
    assert "a &lt; b &amp; c" in rendered


def test_empty_non_markdown_content_shows_placeholder():
    rendered = render_content("", "creole", _PassThroughLocalizer())
    assert "No content." in rendered
