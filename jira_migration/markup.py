"""Converts Bitbucket Markdown to Jira Wiki Markup."""

from __future__ import annotations

import re

_NOFORMAT = "{noformat}"


_BOLD_PLACEHOLDER = "\x00B\x00"


def convert(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _convert_fenced_code_blocks(text)
    text = _convert_indented_code_blocks(text)
    text = _convert_headings(text)
    text = _convert_horizontal_rules(text)
    text = _convert_tables(text)
    text = _convert_blockquotes(text)
    text = _convert_lists(text)
    text = _convert_strikethrough(text)
    text = _convert_images(text)  # before bold/italic so URLs aren't mangled
    text = _convert_raw_urls(text)  # bare URLs before link conversion
    text = _convert_links(text)
    text = _convert_bold_placeholder(text)  # bold → placeholder
    text = _convert_italic(text)  # italic (won't re-match bold)
    text = _restore_bold(text)  # placeholder → *bold*
    text = _convert_inline_code(text)
    return text


def _convert_fenced_code_blocks(text: str) -> str:
    def replace(m: re.Match[str]) -> str:
        lang = (m.group(1) or "").strip()
        code = m.group(2)
        return f"{{code:{lang}}}\n{code}{{code}}" if lang else f"{_NOFORMAT}\n{code}{_NOFORMAT}"

    text = re.sub(r"~~~\s*(\w+)?\n(.*?)~~~", replace, text, flags=re.DOTALL)
    text = re.sub(r"```(\w+)?\n(.*?)```", replace, text, flags=re.DOTALL)
    return text


def _convert_indented_code_blocks(text: str) -> str:
    """Convert 4-space or tab indented lines to Jira {noformat} blocks.
    Only treat as code if preceded by a blank line (standard Markdown rule).
    Skips lines already inside a {noformat} or {code} block.
    """
    lines = text.split("\n")
    result: list[str] = []
    block: list[str] = []
    prev_blank = True
    in_preformatted = False

    for line in lines:
        s = line.strip()
        if not in_preformatted and (s == _NOFORMAT or re.match(r"\{code:\w", s)):
            in_preformatted = True
            if block:
                result += [_NOFORMAT, *block, _NOFORMAT]
                block = []
            result.append(line)
            prev_blank = False
            continue
        if in_preformatted:
            if s in (_NOFORMAT, "{code}"):
                in_preformatted = False
            result.append(line)
            prev_blank = s == ""
            continue

        is_indented = line.startswith("    ") or (line.startswith("\t") and not line.startswith("\t*"))
        if is_indented and prev_blank:
            stripped = line.lstrip("\t")
            block.append(stripped[4:] if stripped.startswith("    ") else stripped)
            prev_blank = False
        elif block and is_indented:
            stripped = line.lstrip("\t")
            block.append(stripped[4:] if stripped.startswith("    ") else stripped)
            prev_blank = False
        else:
            if block:
                result += [_NOFORMAT, *block, _NOFORMAT]
                block = []
            result.append(line)
            prev_blank = s == ""

    if block:
        result += [_NOFORMAT, *block, _NOFORMAT]

    return "\n".join(result)


def _convert_headings(text: str) -> str:
    for level in range(6, 0, -1):
        hashes = "#" * level
        text = re.sub(
            rf"^{hashes}\s+(.*?)(?:\s+#{{{level}}})?\s*$",
            rf"h{level}. \1",
            text,
            flags=re.MULTILINE,
        )
    return text


def _convert_horizontal_rules(text: str) -> str:
    # --- or *** or ___ on their own line → ----
    return re.sub(r"^[ \t]*[-*_]{3,}[ \t]*$", "----", text, flags=re.MULTILINE)


def _is_table_separator(line: str) -> bool:
    """True if the line is a Markdown table separator (only -, |, :, spaces; must contain both - and |)."""
    return bool(re.match(r"^[|\-: ]+$", line)) and "-" in line and "|" in line


def _convert_tables(text: str) -> str:
    """Convert Markdown tables to Jira wiki table syntax.
    Handles both pipe-bordered (|col|col|) and borderless (col|col) formats.
    """
    lines = text.split("\n")
    result: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        next_line = lines[i + 1] if i + 1 < len(lines) else ""
        is_header = (re.match(r"^\|.+\|", line) and re.match(r"^\|[-| :]+\|", next_line)) or (
            "|" in line and _is_table_separator(next_line)
        )
        if is_header:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            result.append("||" + "||".join(cells) + "||")
            i += 2  # skip separator
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                result.append("|" + "|".join(cells) + "|")
                i += 1
            continue
        result.append(line)
        i += 1
    return "\n".join(result)


def _convert_blockquotes(text: str) -> str:
    """Convert > blockquotes to Jira {quote} blocks."""
    lines = text.split("\n")
    result: list[str] = []
    block: list[str] = []

    for line in lines:
        if line.startswith("> ") or line == ">":
            block.append(line[2:] if line.startswith("> ") else "")
        else:
            if block:
                result.append("{quote}")
                result.extend(block)
                result.append("{quote}")
                block = []
            result.append(line)

    if block:
        result.append("{quote}")
        result.extend(block)
        result.append("{quote}")

    return "\n".join(result)


def _convert_lists(text: str) -> str:
    """Convert Markdown unordered and ordered lists to Jira wiki list syntax."""
    lines = text.split("\n")
    result: list[str] = []

    for line in lines:
        # Unordered: leading spaces/tabs then * or -
        m = re.match(r"^((?:    |\t)*)([*\-+])\s+(.*)", line)
        if m:
            depth = len(m.group(1).replace("    ", "\t").replace("\t", "\t")) + 1
            result.append("*" * depth + " " + m.group(3))
            continue
        # Ordered: leading spaces/tabs then digits.
        m = re.match(r"^((?:    |\t)*)(\d+)\.\s+(.*)", line)
        if m:
            depth = len(m.group(1).replace("    ", "\t").replace("\t", "\t")) + 1
            result.append("#" * depth + " " + m.group(3))
            continue
        result.append(line)

    return "\n".join(result)


def _convert_strikethrough(text: str) -> str:
    # ~~text~~ → -text-
    return re.sub(r"~~(.*?)~~", r"-\1-", text)


def _convert_bold_placeholder(text: str) -> str:
    # **text** or __text__ → placeholder
    text = re.sub(r"\*\*(.*?)\*\*", lambda m: f"{_BOLD_PLACEHOLDER}{m.group(1)}{_BOLD_PLACEHOLDER}", text)
    text = re.sub(r"__(.*?)__", lambda m: f"{_BOLD_PLACEHOLDER}{m.group(1)}{_BOLD_PLACEHOLDER}", text)
    return text


def _restore_bold(text: str) -> str:
    p = re.escape(_BOLD_PLACEHOLDER)
    return re.sub(rf"{p}(.*?){p}", r"*\1*", text)


def _convert_italic(text: str) -> str:
    # *text* or _text_ → _text_  (must not match already-converted bold *text*)
    text = re.sub(r"(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)", r"_\1_", text)
    text = re.sub(r"(?<!_)_(?!_)(.*?)(?<!_)_(?!_)", r"_\1_", text)
    return text


def _convert_inline_code(text: str) -> str:
    # ``text`` or `text` → {{text}}
    text = re.sub(r"``(.+?)``", r"{{\1}}", text)
    text = re.sub(r"`([^`]+)`", r"{{\1}}", text)
    return text


def _convert_images(text: str) -> str:
    # ![alt](url) → !url!
    return re.sub(r"!\[.*?\]\((.*?)\)", r"!\1!", text)


def _convert_links(text: str) -> str:
    # [text](url) → [text|url]
    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"[\1|\2]", text)


def _convert_raw_urls(text: str) -> str:
    # Bare URLs not already inside [...] or !...! → [url]
    return re.sub(r"(?<![!(\[])(https?://[^\s\]!)]+)", r"[\1]", text)
