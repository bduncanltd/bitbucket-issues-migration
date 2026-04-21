"""HTML and markdown rendering helpers for the Bitbucket issue archive."""

from __future__ import annotations

import html
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlparse

ZERO_WIDTH_NON_JOINER = "\u200c"

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
LIST_ITEM_RE = re.compile(r"^(\s*)([*+-]|\d+\.)\s+(.*)$")
TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")
MARKDOWN_URL_PATTERN = r"[^\s<>()]+(?:\([^\s<>()]*\)[^\s<>()]*)*"
IMAGE_RE = re.compile(rf"!\[(.*?)\]\(({MARKDOWN_URL_PATTERN})\)(?:\{{[^}}]*\}})?")
LINKED_IMAGE_RE = re.compile(
    rf"\[!\[(.*?)\]\(({MARKDOWN_URL_PATTERN})\)\]\(({MARKDOWN_URL_PATTERN})\)(?:\{{[^}}]*\}})?"
)
LINK_RE = re.compile(rf"\[(.*?)\]\(({MARKDOWN_URL_PATTERN})\)")
TOKEN_RE = re.compile(
    r"`[^`]+`"
    rf"|\[!\[[^\]]*\]\({MARKDOWN_URL_PATTERN}\)\]\({MARKDOWN_URL_PATTERN}\)(?:\{{[^}}]*\}})?"
    rf"|!\[[^\]]*\]\({MARKDOWN_URL_PATTERN}\)(?:\{{[^}}]*\}})?"
    rf"|\[[^\]]+\]\({MARKDOWN_URL_PATTERN}\)"
    r"|https?://[^\s<>()]+(?:\([^\s<>()]*\)[^\s<>()]*)*"
)
MALFORMED_NESTED_IMAGE_RE = re.compile(
    rf"!\[\s*!\[(?P<alt>.*?)\]\((?P<inner_url>{MARKDOWN_URL_PATTERN})\)\s*\]\((?P<outer_url>{MARKDOWN_URL_PATTERN})\)",
    re.DOTALL,
)
ESCAPED_MARKDOWN_RE = re.compile(r"\\([\\`*_{}\[\]()#+.!|~-])")
STRIKETHROUGH_RE = re.compile(r"~~(.*?)~~")
BOLD_RE = re.compile(r"\*\*(.*?)\*\*")
ITALIC_RE = re.compile(r"(?<!\*)\*(?!\s)(.*?)(?<!\s)\*(?!\*)")
PLAIN_ISSUE_REF_RE = re.compile(r"(?<![\w/])#(\d+)\b")
MIN_TABLE_DIVIDERS = 2


class AssetLocalizerLike(Protocol):
    attachment_url_map: dict[str, str]
    issue_ids: set[int]

    def localize_link(self, url: str) -> str: ...

    def localize_inline_image(self, url: str, context: dict[str, str] | None = None) -> str | None: ...


@dataclass(frozen=True)
class IssuePageData:
    issue: dict
    archive_title: str
    repo_slug: str | None
    comments: Sequence[dict]
    logs: Sequence[dict]
    attachments: Sequence[dict]


