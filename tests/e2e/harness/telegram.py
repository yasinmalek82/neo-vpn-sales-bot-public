"""A Telegram that refuses what Telegram refuses.

``FakeTelegram`` is an aiogram session: every call the bot makes lands here
instead of on the network. It records the call and answers with a realistic
result, or raises the exception class aiogram raises for the same Bot API error.

What it rejects (and the ``kind`` each rejection is filed under):

``empty_text``        text or caption empty after parsing
``too_long``          text over 4096, caption over 1024 (UTF-16 units, after parsing)
``html``              a bare ``<``, an unsupported or unclosed tag, a mismatched end tag
``button_data``       ``callback_data`` outside 1-64 bytes
``button``            empty button text, a button with no action, a bad URL, too many buttons
``not_modified``      an edit that changes neither text nor keyboard
``message_gone``      editing or deleting a message that does not exist (or is not the bot's)
``wrong_media``       editing text on a photo, a caption on a text message
``blocked``           sending to a user who blocked the bot (``TelegramForbiddenError``)
``chat_not_found``    sending to a chat that never talked to the bot
``no_thread``         a forum thread that does not exist
``callback``          answering a callback twice, a stale id, or with text over 200
``photo``             bytes that are not an image, a file id the bot never had
``rights``            creating a topic without being an admin who may

The first group are bugs in what the bot *built*; they fail the test at the end
unless it opted in with ``accept()``. The others are conditions the bot is
supposed to survive, so they are recorded and left to the scenario.
"""

# ruff: noqa: ARG002  -- handlers share one signature; early returns read better here

from __future__ import annotations

import io
import secrets
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from aiogram.methods import TelegramMethod
from aiogram.types import (
    ChatFullInfo,
    ChatMemberAdministrator,
    ChatMemberOwner,
    ForumTopic,
    InlineKeyboardMarkup,
    InputFile,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    User,
)
from aiogram.types.accepted_gift_types import AcceptedGiftTypes
from PIL import Image

from tests.e2e.harness.htmlcheck import HtmlParseError, parse_html, utf16_len

MAX_TEXT = 4096
MAX_CAPTION = 1024
MAX_CALLBACK_ANSWER = 200
MAX_CALLBACK_DATA_BYTES = 64
GENERAL_THREAD = 1

# Bugs in what the bot built. A scenario that triggers one fails unless it says so.
STRICT_KINDS = frozenset(
    {
        "empty_text",
        "too_long",
        "html",
        "button_data",
        "button",
        "message_gone",
        "wrong_media",
        "no_thread",
        "callback",
        "photo",
        "rights",
    }
)


@dataclass(frozen=True)
class Rejection:
    kind: str
    method: str
    message: str
    chat_id: int | None

    def __str__(self) -> str:
        where = f" (chat {self.chat_id})" if self.chat_id is not None else ""
        return f"{self.method}{where}: [{self.kind}] {self.message}"


@dataclass
class SentMessage:
    """One message in a chat, as the person on the other end would see it."""

    seq: int
    chat_id: int
    message_id: int
    thread_id: int | None
    kind: str  # text | photo | document
    html: str  # as sent: tags included
    text: str  # as read: tags gone
    parse_mode: str | None
    markup: InlineKeyboardMarkup | None = None
    file_id: str | None = None
    file_bytes: bytes | None = None
    filename: str | None = None
    from_bot: bool = True
    edits: int = 0
    touched: int = 0
    deleted: bool = False

    @property
    def rows(self) -> list[list[str]]:
        """Inline button labels, row by row."""
        if self.markup is None:
            return []
        return [[b.text for b in row] for row in self.markup.inline_keyboard]

    @property
    def buttons(self) -> list[str]:
        return [label for row in self.rows for label in row]

    def __repr__(self) -> str:
        shown = self.text if len(self.text) <= 60 else self.text[:57] + "..."
        return f"<{self.kind} #{self.message_id} {shown!r} buttons={self.buttons}>"


@dataclass
class ChatState:
    id: int
    type: str  # private | supergroup
    title: str | None = None
    first_name: str | None = None
    username: str | None = None
    is_forum: bool = False
    bot_present: bool = True
    bot_is_admin: bool = False
    can_manage_topics: bool = False
    blocked: bool = False
    next_id: int = 1
    messages: dict[int, SentMessage] = field(default_factory=dict)
    reply_keyboard: list[list[str]] | None = None
    topics: dict[int, str] = field(default_factory=dict)
    next_thread: int = 2

    def take_id(self) -> int:
        value = self.next_id
        self.next_id += 1
        return value


