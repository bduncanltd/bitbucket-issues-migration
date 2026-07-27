"""The exporter's image collector must be a superset of what the renderer displays.

This is the property that keeps images from going missing: anything the site would
try to show must have been downloaded while Bitbucket still existed.
"""

from __future__ import annotations

from bitbucket_export.markdown_images import iter_image_urls
from static_site.rendering import iter_inline_image_urls

SAMPLES = [
    "![a](https://x.example/a.png)",
    "text before ![b](https://x.example/b.png?query=1) text after",
    "[![alt](https://x.example/inner.png)](https://x.example/outer.png)",
    "![![alt](https://x.example/nested-inner.png)](https://x.example/nested-outer.png)",
    "![](https://x.example/empty-alt.png)",
    "paragraph\n\n![c](https://x.example/c.png)\n\nanother paragraph",
    "two ![d](https://x.example/d.png) images ![e](https://x.example/e.png) in one line",
    "the same ![f](https://x.example/f.png) twice ![f](https://x.example/f.png)",
    "a url with parens ![g](https://x.example/g(1).png)",
]


def test_exporter_collects_a_superset_of_what_the_renderer_displays():
    for markdown in SAMPLES:
        rendered = set(iter_inline_image_urls(markdown))
        collected = set(iter_image_urls(markdown))
        assert rendered <= collected, f"renderer needs {rendered - collected} for: {markdown!r}"


def test_collects_both_halves_of_a_linked_image():
    urls = iter_image_urls("[![alt](https://x.example/inner.png)](https://x.example/outer.png)")
    assert "https://x.example/inner.png" in urls
    assert "https://x.example/outer.png" in urls


def test_deduplicates_in_first_appearance_order():
    markdown = "![a](https://x.example/1.png) ![b](https://x.example/2.png) ![c](https://x.example/1.png)"
    assert iter_image_urls(markdown) == ["https://x.example/1.png", "https://x.example/2.png"]


def test_skips_code_spans_and_fenced_blocks():
    markdown = (
        "`![inline](https://x.example/span.png)`\n"
        "```\n![fenced](https://x.example/fence.png)\n```\n"
        "![real](https://x.example/real.png)"
    )
    assert iter_image_urls(markdown) == ["https://x.example/real.png"]


def test_empty_and_imageless_markdown():
    assert iter_image_urls("") == []
    assert iter_image_urls("no images here, just [a link](https://x.example/page)") == []