def render_issue_page(
    page: IssuePageData,
    localizer: AssetLocalizerLike,
) -> str:
    issue = page.issue
    archive_title = page.archive_title
    repo_slug = page.repo_slug
    comments = page.comments
    logs = page.logs
    attachments = page.attachments

    issue_title = f"#{issue['id']} - {issue['title']}"
    original_issue_url = f"https://bitbucket.org/{repo_slug}/issues/{issue['id']}" if repo_slug is not None else None
    issue_render_context = build_render_context(issue, repo_slug)

    description_html = render_markdown(issue.get("content") or "", localizer, issue_render_context)
    attachments_html = render_attachments(attachments, localizer)
    comments_html = render_comments(comments, issue, repo_slug, localizer)
    history_html = render_history(logs)

    metadata_pairs = [
        ("Reporter", issue.get("reporter")),
        ("Assignee", issue.get("assignee")),
        ("Created", format_datetime(issue.get("created_on"))),
        ("Updated", format_datetime(issue.get("updated_on"))),
        ("Edited", format_datetime(issue.get("edited_on"))),
        ("Content Updated", format_datetime(issue.get("content_updated_on"))),
        ("Kind", issue.get("kind")),
        ("Status", issue.get("status")),
        ("Priority", issue.get("priority")),
        ("Milestone", issue.get("milestone")),
        ("Component", issue.get("component")),
        ("Version", issue.get("version")),
        ("Watchers", render_joined(issue.get("watchers") or [])),
        ("Voters", render_joined(issue.get("voters") or [])),
    ]

    metadata_html_parts = []
    for label, value in metadata_pairs:
        value_html = html.escape(value) if value else '<span class="muted">-</span>'
        metadata_html_parts.append(f"<dt>{html.escape(label)}</dt><dd>{value_html}</dd>")
    metadata_html = "\n".join(metadata_html_parts)

    topbar_links = ['<a class="pill-link" href="index.html">Back to index</a>']
    if original_issue_url is not None:
        topbar_links.append(
            f'<a class="pill-link" href="{html.escape(original_issue_url)}" target="_blank" rel="noreferrer">'
            "Original Bitbucket issue</a>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(issue_title)} - {html.escape(archive_title)}</title>
  <link rel="stylesheet" href="assets/style.css">
</head>
<body>
  <div class="page-shell">
    <div class="topbar">
      <div>
        <p>{html.escape(archive_title)}</p>
        <h1>{html.escape(issue_title)}</h1>
      </div>
      <div class="topbar-actions">
        {" ".join(topbar_links)}
      </div>
    </div>

    <div class="issue-layout">
      <div class="stack">
        <section class="card">
          <h2 class="issue-title">{html.escape(issue["title"])}</h2>
          <div class="issue-subtitle">
            {render_badge(issue.get("status"), "status")}
            {render_badge(issue.get("priority"), "priority")}
            <span>Created {html.escape(format_datetime(issue.get("created_on")))}</span>
            <span>Updated {html.escape(format_datetime(issue.get("updated_on")))}</span>
          </div>
        </section>

        <section class="card">
          <h2 class="section-title">Description</h2>
          <div class="content">
            {description_html}
          </div>
        </section>

        <section class="card">
          <h2 class="section-title">Attachments ({len(attachments)})</h2>
          {attachments_html}
        </section>

        <section class="card">
          <h2 class="section-title">Comments ({len(comments)})</h2>
          {comments_html}
        </section>

        <section class="card">
          <h2 class="section-title">Change History ({len(logs)})</h2>
          {history_html}
        </section>
      </div>

      <aside class="stack">
        <section class="card meta-grid">
          <h2 class="section-title">Metadata</h2>
          <dl>
            {metadata_html}
          </dl>
        </section>
      </aside>
    </div>
  </div>
</body>
</html>
"""


def render_index_page(
    archive_title: str,
    issues: Sequence[dict],
    comments_by_issue: defaultdict[int, list[dict]],
    attachments_by_issue: defaultdict[int, list[dict]],
) -> str:
    rows = []
    for issue in issues:
        issue_id = issue["id"]
        rows.append(
            f"""
            <tr data-search="{html.escape(build_issue_search_text(issue))}">
              <td><a class="issue-title-link" href="{issue_id}.html">#{issue_id}</a></td>
              <td><a class="issue-title-link" href="{issue_id}.html">{html.escape(issue["title"])}</a></td>
              <td>{render_badge(issue.get("status"), "status")}</td>
              <td>{render_badge(issue.get("priority"), "priority")}</td>
              <td>{html.escape(issue.get("assignee") or "-")}</td>
              <td>{html.escape(format_datetime(issue.get("updated_on")))}</td>
              <td>{len(comments_by_issue[issue_id])}</td>
              <td>{len(attachments_by_issue[issue_id])}</td>
            </tr>
            """.strip()
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(archive_title)} - Bitbucket Issue Archive</title>
  <link rel="stylesheet" href="assets/style.css">
</head>
<body>
  <div class="page-shell">
    <div class="topbar">
      <div>
        <p>Static archive</p>
        <h1>{html.escape(archive_title)}</h1>
      </div>
      <div class="topbar-actions">
        <span class="pill-link">{len(issues)} issues</span>
      </div>
    </div>

    <div class="search-row">
      <input id="issue-search" type="search" placeholder="Filter issues by title, id, assignee, status, or priority">
    </div>

    <table class="issue-table">
      <thead>
        <tr>
          <th>ID</th>
          <th>Title</th>
          <th>Status</th>
          <th>Priority</th>
          <th>Assignee</th>
          <th>Updated</th>
          <th>Comments</th>
          <th>Attachments</th>
        </tr>
      </thead>
      <tbody id="issue-table-body">
        {" ".join(rows)}
      </tbody>
    </table>

    <p class="footer-note">
      This archive was generated from the Bitbucket JSON export. Named issue attachments are copied into
      <code>assets/attachments/</code>. Inline images embedded in descriptions or comments are only available if they
      were supplied separately or downloaded before Bitbucket access disappears.
    </p>
  </div>

  <script>
    const search = document.getElementById("issue-search");
    const rows = Array.from(document.querySelectorAll("#issue-table-body tr"));

    search.addEventListener("input", function () {{
      const needle = search.value.trim().toLowerCase();
      rows.forEach(function (row) {{
        const haystack = (row.getAttribute("data-search") || "").toLowerCase();
        row.style.display = !needle || haystack.includes(needle) ? "" : "none";
      }});
    }});
  </script>
</body>
</html>
"""


def render_attachments(attachments: Sequence[dict], localizer: AssetLocalizerLike) -> str:
    if not attachments:
        return '<p class="muted">No named issue attachments were included in the export metadata.</p>'

    items = []
    for attachment in attachments:
        archive_path = localizer.attachment_url_map.get(attachment["url"])
        link_html = (
            f'<a href="{html.escape(archive_path)}">{html.escape(attachment["filename"])}</a>'
            if archive_path is not None
            else html.escape(attachment["filename"])
        )
        items.append(
            f"""
            <div class="attachment-item">
              <div>{link_html}</div>
              <div class="attachment-meta">Uploaded by {html.escape(attachment["user"])}</div>
            </div>
            """.strip()
        )

    return f'<div class="attachments-list">{" ".join(items)}</div>'


def render_comments(
    comments: Sequence[dict],
    issue: dict,
    repo_slug: str | None,
    localizer: AssetLocalizerLike,
) -> str:
    if not comments:
        return '<p class="muted">No comments.</p>'

    rendered_comments = []
    for comment in comments:
        comment_render_context = build_render_context(issue, repo_slug, source_type="comment", comment=comment)
        content_html = render_markdown(comment.get("content") or "", localizer, comment_render_context)
        rendered_comments.append(
            f"""
            <article class="comment-item" id="comment-{comment["id"]}">
              <div class="comment-head">
                <span class="comment-author">{html.escape(comment["user"])}</span>
                <span class="comment-date">{html.escape(format_datetime(comment.get("created_on")))}</span>
              </div>
              <div class="content">
                {content_html}
              </div>
            </article>
            """.strip()
        )

    return f'<div class="comments-list">{" ".join(rendered_comments)}</div>'


def render_history(logs: Sequence[dict]) -> str:
    if not logs:
        return '<p class="muted">No change history records.</p>'

    items = []
    for log in logs:
        field = log.get("field") or "field"
        changed_from = log.get("changed_from") or "-"
        changed_to = log.get("changed_to") or "-"
        comment_html = ""
        if log.get("comment") is not None:
            comment_html = (
                f' <a href="#comment-{log["comment"]}">Related comment #{html.escape(str(log["comment"]))}</a>'
            )

        items.append(
            f"""
            <div class="history-item">
              <div class="history-head">
                <span class="history-user">{html.escape(log.get("user") or "-")}</span>
                <span class="history-date">{html.escape(format_datetime(log.get("created_on")))}</span>
              </div>
              <div>
                Changed <strong>{html.escape(field)}</strong> from
                <code>{html.escape(changed_from)}</code> to <code>{html.escape(changed_to)}</code>.{comment_html}
              </div>
            </div>
            """.strip()
        )

    return f'<div class="history-list">{" ".join(items)}</div>'


def build_render_context(
    issue: dict,
    repo_slug: str | None,
    source_type: str = "issue",
    comment: dict | None = None,
) -> dict[str, str]:
    original_issue_url = f"https://bitbucket.org/{repo_slug}/issues/{issue['id']}" if repo_slug is not None else ""
    page_path = f"{issue['id']}.html"
    comment_id = ""
    user = issue.get("reporter") or ""
    created_on = issue.get("content_updated_on") or issue.get("created_on") or ""

    if comment is not None:
        comment_id = str(comment["id"])
        page_path = f"{page_path}#comment-{comment['id']}"
        user = comment.get("user") or ""
        created_on = comment.get("created_on") or ""

    return {
        "issue_id": str(issue["id"]),
        "issue_title": issue.get("title") or "",
        "source_type": source_type,
        "comment_id": comment_id,
        "user": user,
        "created_on": created_on,
        "page_path": page_path,
        "original_issue_url": original_issue_url,
    }


def render_markdown(text: str, localizer: AssetLocalizerLike, context: dict[str, str] | None = None) -> str:
    normalized = normalize_markdown(text)
    if not normalized:
        return '<p class="muted">No content.</p>'

    lines = normalized.split("\n")
    blocks: list[str] = []
    index = 0

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            index += 1
            continue

        if stripped.startswith("```"):
            block_html, index = render_fenced_code(lines, index)
            blocks.append(block_html)
            continue

        if is_table_block(lines, index):
            block_html, index = render_table(lines, index, localizer, context)
            blocks.append(block_html)
            continue

        heading_match = HEADING_RE.match(stripped)
        if heading_match is not None:
            level = len(heading_match.group(1))
            blocks.append(f"<h{level}>{render_inline(heading_match.group(2), localizer, context)}</h{level}>")
            index += 1
            continue

        if stripped.startswith(">"):
            blockquote_lines = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                blockquote_lines.append(lines[index].strip()[1:].lstrip())
                index += 1
            quote_html = render_markdown("\n".join(blockquote_lines), localizer, context)
            blocks.append(f"<blockquote>{quote_html}</blockquote>")
            continue

        list_match = LIST_ITEM_RE.match(line)
        if list_match is not None:
            block_html, index = render_list(lines, index, localizer, context)
            blocks.append(block_html)
            continue

        paragraph_lines = [line]
        index += 1
        while index < len(lines) and should_continue_paragraph(lines[index]):
            paragraph_lines.append(lines[index])
            index += 1

        paragraph_html = "<br>\n".join(render_inline(chunk.strip(), localizer, context) for chunk in paragraph_lines)
        blocks.append(f"<p>{paragraph_html}</p>")

    return "\n".join(blocks)


def render_fenced_code(lines: Sequence[str], start_index: int) -> tuple[str, int]:
    first_line = lines[start_index].strip()
    language = html.escape(first_line[3:].strip())
    code_lines: list[str] = []
    index = start_index + 1
    while index < len(lines) and not lines[index].strip().startswith("```"):
        code_lines.append(lines[index])
        index += 1

    if index < len(lines):
        index += 1

    class_attr = f' class="language-{language}"' if language else ""
    code_html = html.escape("\n".join(code_lines))
    return f"<pre><code{class_attr}>{code_html}</code></pre>", index


def render_table(
    lines: Sequence[str],
    start_index: int,
    localizer: AssetLocalizerLike,
    context: dict[str, str] | None = None,
) -> tuple[str, int]:
    header_cells = split_table_row(lines[start_index])
    rows = []
    index = start_index + 2

    while index < len(lines):
        line = lines[index]
        if "|" not in line or not line.strip():
            break
        rows.append(split_table_row(line))
        index += 1

    header_html = "".join(f"<th>{render_inline(cell, localizer, context)}</th>" for cell in header_cells)
    body_html = "".join(
        "<tr>" + "".join(f"<td>{render_inline(cell, localizer, context)}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{header_html}</tr></thead><tbody>{body_html}</tbody></table>", index


def render_list(
    lines: Sequence[str],
    start_index: int,
    localizer: AssetLocalizerLike,
    context: dict[str, str] | None = None,
) -> tuple[str, int]:
    items: list[str] = []
    list_tag = "ul"
    index = start_index

    while index < len(lines):
        match = LIST_ITEM_RE.match(lines[index])
        if match is None:
            break

        marker = match.group(2)
        if marker[0].isdigit():
            list_tag = "ol"

        content_lines = [match.group(3).strip()]
        index += 1

        while index < len(lines):
            next_line = lines[index]
            next_match = LIST_ITEM_RE.match(next_line)
            if next_match is not None or not next_line.strip():
                break
            content_lines.append(next_line.strip())
            index += 1

        item_html = "<br>\n".join(render_inline(line, localizer, context) for line in content_lines)
        items.append(f"<li>{item_html}</li>")

        while index < len(lines) and not lines[index].strip():
            index += 1
            break

    return f"<{list_tag}>{''.join(items)}</{list_tag}>", index


def render_inline(text: str, localizer: AssetLocalizerLike, context: dict[str, str] | None = None) -> str:
    parts: list[str] = []
    position = 0

    for match in TOKEN_RE.finditer(text):
        if match.start() > position:
            parts.append(render_plain_text(text[position : match.start()], localizer))

        token = match.group(0)
        if token.startswith("[!["):
            parts.append(render_linked_image_token(token, localizer, context))
        elif token.startswith("!["):
            parts.append(render_image_token(token, localizer, context))
        elif token.startswith("["):
            parts.append(render_link_token(token, localizer))
        elif token.startswith("`"):
            parts.append(f"<code>{html.escape(token[1:-1])}</code>")
        else:
            link = localizer.localize_link(token)
            parts.append(f'<a href="{html.escape(link)}">{html.escape(token)}</a>')

        position = match.end()

    if position < len(text):
        parts.append(render_plain_text(text[position:], localizer))

    return "".join(parts)


def render_plain_text(text: str, localizer: AssetLocalizerLike) -> str:
    escaped = html.escape(text)
    escaped = STRIKETHROUGH_RE.sub(r"<del>\1</del>", escaped)
    escaped = BOLD_RE.sub(r"<strong>\1</strong>", escaped)
    escaped = ITALIC_RE.sub(r"<em>\1</em>", escaped)

    def replace_issue_ref(match: re.Match[str]) -> str:
        issue_id = int(match.group(1))
        if issue_id in localizer.issue_ids:
            return f'<a href="{issue_id}.html">#{issue_id}</a>'
        return match.group(0)

    return PLAIN_ISSUE_REF_RE.sub(replace_issue_ref, escaped)


def render_image_token(
    token: str,
    localizer: AssetLocalizerLike,
    context: dict[str, str] | None = None,
) -> str:
    match = IMAGE_RE.fullmatch(token)
    if match is None:
        return html.escape(token)

    alt_text = match.group(1)
    url = match.group(2)
    localized = localizer.localize_inline_image(url, context)

    if localized is None:
        escaped_url = html.escape(url)
        escaped_alt = html.escape(alt_text or derive_url_filename(url))
        return (
            '<div class="missing-inline-image">'
            f"<p>Inline image unavailable offline: {escaped_alt}</p>"
            f'<a href="{escaped_url}" target="_blank" rel="noreferrer">{escaped_url}</a>'
            "</div>"
        )

    return f'<img src="{html.escape(localized)}" alt="{html.escape(alt_text)}">'


def render_linked_image_token(
    token: str,
    localizer: AssetLocalizerLike,
    context: dict[str, str] | None = None,
) -> str:
    match = LINKED_IMAGE_RE.fullmatch(token)
    if match is None:
        return html.escape(token)

    alt_text = match.group(1)
    image_url = match.group(2)
    target_url = localizer.localize_link(match.group(3))
    localized = localizer.localize_inline_image(image_url, context)

    if localized is None:
        escaped_url = html.escape(image_url)
        escaped_alt = html.escape(alt_text or derive_url_filename(image_url))
        return (
            '<div class="missing-inline-image">'
            f"<p>Inline image unavailable offline: {escaped_alt}</p>"
            f'<a href="{escaped_url}" target="_blank" rel="noreferrer">{escaped_url}</a>'
            "</div>"
        )

    return (
        f'<a href="{html.escape(target_url)}" target="_blank" rel="noreferrer">'
        f'<img src="{html.escape(localized)}" alt="{html.escape(alt_text)}">'
        "</a>"
    )


def render_link_token(token: str, localizer: AssetLocalizerLike) -> str:
    match = LINK_RE.fullmatch(token)
    if match is None:
        return html.escape(token)

    label = render_plain_text(match.group(1), localizer)
    target = localizer.localize_link(match.group(2))
    return f'<a href="{html.escape(target)}">{label}</a>'


def normalize_markdown(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace(ZERO_WIDTH_NON_JOINER, "")
    normalized = ESCAPED_MARKDOWN_RE.sub(r"\1", normalized)
    normalized = normalize_malformed_nested_images(normalized)
    return normalized.strip()


def normalize_malformed_nested_images(text: str) -> str:
    def replace_nested_image(match: re.Match[str]) -> str:
        alt_text = match.group("alt").strip()
        outer_url = match.group("outer_url")
        return f"[![{alt_text}]({outer_url})]({outer_url})"

    return MALFORMED_NESTED_IMAGE_RE.sub(replace_nested_image, text)


def should_continue_paragraph(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("```") or stripped.startswith(">"):
        return False
    if HEADING_RE.match(stripped) is not None:
        return False
    if LIST_ITEM_RE.match(line) is not None:
        return False
    return not is_table_start_line(stripped)


def is_table_block(lines: Sequence[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    return is_table_start_line(lines[index]) and TABLE_SEPARATOR_RE.match(lines[index + 1].strip()) is not None


def is_table_start_line(line: str) -> bool:
    stripped = line.strip()
    return "|" in stripped and stripped.count("|") >= MIN_TABLE_DIVIDERS


def split_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def derive_url_filename(url: str) -> str:
    parsed = urlparse(url)
    filename = unquote(Path(parsed.path).name)
    return filename or "downloaded-asset"


def render_badge(value: str | None, prefix: str) -> str:
    if not value:
        return '<span class="badge muted">Unknown</span>'
    class_name = slugify_css(value)
    return f'<span class="badge {prefix}-{class_name}">{html.escape(value)}</span>'


def slugify_css(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "unknown"


def render_joined(values: Sequence[str]) -> str:
    if not values:
        return "-"
    return ", ".join(values)


def format_datetime(value: str | None) -> str:
    if not value:
        return "-"

    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value

    dt_utc = dt.astimezone(UTC)
    return dt_utc.strftime("%Y-%m-%d %H:%M UTC")


def build_issue_search_text(issue: dict) -> str:
    parts = [
        str(issue["id"]),
        issue.get("title") or "",
        issue.get("status") or "",
        issue.get("priority") or "",
        issue.get("assignee") or "",
        issue.get("reporter") or "",
        issue.get("component") or "",
        issue.get("kind") or "",
    ]
    return " ".join(parts)


def comment_sort_key(comment: dict) -> tuple[str, int]:
    return comment.get("created_on") or "", comment["id"]


def log_sort_key(log: dict) -> tuple[str, int]:
    comment_id = log.get("comment")
    return log.get("created_on") or "", int(comment_id or 0)
