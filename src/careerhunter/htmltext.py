"""Turn a board's HTML job description into plain text, with the standard library only.

Greenhouse sends `content` as HTML whose markup is itself entity-escaped
(`&lt;p&gt;Salary ...&lt;/p&gt;`), so the text is unescaped once and then parsed.
Script and style contents are dropped. Block elements and `<br>` become line breaks,
so a sentence never runs into the next paragraph, which the salary check relies on.
Runs of spaces collapse and blank lines go. The text is used in memory for the filter
decision and the scoring prompt; nothing here stores it.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_BLOCKS = frozenset(
    {
        "address",
        "article",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)
_SKIPPED = frozenset({"script", "style", "template", "noscript"})
_SPACES = re.compile(r"[ \t\f\v\u00a0]+")  # \u00a0 is a no-break space


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED:
            self.skipping += 1
        elif tag in _BLOCKS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED:
            self.skipping = max(0, self.skipping - 1)
        elif tag in _BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.parts.append(data)


def to_text(markup: str, *, escaped: bool = False) -> str:
    """Plain text of `markup`. `escaped` unescapes it once first (Greenhouse `content`)."""
    source = html.unescape(markup) if escaped else markup
    parser = _Text()
    parser.feed(source)
    parser.close()
    lines = (_SPACES.sub(" ", line).strip() for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)
