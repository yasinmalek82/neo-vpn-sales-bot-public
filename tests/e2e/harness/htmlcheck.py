"""Telegram's HTML parse mode, reduced to what rejects a message.

Telegram refuses a whole message when its HTML does not parse: a bare ``<``, a
tag it does not support, a tag left open, an end tag that closes the wrong
thing. A forgiving parser would let all of those pass, so this one fails on the
same inputs and returns the plain text a reader would see.

``&`` and ``>`` need no escaping; ``&lt;`` ``&gt;`` ``&amp;`` ``&quot;`` and
numeric references are decoded; any other ``&name;`` stays literal. That is the
Bot API's behaviour, not Python's ``html.unescape``.
"""

from __future__ import annotations

import re

# Tag name as written -> the entity it produces. Everything else is refused.
_TAGS: dict[str, str] = {
    "b": "b",
    "strong": "b",
    "i": "i",
    "em": "i",
    "u": "u",
    "ins": "u",
    "s": "s",
    "strike": "s",
    "del": "s",
    "a": "a",
    "code": "code",
    "pre": "pre",
    "tg-spoiler": "spoiler",
    "span": "span",
    "tg-emoji": "emoji",
    "blockquote": "blockquote",
}

_ATTR = re.compile(r"""([A-Za-z_][\w-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")
_ENTITY = re.compile(r"&(lt|gt|amp|quot);|&#(\d+);|&#[xX]([0-9a-fA-F]+);")
_NAMED = {"lt": "<", "gt": ">", "amp": "&", "quot": '"'}


class HtmlParseError(ValueError):
    """The message text Telegram would answer with «can't parse entities»."""


def _unescape(chunk: str) -> str:
    def repl(match: re.Match[str]) -> str:
        if match.group(1):
            return _NAMED[match.group(1)]
        code = int(match.group(2)) if match.group(2) else int(match.group(3), 16)
        try:
            return chr(code)
        except (ValueError, OverflowError):
            return match.group(0)

    return _ENTITY.sub(repl, chunk)


def _offset(text: str, index: int) -> int:
    return len(text[:index].encode("utf-8"))


def _check_attrs(name: str, attrs: dict[str, str], text: str, at: int) -> None:
    if name == "a" and not attrs.get("href", "").strip():
        raise HtmlParseError(
            f'Tag "a" must have an "href" attribute at byte offset {_offset(text, at)}'
        )
    if name == "span" and attrs.get("class") != "tg-spoiler":
        raise HtmlParseError(
            f'Tag "span" must have class "tg-spoiler" at byte offset {_offset(text, at)}'
        )
    if name == "tg-emoji" and not attrs.get("emoji-id", "").isdigit():
        raise HtmlParseError(
            f'Tag "tg-emoji" must have a numeric "emoji-id" at byte offset {_offset(text, at)}'
        )


def parse_html(text: str) -> str:
    """Return the visible text, or raise ``HtmlParseError`` like Telegram does."""
    out: list[str] = []
    stack: list[tuple[str, str]] = []  # (entity, tag as written)
    i = 0
    n = len(text)
    while i < n:
        lt = text.find("<", i)
        if lt == -1:
            out.append(_unescape(text[i:]))
            break
        out.append(_unescape(text[i:lt]))
        gt = text.find(">", lt + 1)
        if gt == -1:
            raise HtmlParseError(f"Unclosed start tag at byte offset {_offset(text, lt)}")
        body = text[lt + 1 : gt]
        if body.startswith("/"):
            name = body[1:].strip().lower()
            entity = _TAGS.get(name)
            if entity is None or not stack:
                raise HtmlParseError(f"Unmatched end tag at byte offset {_offset(text, lt)}")
            if stack[-1][0] != entity:
                raise HtmlParseError(
                    f"Unmatched end tag at byte offset {_offset(text, lt)}, "
                    f'expected "</{stack[-1][1]}>", found "</{name}>"'
                )
            stack.pop()
        else:
            parts = body.split(None, 1)
            name = parts[0].lower() if parts else ""
            if name not in _TAGS:
                raise HtmlParseError(
                    f'Unsupported start tag "{name}" at byte offset {_offset(text, lt)}'
                )
            attrs = {
                m.group(1).lower(): next(g for g in m.groups()[1:] if g is not None)
                for m in _ATTR.finditer(parts[1] if len(parts) > 1 else "")
            }
            _check_attrs(name, attrs, text, lt)
            stack.append((_TAGS[name], name))
        i = gt + 1
    if stack:
        raise HtmlParseError(f'Can\'t find end tag corresponding to start tag "{stack[-1][1]}"')
    return "".join(out)


def plain(text: str) -> str:
    """The text a reader sees, for comparing against copy that carries tags.

    Unlike ``parse_html`` this never raises: it is for the test's own
    expectations (``plain(texts.welcome_new.format(...))``), not for judging the
    bot's output.
    """
    try:
        return parse_html(text)
    except HtmlParseError:
        return text


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def digits(text: str) -> str:
    """Western digits with no thousands separators: «۱۵۰٬۰۰۰» and «150,000» both become «150000».

    The bot prints one amount in three styles (Persian digits, Arabic separator,
    comma). A scenario that means «this number is on screen» should not care which.
    """
    return text.translate(_DIGITS).replace("٬", "").replace(",", "").replace("٬", "")
