"""Turn HTML or Markdown into plain text plus Telegram message entities.

Offsets are counted in UTF-16 code units, as Telegram requires, so emoji and
other astral characters do not shift formatting.

HTML tags: ``b strong i em u ins s strike del code pre a blockquote
tg-spoiler span.tg-spoiler tg-emoji``.

Markdown: ``**bold** __italic__ --underline-- ~~strike~~ ||spoiler||
`code` ```lang\\npre``` [text](url)`` and ``> quote`` lines.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import List, Optional, Tuple

from wirelet import schema
from wirelet.utils import utf16_len

Entities = List["schema.kinds.MessageEntity"]


class _HTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text = ""
        self.entities: Entities = []
        self._open: List[Tuple[str, int, dict]] = []
        self.mentions: List[Tuple[int, int, int]] = []  # offset, length, user id

    @property
    def pos(self) -> int:
        return utf16_len(self.text)

    def handle_starttag(self, tag: str, attrs) -> None:
        self._open.append((tag, self.pos, dict(attrs)))

    def handle_data(self, data: str) -> None:
        self.text += data

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self._open) - 1, -1, -1):
            if self._open[i][0] == tag:
                name, start, attrs = self._open.pop(i)
                self._close(name, start, attrs)
                return

    def _close(self, tag: str, start: int, attrs: dict) -> None:
        length = self.pos - start
        if length <= 0:
            return
        t = schema.objects
        simple = {
            "b": t.MessageEntityBold, "strong": t.MessageEntityBold,
            "i": t.MessageEntityItalic, "em": t.MessageEntityItalic,
            "u": t.MessageEntityUnderline, "ins": t.MessageEntityUnderline,
            "s": t.MessageEntityStrike, "strike": t.MessageEntityStrike, "del": t.MessageEntityStrike,
            "code": t.MessageEntityCode, "tg-spoiler": t.MessageEntitySpoiler,
        }

        if tag in simple:
            # <pre><code class="language-x"> is one entity, not two.
            if tag == "code" and self._open and self._open[-1][0] == "pre":
                lang = (attrs.get("class") or "").removeprefix("language-")
                if lang:
                    self._open[-1][2].setdefault("language", lang)
                return
            self.entities.append(simple[tag](offset=start, length=length))
        elif tag == "span" and attrs.get("class") == "tg-spoiler":
            self.entities.append(t.MessageEntitySpoiler(offset=start, length=length))
        elif tag == "pre":
            self.entities.append(t.MessageEntityPre(
                offset=start, length=length, language=attrs.get("language") or ""
            ))
        elif tag == "blockquote":
            self.entities.append(t.MessageEntityBlockquote(
                offset=start, length=length, collapsed="expandable" in attrs or None
            ))
        elif tag == "tg-emoji" and attrs.get("emoji-id"):
            self.entities.append(t.MessageEntityCustomEmoji(
                offset=start, length=length, document_id=int(attrs["emoji-id"])
            ))
        elif tag == "a":
            url = attrs.get("href") or ""
            match = re.match(r"^tg://user\?id=(\d+)$", url)
            if match:
                self.mentions.append((start, length, int(match.group(1))))
            elif url:
                self.entities.append(t.MessageEntityTextUrl(offset=start, length=length, url=url))


def parse_html(text: str):
    parser = _HTML()
    parser.feed(text)
    parser.close()
    return parser.text, parser.entities, parser.mentions


_MD_TOKENS = re.compile(
    r"```(?P<lang>[^\n`]*)\n(?P<pre>.*?)```"
    r"|`(?P<code>[^`\n]+)`"
    r"|\[(?P<ltext>[^\]]+)\]\((?P<url>[^)\s]+)\)"
    r"|(?P<delim>\*\*|__|--|~~|\|\|)",
    re.S,
)
_MD_TAGS = {"**": "b", "__": "i", "--": "u", "~~": "s", "||": "tg-spoiler"}


def markdown_to_html(text: str) -> str:
    out: List[str] = []
    open_tags: List[str] = []
    last = 0

    for m in _MD_TOKENS.finditer(text):
        out.append(html.escape(text[last:m.start()], quote=False))
        last = m.end()

        if m.group("pre") is not None:
            lang = m.group("lang").strip()
            attr = f' language="{html.escape(lang)}"' if lang else ""
            out.append(f"<pre{attr}>{html.escape(m.group('pre').rstrip(chr(10)), quote=False)}</pre>")
        elif m.group("code") is not None:
            out.append(f"<code>{html.escape(m.group('code'), quote=False)}</code>")
        elif m.group("url") is not None:
            out.append(f'<a href="{html.escape(m.group("url"))}">{html.escape(m.group("ltext"), quote=False)}</a>')
        else:
            tag = _MD_TAGS[m.group("delim")]
            if tag in open_tags:
                # close everything opened after it, then reopen those
                idx = open_tags.index(tag)
                reopen = open_tags[idx + 1:]
                for t in reversed(open_tags[idx:]):
                    out.append(f"</{t}>")
                del open_tags[idx:]
                for t in reopen:
                    out.append(f"<{t}>")
                    open_tags.append(t)
            else:
                out.append(f"<{tag}>")
                open_tags.append(tag)

    out.append(html.escape(text[last:], quote=False))
    for t in reversed(open_tags):
        out.append(f"</{t}>")

    # "> quote" lines become a blockquote
    joined = "".join(out)
    return re.sub(
        r"(?m)(^&gt; ?.*(?:\n&gt; ?.*)*)",
        lambda q: "<blockquote>" + re.sub(r"(?m)^&gt; ?", "", q.group(1)) + "</blockquote>",
        joined,
    )


def parse(text: str, mode: Optional[str]):
    """Returns ``(text, entities, mentions)`` for ``mode`` in html/markdown/None."""
    if not mode or mode in ("disabled", "none", "plain"):
        return text, [], []
    mode = mode.lower()
    if mode in ("md", "markdown"):
        text = markdown_to_html(text)
    elif mode != "html":
        raise ValueError(f"Unknown parse mode {mode!r}; use 'html' or 'markdown'")

    plain, entities, mentions = parse_html(text)

    # Telegram rejects entities that start or end on whitespace; trim both.
    stripped = plain.strip()
    lead = utf16_len(plain) - utf16_len(plain.lstrip())
    total = utf16_len(stripped)
    fixed = []
    for e in entities:
        start = max(e.offset - lead, 0)
        end = min(e.offset - lead + e.length, total)
        if end > start:
            e.offset, e.length = start, end - start
            fixed.append(e)
    mentions = [(max(o - lead, 0), length, uid) for o, length, uid in mentions]
    return stripped, sorted(fixed, key=lambda e: e.offset), mentions


def unparse_html(text: str, entities: Optional[Entities]) -> str:
    """Rebuild HTML from text and entities (inverse of :func:`parse_html`)."""
    if not entities:
        return html.escape(text, quote=False)

    t = schema.objects
    utf16 = text.encode("utf-16-le")
    inserts: List[tuple] = []  # (utf16 position, close/open, tiebreaks..., tag)

    for i, e in enumerate(entities):
        pairs = {
            t.MessageEntityBold: ("<b>", "</b>"), t.MessageEntityItalic: ("<i>", "</i>"),
            t.MessageEntityUnderline: ("<u>", "</u>"), t.MessageEntityStrike: ("<s>", "</s>"),
            t.MessageEntityCode: ("<code>", "</code>"), t.MessageEntitySpoiler: ("<tg-spoiler>", "</tg-spoiler>"),
            t.MessageEntityBlockquote: ("<blockquote>", "</blockquote>"),
        }
        if type(e) in pairs:
            open_tag, close_tag = pairs[type(e)]
        elif isinstance(e, t.MessageEntityPre):
            lang = f' language="{html.escape(e.language)}"' if e.language else ""
            open_tag, close_tag = f"<pre{lang}>", "</pre>"
        elif isinstance(e, t.MessageEntityTextUrl):
            open_tag, close_tag = f'<a href="{html.escape(e.url)}">', "</a>"
        elif isinstance(e, t.MessageEntityMentionName):
            open_tag, close_tag = f'<a href="tg://user?id={e.user_id}">', "</a>"
        elif isinstance(e, t.MessageEntityCustomEmoji):
            open_tag, close_tag = f'<tg-emoji emoji-id="{e.document_id}">', "</tg-emoji>"
        else:
            continue
        end = e.offset + e.length
        # At one position closes come before opens. Longer entities open first
        # and therefore close last, so tags always nest properly.
        inserts.append((e.offset, 1, -end, i, open_tag))
        inserts.append((end, 0, -e.offset, -i, close_tag))

    inserts.sort()
    out, last = [], 0
    for pos, *_, tag in inserts:
        out.append(html.escape(utf16[last * 2:pos * 2].decode("utf-16-le"), quote=False))
        out.append(tag)
        last = pos
    out.append(html.escape(utf16[last * 2:].decode("utf-16-le"), quote=False))
    return "".join(out)