@dataclass
class Call:
    method: str
    params: dict[str, Any]
    ok: bool
    error: str | None = None


@dataclass
class CallbackState:
    id: str
    chat_id: int
    message_id: int
    answered: bool = False
    text: str | None = None
    show_alert: bool = False


def _bad(method: TelegramMethod[Any], message: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=method, message=f"Bad Request: {message}")


class FakeTelegram(BaseSession):
    def __init__(self, *, bot_username: str = "neo_test_bot") -> None:
        super().__init__()
        self.me = User(
            id=777_000_111,
            is_bot=True,
            first_name="Neo Test",
            username=bot_username,
            can_join_groups=True,
        )
        self.chats: dict[int, ChatState] = {}
        self.history: list[SentMessage] = []
        self.calls: list[Call] = []
        self.rejections: list[Rejection] = []
        self.callbacks: dict[str, CallbackState] = {}
        self.files: dict[str, bytes] = {}
        self.commands_set: list[Any] = []
        self._accepted: set[str] = set()
        self._seq = 0
        self._floods: dict[str, list[int]] = {}
        # Called before every Bot API call (see ``App.commit_at_every_network_call``).
        self.on_network: Callable[[], None] | None = None

    # ------------------------------------------------------------------ setup

    def add_private_chat(
        self, user_id: int, *, first_name: str | None = None, username: str | None = None
    ) -> ChatState:
        chat = self.chats.get(user_id)
        if chat is None:
            chat = ChatState(id=user_id, type="private", first_name=first_name, username=username)
            self.chats[user_id] = chat
        return chat

    def add_forum(
        self,
        chat_id: int = -1001234567890,
        *,
        title: str = "Neo reports",
        is_forum: bool = True,
        bot_is_admin: bool = True,
        can_manage_topics: bool = True,
    ) -> ChatState:
        chat = ChatState(
            id=chat_id,
            type="supergroup",
            title=title,
            is_forum=is_forum,
            bot_is_admin=bot_is_admin,
            can_manage_topics=can_manage_topics,
        )
        self.chats[chat_id] = chat
        return chat

    def block(self, user_id: int) -> None:
        """The person blocked the bot: every send to them now fails."""
        self.chats[user_id].blocked = True

    def unblock(self, user_id: int) -> None:
        self.chats[user_id].blocked = False

    def accept(self, *kinds: str) -> None:
        """Declare that this scenario expects rejections of these kinds."""
        unknown = set(kinds) - STRICT_KINDS
        if unknown:
            raise ValueError(f"not strict kinds (nothing to accept): {sorted(unknown)}")
        self._accepted.update(kinds)

    def flood_next(self, method: str, *, retry_after: int = 3, times: int = 1) -> None:
        """Telegram throttles the next ``times`` calls of ``method`` («EditForumTopic»).

        Live, attaching the report group met «Too Many Requests: retry after N»
        on the topic calls; the caller must wait and retry, never rebuild.
        """
        self._floods.setdefault(method, []).extend([retry_after] * times)

    # -------------------------------------------------------------- inspection

    @property
    def last_seq(self) -> int:
        """Ordering counter: every send and edit advances it. Bookmarks use it."""
        return self._seq

    def unaccepted(self) -> list[Rejection]:
        return [
            r for r in self.rejections if r.kind in STRICT_KINDS and r.kind not in self._accepted
        ]

    def api_calls(self, name: str | None = None) -> list[Call]:
        return [c for c in self.calls if name is None or c.method == name]

    def thread(self, chat_id: int, thread_id: int | None) -> list[SentMessage]:
        """Messages the bot posted in one forum topic (``None`` = General), oldest first."""
        want = thread_id if thread_id is not None else GENERAL_THREAD
        return [
            m
            for m in self.history
            if m.chat_id == chat_id and not m.deleted and (m.thread_id or GENERAL_THREAD) == want
        ]

    def topic_threads(self, chat_id: int) -> dict[str, int]:
        return {name: tid for tid, name in self.chats[chat_id].topics.items()}

    # ------------------------------------------------------------- the session

    async def close(self) -> None:
        return None

    async def stream_content(  # type: ignore[override]
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        raise NotImplementedError("FakeTelegram does not serve file downloads")
        yield b""

    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        if self.on_network is not None:
            self.on_network()
        name = type(method).__name__
        handler = getattr(self, f"_m_{name}", None)
        if handler is None:
            raise NotImplementedError(
                f"FakeTelegram does not implement {name}; add _m_{name} in "
                "tests/e2e/harness/telegram.py (and make it as strict as the real one)"
            )
        params = {k: getattr(method, k) for k in type(method).model_fields}
        pending = self._floods.get(name)
        if pending:
            seconds = pending.pop(0)
            message = f"Too Many Requests: retry after {seconds}"
            self.calls.append(Call(name, params, False, message))
            raise TelegramRetryAfter(method=method, message=message, retry_after=seconds)
        try:
            result = await handler(bot, method)
        except TelegramAPIError as exc:
            self.calls.append(Call(name, params, False, str(exc)))
            raise
        self.calls.append(Call(name, params, True))
        return result

    # ----------------------------------------------------------------- helpers

    def _reject(
        self, kind: str, method: TelegramMethod[Any], message: str, chat_id: int | None = None
    ) -> TelegramBadRequest:
        self.rejections.append(Rejection(kind, type(method).__name__, message, chat_id))
        return _bad(method, message)

    def _reject_as(
        self,
        exc: TelegramAPIError,
        kind: str,
        method: TelegramMethod[Any],
        message: str,
        chat_id: int | None,
    ) -> TelegramAPIError:
        self.rejections.append(Rejection(kind, type(method).__name__, message, chat_id))
        return exc

    @staticmethod
    def _resolve(bot: Bot, value: Any) -> Any:
        from aiogram.client.default import Default

        if isinstance(value, Default):
            return bot.default[value.name]
        return value

    def _chat(self, method: TelegramMethod[Any], chat_id: Any) -> ChatState:
        if not isinstance(chat_id, int) or chat_id not in self.chats:
            raise self._reject(
                "chat_not_found",
                method,
                "chat not found",
                chat_id if isinstance(chat_id, int) else None,
            )
        chat = self.chats[chat_id]
        if chat.type == "private" and chat.blocked:
            raise self._reject_as(
                TelegramForbiddenError(
                    method=method, message="Forbidden: bot was blocked by the user"
                ),
                "blocked",
                method,
                "bot was blocked by the user",
                chat_id,
            )
        if chat.type != "private" and not chat.bot_present:
            raise self._reject_as(
                TelegramForbiddenError(
                    method=method, message="Forbidden: bot was kicked from the supergroup chat"
                ),
                "blocked",
                method,
                "bot was kicked from the chat",
                chat_id,
            )
        return chat

    def _check_thread(
        self, method: TelegramMethod[Any], chat: ChatState, thread_id: int | None
    ) -> None:
        if thread_id is None:
            return
        if not chat.is_forum or (thread_id != GENERAL_THREAD and thread_id not in chat.topics):
            raise self._reject("no_thread", method, "message thread not found", chat.id)

    def _text(
        self,
        method: TelegramMethod[Any],
        chat_id: int,
        raw: str | None,
        parse_mode: str | None,
        *,
        limit: int,
        what: str,
    ) -> str:
        """Validate and render a text or caption."""
        raw = raw or ""
        if (
            parse_mode is not None
            and str(getattr(parse_mode, "value", parse_mode)).upper() == "HTML"
        ):
            try:
                shown = parse_html(raw)
            except HtmlParseError as exc:
                raise self._reject("html", method, f"can't parse entities: {exc}", chat_id) from exc
        else:
            shown = raw
        if not shown.strip():
            raise self._reject("empty_text", method, f"{what} is empty", chat_id)
        if utf16_len(shown) > limit:
            raise self._reject("too_long", method, f"{what} is too long", chat_id)
        return shown

    def _inline(
        self, method: TelegramMethod[Any], chat_id: int, markup: InlineKeyboardMarkup
    ) -> None:
        total = 0
        for row in markup.inline_keyboard:
            if len(row) > 8:
                raise self._reject("button", method, "too many buttons in one row (max 8)", chat_id)
            for button in row:
                total += 1
                if not (button.text or "").strip():
                    raise self._reject(
                        "button", method, "inline keyboard button text is empty", chat_id
                    )
                actions = [
                    name
                    for name in (
                        "url",
                        "callback_data",
                        "web_app",
                        "login_url",
                        "switch_inline_query",
                        "switch_inline_query_current_chat",
                        "switch_inline_query_chosen_chat",
                        "copy_text",
                        "callback_game",
                        "pay",
                    )
                    if getattr(button, name, None) not in (None, False)
                ]
                if len(actions) != 1 and not (len(actions) == 2 and "pay" in actions):
                    raise self._reject(
                        "button",
                        method,
                        "can't parse inline keyboard button: "
                        f"{button.text!r} needs exactly one action, has {actions or 'none'}",
                        chat_id,
                    )
                if button.callback_data is not None:
                    size = len(button.callback_data.encode("utf-8"))
                    if not 1 <= size <= MAX_CALLBACK_DATA_BYTES:
                        raise self._reject(
                            "button_data",
                            method,
                            f"BUTTON_DATA_INVALID ({size} bytes: {button.callback_data[:30]!r}...)",
                            chat_id,
                        )
                if button.url is not None and not button.url.startswith(
                    ("http://", "https://", "tg://")
                ):
                    raise self._reject(
                        "button", method, f"BUTTON_URL_INVALID {button.url!r}", chat_id
                    )
                style = getattr(button, "style", None)
                if style is not None and str(getattr(style, "value", style)) not in {
                    "primary",
                    "success",
                    "danger",
                }:
                    raise self._reject("button", method, f"unknown button style {style!r}", chat_id)
        if total > 100:
            raise self._reject(
                "button", method, "REPLY_MARKUP_TOO_LONG (over 100 buttons)", chat_id
            )

    def _reply_kb(
        self, method: TelegramMethod[Any], chat: ChatState, markup: Any
    ) -> list[list[str]] | None | bool:
        """Validate a reply keyboard. Returns the labels, ``None`` for remove, ``False`` for no change."""
        if isinstance(markup, ReplyKeyboardRemove):
            return None
        if isinstance(markup, ReplyKeyboardMarkup):
            rows: list[list[str]] = []
            for row in markup.keyboard:
                if len(row) > 12:
                    raise self._reject("button", method, "too many reply buttons in a row", chat.id)
                labels = []
                for button in row:
                    label = button if isinstance(button, str) else button.text
                    if not (label or "").strip():
                        raise self._reject(
                            "button", method, "reply keyboard button text is empty", chat.id
                        )
                    labels.append(label)
                rows.append(labels)
            if not rows:
                raise self._reject("button", method, "reply keyboard has no buttons", chat.id)
            return rows
        return False

    async def _media(
        self, bot: Bot, method: TelegramMethod[Any], chat_id: int, source: Any
    ) -> tuple[str, bytes | None, str | None]:
        """Resolve a photo or document to (file_id, bytes, filename), as Telegram would."""
        if isinstance(source, InputFile):
            chunks = [chunk async for chunk in source.read(bot)]
            data = b"".join(chunks)
            file_id = self.register_file(data)
            return file_id, data, getattr(source, "filename", None)
        if isinstance(source, str):
            if source in self.files:
                return source, self.files[source], None
            if source.startswith(("http://", "https://")):
                return source, None, None
            raise self._reject(
                "photo",
                method,
                "wrong file identifier/HTTP URL specified (the bot never held this file)",
                chat_id,
            )
        raise self._reject("photo", method, f"unsupported media {type(source).__name__}", chat_id)

    def register_file(self, data: bytes) -> str:
        file_id = "AgAC" + secrets.token_urlsafe(24)
        self.files[file_id] = data
        return file_id

    def _check_image(self, method: TelegramMethod[Any], chat_id: int, data: bytes | None) -> None:
        if data is None:
            return
        try:
            with Image.open(io.BytesIO(data)) as img:
                width, height = img.size
        except Exception as exc:  # noqa: BLE001
            raise self._reject(
                "photo", method, "IMAGE_PROCESS_FAILED: the bytes are not an image", chat_id
            ) from exc
        if width + height > 10_000 or max(width, height) > 20 * max(1, min(width, height)):
            raise self._reject("photo", method, "PHOTO_INVALID_DIMENSIONS", chat_id)

    def _record(
        self,
        chat: ChatState,
        *,
        kind: str,
        html: str,
        text: str,
        parse_mode: str | None,
        markup: InlineKeyboardMarkup | None,
        thread_id: int | None,
        file_id: str | None = None,
        file_bytes: bytes | None = None,
        filename: str | None = None,
    ) -> SentMessage:
        self._seq += 1
        message = SentMessage(
            seq=self._seq,
            chat_id=chat.id,
            message_id=chat.take_id(),
            thread_id=thread_id,
            kind=kind,
            html=html,
            text=text,
            parse_mode=parse_mode,
            markup=markup,
            file_id=file_id,
            file_bytes=file_bytes,
            filename=filename,
            touched=self._seq,
        )
        chat.messages[message.message_id] = message
        self.history.append(message)
        return message

    def record_incoming(
        self, chat_id: int, *, kind: str, text: str, file_id: str | None = None
    ) -> int:
        """A message the person sent. It takes a message id like any other."""
        chat = self.chats[chat_id]
        self._seq += 1
        message = SentMessage(
            seq=self._seq,
            chat_id=chat_id,
            message_id=chat.take_id(),
            thread_id=None,
            kind=kind,
            html=text,
            text=text,
            parse_mode=None,
            file_id=file_id,
            from_bot=False,
            touched=self._seq,
        )
        chat.messages[message.message_id] = message
        return message.message_id

    def touch(self, message: SentMessage) -> None:
        self._seq += 1
        message.touched = self._seq

    def to_result(self, bot: Bot, message: SentMessage) -> Message:
        return Message.model_validate(self.message_raw(message), context={"bot": bot})

    def message_raw(self, message: SentMessage) -> dict[str, Any]:
        """The message as Telegram's JSON, which is also what a callback carries."""
        chat = self.chats[message.chat_id]
        raw: dict[str, Any] = {
            "message_id": message.message_id,
            "date": int(time.time()),
            "chat": self.chat_raw(chat),
            "from": self.me.model_dump(exclude_none=True),
        }
        if message.thread_id and chat.is_forum:
            raw["message_thread_id"] = message.thread_id
            raw["is_topic_message"] = message.thread_id != GENERAL_THREAD
        key = "text" if message.kind == "text" else "caption"
        raw[key] = message.text
        if message.kind == "photo":
            raw["photo"] = [
                {
                    "file_id": message.file_id,
                    "file_unique_id": (message.file_id or "")[:12],
                    "width": 320,
                    "height": 320,
                }
            ]
        if message.kind == "document":
            raw["document"] = {
                "file_id": message.file_id,
                "file_unique_id": (message.file_id or "")[:12],
                "file_name": message.filename,
            }
        if message.markup is not None:
            raw["reply_markup"] = message.markup.model_dump(exclude_none=True)
        if message.edits:
            raw["edit_date"] = int(time.time())
        return raw

    @staticmethod
    def chat_raw(chat: ChatState) -> dict[str, Any]:
        raw: dict[str, Any] = {"id": chat.id, "type": chat.type}
        if chat.title:
            raw["title"] = chat.title
        if chat.first_name:
            raw["first_name"] = chat.first_name
        if chat.username:
            raw["username"] = chat.username
        if chat.is_forum:
            raw["is_forum"] = True
        return raw

    # ------------------------------------------------------------ send methods

    async def _m_SendMessage(self, bot: Bot, method: Any) -> Message:
        chat = self._chat(method, method.chat_id)
        thread = method.message_thread_id
        self._check_thread(method, chat, thread)
        mode = self._resolve(bot, method.parse_mode)
        shown = self._text(method, chat.id, method.text, mode, limit=MAX_TEXT, what="message text")
        inline = self._markup_of(method, chat, method.reply_markup)
        message = self._record(
            chat,
            kind="text",
            html=method.text,
            text=shown,
            parse_mode=_mode_name(mode),
            markup=inline,
            thread_id=thread,
        )
        self._apply_reply_kb(method, chat, method.reply_markup)
        return self.to_result(bot, message)

    def _markup_of(
        self, method: TelegramMethod[Any], chat: ChatState, markup: Any
    ) -> InlineKeyboardMarkup | None:
        if isinstance(markup, InlineKeyboardMarkup):
            self._inline(method, chat.id, markup)
            return markup
        self._reply_kb(method, chat, markup)  # validated here, applied after the send succeeds
        return None

    def _apply_reply_kb(self, method: TelegramMethod[Any], chat: ChatState, markup: Any) -> None:
        result = self._reply_kb(method, chat, markup)
        if result is False:
            return
        chat.reply_keyboard = result

    async def _m_SendPhoto(self, bot: Bot, method: Any) -> Message:
        chat = self._chat(method, method.chat_id)
        self._check_thread(method, chat, method.message_thread_id)
        mode = self._resolve(bot, method.parse_mode)
        file_id, data, _ = await self._media(bot, method, chat.id, method.photo)
        self._check_image(method, chat.id, data)
        shown = ""
        if method.caption:
            shown = self._text(
                method, chat.id, method.caption, mode, limit=MAX_CAPTION, what="message caption"
            )
        inline = self._markup_of(method, chat, method.reply_markup)
        message = self._record(
            chat,
            kind="photo",
            html=method.caption or "",
            text=shown,
            parse_mode=_mode_name(mode),
            markup=inline,
            thread_id=method.message_thread_id,
            file_id=file_id,
            file_bytes=data,
        )
        self._apply_reply_kb(method, chat, method.reply_markup)
        return self.to_result(bot, message)

    async def _m_SendDocument(self, bot: Bot, method: Any) -> Message:
        chat = self._chat(method, method.chat_id)
        self._check_thread(method, chat, method.message_thread_id)
        mode = self._resolve(bot, method.parse_mode)
        file_id, data, filename = await self._media(bot, method, chat.id, method.document)
        shown = ""
        if method.caption:
            shown = self._text(
                method, chat.id, method.caption, mode, limit=MAX_CAPTION, what="message caption"
            )
        inline = self._markup_of(method, chat, method.reply_markup)
        message = self._record(
            chat,
            kind="document",
            html=method.caption or "",
            text=shown,
            parse_mode=_mode_name(mode),
            markup=inline,
            thread_id=method.message_thread_id,
            file_id=file_id,
            file_bytes=data,
            filename=filename,
        )
        return self.to_result(bot, message)

    async def _m_SendChatAction(self, bot: Bot, method: Any) -> bool:
        self._chat(method, method.chat_id)
        return True

    # ----------------------------------------------------------- edit / delete

    def _target(self, method: Any) -> tuple[ChatState, SentMessage]:
        if method.inline_message_id is not None:
            raise NotImplementedError("FakeTelegram: inline messages are not modelled")
        chat = self._chat(method, method.chat_id)
        message = chat.messages.get(method.message_id)
        if message is None or message.deleted:
            raise self._reject("message_gone", method, "message to edit not found", chat.id)
        if not message.from_bot:
            raise self._reject("message_gone", method, "message can't be edited", chat.id)
        return chat, message

    @staticmethod
    def _dump(markup: InlineKeyboardMarkup | None) -> Any:
        return None if markup is None else markup.model_dump(exclude_none=True)

    def _not_modified(self, method: TelegramMethod[Any], chat_id: int) -> TelegramBadRequest:
        return self._reject(
            "not_modified",
            method,
            "message is not modified: specified new message content and reply markup "
            "are exactly the same as a current content and reply markup of the message",
            chat_id,
        )

    async def _m_EditMessageText(self, bot: Bot, method: Any) -> Message:
        chat, message = self._target(method)
        if message.kind != "text":
            raise self._reject(
                "wrong_media", method, "there is no text in the message to edit", chat.id
            )
        mode = self._resolve(bot, method.parse_mode)
        shown = self._text(method, chat.id, method.text, mode, limit=MAX_TEXT, what="message text")
        markup = method.reply_markup
        if markup is not None:
            self._inline(method, chat.id, markup)
        if (
            method.text == message.html
            and _mode_name(mode) == message.parse_mode
            and self._dump(markup) == self._dump(message.markup)
        ):
            raise self._not_modified(method, chat.id)
        message.html, message.text, message.parse_mode = method.text, shown, _mode_name(mode)
        message.markup = markup
        message.edits += 1
        self.touch(message)
        return self.to_result(bot, message)

    async def _m_EditMessageReplyMarkup(self, bot: Bot, method: Any) -> Message:
        chat, message = self._target(method)
        markup = method.reply_markup
        if markup is not None:
            self._inline(method, chat.id, markup)
        if self._dump(markup) == self._dump(message.markup):
            raise self._not_modified(method, chat.id)
        message.markup = markup
        message.edits += 1
        self.touch(message)
        return self.to_result(bot, message)

    async def _m_EditMessageCaption(self, bot: Bot, method: Any) -> Message:
        chat, message = self._target(method)
        if message.kind == "text":
            raise self._reject(
                "wrong_media", method, "there is no caption in the message to edit", chat.id
            )
        mode = self._resolve(bot, method.parse_mode)
        shown = ""
        if method.caption:
            shown = self._text(
                method, chat.id, method.caption, mode, limit=MAX_CAPTION, what="message caption"
            )
        markup = method.reply_markup
        if markup is not None:
            self._inline(method, chat.id, markup)
        if (
            (method.caption or "") == message.html
            and _mode_name(mode) == message.parse_mode
            and self._dump(markup) == self._dump(message.markup)
        ):
            raise self._not_modified(method, chat.id)
        message.html, message.text, message.parse_mode = (
            method.caption or "",
            shown,
            _mode_name(mode),
        )
        message.markup = markup
        message.edits += 1
        self.touch(message)
        return self.to_result(bot, message)

    async def _m_EditMessageMedia(self, bot: Bot, method: Any) -> Message:
        chat, message = self._target(method)
        if message.kind == "text":
            raise self._reject("wrong_media", method, "message can't be edited into media", chat.id)
        media = method.media
        file_id, data, _ = await self._media(bot, method, chat.id, media.media)
        self._check_image(method, chat.id, data)
        mode = self._resolve(bot, getattr(media, "parse_mode", None))
        caption = getattr(media, "caption", None) or ""
        shown = (
            self._text(method, chat.id, caption, mode, limit=MAX_CAPTION, what="message caption")
            if caption
            else ""
        )
        markup = method.reply_markup
        if markup is not None:
            self._inline(method, chat.id, markup)
        if (
            file_id == message.file_id
            and caption == message.html
            and self._dump(markup) == self._dump(message.markup)
        ):
            raise self._not_modified(method, chat.id)
        message.file_id, message.file_bytes = file_id, data
        message.html, message.text, message.parse_mode = caption, shown, _mode_name(mode)
        message.markup = markup
        message.edits += 1
        self.touch(message)
        return self.to_result(bot, message)

    async def _m_DeleteMessage(self, bot: Bot, method: Any) -> bool:
        chat = self._chat(method, method.chat_id)
        message = chat.messages.get(method.message_id)
        if message is None or message.deleted:
            raise self._reject("message_gone", method, "message to delete not found", chat.id)
        message.deleted = True
        self.touch(message)
        return True

    # --------------------------------------------------------------- callbacks

    def open_callback(self, callback_id: str, chat_id: int, message_id: int) -> CallbackState:
        state = CallbackState(callback_id, chat_id, message_id)
        self.callbacks[callback_id] = state
        return state

    async def _m_AnswerCallbackQuery(self, bot: Bot, method: Any) -> bool:
        state = self.callbacks.get(method.callback_query_id)
        if state is None or state.answered:
            raise self._reject(
                "callback",
                method,
                "query is too old and response timeout expired or query ID is invalid"
                + (" (already answered)" if state else " (unknown id)"),
                state.chat_id if state else None,
            )
        if method.text is not None and utf16_len(method.text) > MAX_CALLBACK_ANSWER:
            raise self._reject(
                "callback",
                method,
                "MESSAGE_TOO_LONG: alert text over 200 characters",
                state.chat_id,
            )
        state.answered = True
        state.text = method.text
        state.show_alert = bool(method.show_alert)
        return True

    # ------------------------------------------------------------- bot / chats

    async def _m_GetMe(self, bot: Bot, method: Any) -> User:
        return self.me

    async def _m_SetMyCommands(self, bot: Bot, method: Any) -> bool:
        self.commands_set.append(method)
        return True

    async def _m_DeleteMyCommands(self, bot: Bot, method: Any) -> bool:
        return True

    async def _m_DeleteWebhook(self, bot: Bot, method: Any) -> bool:
        return True

    async def _m_SetWebhook(self, bot: Bot, method: Any) -> bool:
        return True

    async def _m_GetChat(self, bot: Bot, method: Any) -> ChatFullInfo:
        chat_id = method.chat_id
        chat = self.chats.get(chat_id) if isinstance(chat_id, int) else None
        if chat is None or (chat.type != "private" and not chat.bot_present):
            self.rejections.append(Rejection("chat_not_found", "GetChat", "chat not found", None))
            raise TelegramBadRequest(method=method, message="Bad Request: chat not found")
        return ChatFullInfo.model_validate(
            {
                **self.chat_raw(chat),
                "accent_color_id": 0,
                "max_reaction_count": 11,
                "accepted_gift_types": dict.fromkeys(AcceptedGiftTypes.model_fields, False),
            },
            context={"bot": bot},
        )

    async def _m_GetChatMember(
        self, bot: Bot, method: Any
    ) -> ChatMemberAdministrator | ChatMemberOwner:
        chat = self._chat(method, method.chat_id)
        if method.user_id != self.me.id:
            raise NotImplementedError("FakeTelegram only answers getChatMember for the bot itself")
        if not chat.bot_is_admin:
            from aiogram.types import ChatMemberMember

            return ChatMemberMember(user=self.me)  # type: ignore[return-value]
        rights = dict.fromkeys(
            (
                "can_be_edited",
                "can_manage_chat",
                "can_delete_messages",
                "can_manage_video_chats",
                "can_restrict_members",
                "can_promote_members",
                "can_change_info",
                "can_invite_users",
                "can_post_stories",
                "can_edit_stories",
                "can_delete_stories",
                "can_send_welcome_messages",
            ),
            True,
        )
        return ChatMemberAdministrator(
            user=self.me,
            is_anonymous=False,
            can_manage_topics=chat.can_manage_topics,
            **rights,  # type: ignore[arg-type]
        )

    # ------------------------------------------------------------------ forums

    async def _m_CreateForumTopic(self, bot: Bot, method: Any) -> ForumTopic:
        chat = self._chat(method, method.chat_id)
        if not chat.is_forum:
            raise self._reject("rights", method, "the chat is not a forum", chat.id)
        if not (chat.bot_is_admin and chat.can_manage_topics):
            raise self._reject("rights", method, "not enough rights to create a topic", chat.id)
        if not 1 <= len(method.name) <= 128:
            raise self._reject("rights", method, "topic name must be 1-128 characters", chat.id)
        thread_id = chat.next_thread
        chat.next_thread += 1
        chat.topics[thread_id] = method.name
        return ForumTopic(
            message_thread_id=thread_id,
            name=method.name,
            icon_color=method.icon_color or 7322096,
        )

    async def _m_EditForumTopic(self, bot: Bot, method: Any) -> bool:
        chat = self._chat(method, method.chat_id)
        if method.message_thread_id not in chat.topics:
            raise self._reject("no_thread", method, "TOPIC_ID_INVALID", chat.id)
        if method.name is not None:
            if not 1 <= len(method.name) <= 128:
                raise self._reject("rights", method, "topic name must be 1-128 characters", chat.id)
            if chat.topics[method.message_thread_id] == method.name:
                # What real Telegram answers to a rename that changes nothing. Not
                # a bug in what the bot built, so it is not a recorded rejection.
                raise _bad(method, "TOPIC_NOT_MODIFIED")
            chat.topics[method.message_thread_id] = method.name
        return True

    # -------------------------------------------------------------- not found

    async def _m_GetFile(self, bot: Bot, method: Any) -> Any:
        if method.file_id not in self.files:
            raise TelegramNotFound(method=method, message="Not Found: file not found")
        from aiogram.types import File

        return File(file_id=method.file_id, file_unique_id=method.file_id[:12], file_path="x")


def _mode_name(mode: Any) -> str | None:
    if mode is None:
        return None
    return str(getattr(mode, "value", mode))


__all__ = [
    "STRICT_KINDS",
    "Call",
    "ChatState",
    "FakeTelegram",
    "Rejection",
    "SentMessage",
]
