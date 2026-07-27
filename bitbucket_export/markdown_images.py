"""Find the images a Bitbucket markdown body references.

The exporter needs this to know what to download. It is an *export* concern — what
binaries does this issue depend on — and deliberately has no idea what any consumer
will later do with them.

The rule this follows: **collect a superset**. Missing an image is unrecoverable once
Bitbucket is gone; collecting a spare one costs a few kilobytes. So both halves of a
nested ``[![a](inner)](outer)`` are collected even though a given renderer will only
display one of them.

Code spans and fenced blocks *are* skipped, though. URLs in there are illustrative
rather than real, and trying to download them would produce failures that mask genuine
ones.
"""

from __future__ import annotations

import re

URL = r"[^\s<>()]+(?:\([^\s<>()]*\)[^\s<>()]*)*"
FENCED_CODE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`]+`")
# A linked image: the inner URL is the picture, the outer is where clicking goes. Some
# Bitbucket exports also contain a malformed nesting whose *outer* URL is the picture,
# so both are collected.
LINKED_IMAGE_RE = re.compile(rf"\[\s*!\[(?:.*?)\]\((?P<inner>{URL})\)\s*\]\((?P<outer>{URL})\)", re.DOTALL)
IMAGE_RE = re.compile(rf"!\[(?:.*?)\]\((?P<url>{URL})\)")


def iter_image_urls(markdown: str) -> list[str]:
    """Every image URL in ``markdown``, deduplicated, in first-appearance order."""

    if not markdown:
        return []

    stripped = INLINE_CODE_RE.sub(" ", FENCED_CODE_RE.sub(" ", markdown))

    found: list[str] = []
    seen: set[str] = set()

    def keep(url: str) -> None:
        cleaned = url.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            found.append(cleaned)

    for match in LINKED_IMAGE_RE.finditer(stripped):
        keep(match.group("inner"))
        keep(match.group("outer"))

    for match in IMAGE_RE.finditer(stripped):
        keep(match.group("url"))

    return found
