"""A person at a phone: sends messages, taps buttons, reads what came back.

Every action builds a real Telegram ``Update`` and feeds it to the assembled
dispatcher; nothing here calls a handler. What a person sees is read back from
the fake Telegram's chat state, so an edit shows as the edited text, a deleted
message is gone, and a tap can only hit a button that is actually on screen.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any

from PIL import Image

from tests.e2e.harness.telegram import ChatState, SentMessage

if TYPE_CHECKING:
    from tests.e2e.harness.app import App


class UnansweredCallback(AssertionError):
    """The bot handled a button tap and never answered it: the button would spin."""


def receipt_png(color: tuple[int, int, int] = (200, 60, 60)) -> bytes:
    """A small valid image, so a «photo» is a real one."""
    out = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(out, format="PNG")
    return out.getvalue()


class Actor:
    def __init__(
        self,
        app: App,
        user_id: int,
        *,
        name: str,
        first_name: str | None,
        last_name: str | None = None,
        username: str | None = None,
        known_to_bot: bool = False,
    ) -> None:
        self.app = app
        # True: create this person's database row before their first action, as
        # if they had used the bot before (admins). Customers meet the bot with /start.
        self._registered = not known_to_bot
        self.user_id = user_id
        self.name = name
        self.first_name = first_name
        self.last_name = last_name
        self.username = username

    def __repr__(self) -> str:
        return f"<Actor {self.name} id={self.user_id}>"

    # ---------------------------------------------------------------- sending

    def _user(self) -> dict[str, Any]:
        user: dict[str, Any] = {
            "id": self.user_id,
            "is_bot": False,
            "language_code": "fa",
        }
        if self.first_name is not None:
            user["first_name"] = self.first_name
        else:
            user["first_name"] = ""
        if self.last_name:
            user["last_name"] = self.last_name
        if self.username:
            user["username"] = self.username
        return user

    def _chat(self) -> ChatState:
        return self.app.tg.add_private_chat(
            self.user_id, first_name=self.first_name, username=self.username
        )

    async def _ensure_registered(self) -> None:
        """An admin has used the bot before: the database already knows them.

        Without a row ``db_user`` is ``None`` and every admin action answers
        «عملیات انجام نشد». That is true of the real bot for an admin who never
        pressed /start, so a scenario can ask for it (``app.admin(started=False)``).
        """
        if self._registered:
            return
        self._registered = True
        from database.repositories.user import get_or_create_user

        async with self.app.session() as session:
            await get_or_create_user(
                session,
                telegram_id=self.user_id,
                username=self.username,
                first_name=self.first_name,
                last_name=self.last_name,
                language_code="fa",
                admin_telegram_ids=self.app.settings.admin_telegram_ids,
            )

    async def _send_message(self, **fields: Any) -> int:
        await self._ensure_registered()
        chat = self._chat()
        kind = "photo" if "photo" in fields else "text"
        message_id = self.app.tg.record_incoming(
            chat.id,
            kind=kind,
            text=fields.get("text") or fields.get("caption") or "",
            file_id=fields["photo"][-1]["file_id"] if "photo" in fields else None,
        )
        raw = {
            "update_id": self.app.next_update_id(),
            "message": {
                "message_id": message_id,
                "date": self.app.now_unix(),
                "chat": self.app.tg.chat_raw(chat),
                "from": self._user(),
                **fields,
            },
        }
        await self.app.feed(raw)
        return message_id

    async def start(self, payload: str | None = None) -> None:
        """``/start`` (or ``/start <payload>``, as a deep link would send)."""
        await self.send("/start" if payload is None else f"/start {payload}")

    async def send(self, text: str) -> None:
        fields: dict[str, Any] = {"text": text}
        if text.startswith("/"):
            command = text.split(" ", 1)[0]
            fields["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
        await self._send_message(**fields)

    async def send_photo(self, caption: str | None = None, *, data: bytes | None = None) -> str:
        """Send a photo (a receipt, usually). Returns its file id."""
        file_id = self.app.tg.register_file(data or receipt_png())
        fields: dict[str, Any] = {
            "photo": [
                {"file_id": file_id, "file_unique_id": file_id[:12], "width": 64, "height": 64}
            ]
        }
        if caption:
            fields["caption"] = caption
        await self._send_message(**fields)
        return file_id

    async def tap_reply(self, label: str) -> None:
        """Tap a button of the keyboard at the bottom of the screen."""
        chat = self._chat()
        labels = [b for row in (chat.reply_keyboard or []) for b in row]
        if label not in labels:
            raise AssertionError(
                f"{self.name} has no bottom-keyboard button {label!r}. "
                f"The bottom keyboard shows: {labels or 'nothing (no keyboard on screen)'}"
            )
        await self.send(label)

    # ----------------------------------------------------------------- tapping

    def _screens(self) -> list[SentMessage]:
        """Messages that carry inline buttons, newest first."""
        return [m for m in self._received()[::-1] if m.markup is not None]

    def _find_button(self, label: str) -> tuple[SentMessage, Any]:
        screens = self._screens()
        for message in screens:
            for row in message.markup.inline_keyboard:  # type: ignore[union-attr]
                for button in row:
                    if button.text == label:
                        return message, button
        # A label typed from memory is often a piece of the real one («تایید» for
        # «✅ تایید»). Like the exact match it looks at the newest message that has
        # any such button, and accepts it only when exactly one button there fits.
        partial: list[tuple[SentMessage, Any]] = []
        for m in screens:
            partial = [(m, b) for row in m.markup.inline_keyboard for b in row if label in b.text]  # type: ignore[union-attr]
            if partial:
                break
        if len(partial) == 1:
            return partial[0]
        shown = [f"  #{m.message_id}: {m.rows}" for m in screens[:6]]
        hint = f" ({len(partial)} buttons contain it)" if partial else ""
        raise AssertionError(
            f"{self.name} sees no button {label!r}{hint}. Buttons on screen, newest first:\n"
            + ("\n".join(shown) or "  (none)")
        )

    async def tap(self, label: str) -> None:
        """Tap an inline button by its visible text, on the newest message that has it."""
        message, button = self._find_button(label)
        if button.callback_data is None:
            raise AssertionError(
                f"button {label!r} opens a link ({button.url}), not a callback; "
                "read it with actor.button_url(label)"
            )
        await self.press(button.callback_data, message=message)

    def button_url(self, label: str) -> str:
        _, button = self._find_button(label)
        if button.url is None:
            raise AssertionError(f"button {label!r} is not a link")
        return str(button.url)

    async def press(
        self,
        callback_data: str,
        *,
        message: SentMessage | None = None,
        expect_answer: bool = True,
    ) -> None:
        """Send raw ``callback_data``: a stale button, or one forged by a hacked client.

        By default the bot must answer the callback; an unanswered one raises
        ``UnansweredCallback``, because that is a spinner on the person's screen.
        """
        await self._ensure_registered()
        target = message or self.last_message
        callback_id = self.app.next_callback_id()
        self.app.tg.open_callback(callback_id, self.user_id, target.message_id)
        chat = self._chat()
        raw = {
            "update_id": self.app.next_update_id(),
            "callback_query": {
                "id": callback_id,
                "from": self._user(),
                "chat_instance": f"ci-{self.user_id}",
                "message": self.app.tg.message_raw(target),
                "data": callback_data,
            },
        }
        raw["callback_query"]["message"]["chat"] = self.app.tg.chat_raw(chat)
        await self.app.feed(raw)
        if expect_answer and not self.app.tg.callbacks[callback_id].answered:
            raise UnansweredCallback(
                f"the bot never answered the tap on {callback_data!r}: the button would "
                f"spin on {self.name}'s screen (screen: {target!r})"
            )

    # ----------------------------------------------------------------- reading

    def _received(self) -> list[SentMessage]:
        chat = self.app.tg.chats.get(self.user_id)
        if chat is None:
            return []
        return [m for m in self.app.tg.history if m.chat_id == chat.id and not m.deleted]

    @property
    def last_message(self) -> SentMessage:
        """The message most recently sent or edited, as it stands now."""
        received = self._received()
        if not received:
            raise AssertionError(f"{self.name} has received nothing yet")
        return max(received, key=lambda m: m.touched)

    @property
    def last_text(self) -> str:
        """What the last message says, as read (tags gone). A photo reads as its caption."""
        return self.last_message.text

    @property
    def last_html(self) -> str:
        """The last message as the bot wrote it, tags included."""
        return self.last_message.html

    @property
    def buttons(self) -> list[str]:
        """Inline button labels on the last message."""
        return self.last_message.buttons

    @property
    def rows(self) -> list[list[str]]:
        return self.last_message.rows

    @property
    def reply_buttons(self) -> list[str]:
        """The bottom keyboard on screen now, left to right, top to bottom."""
        chat = self.app.tg.chats.get(self.user_id)
        return [b for row in (chat.reply_keyboard or []) for b in row] if chat else []

    @property
    def photos(self) -> list[SentMessage]:
        return [m for m in self._received() if m.kind == "photo"]

    @property
    def messages(self) -> list[SentMessage]:
        return self._received()

    @property
    def popup(self) -> str | None:
        """Text of the last callback answer (a toast or alert), or ``None``."""
        answers = [
            c.text
            for c in self.app.tg.callbacks.values()
            if c.chat_id == self.user_id and c.answered and c.text
        ]
        return answers[-1] if answers else None

    def mark(self) -> int:
        """A bookmark: ``messages_since(mark)`` returns what arrived or changed after it."""
        return self.app.tg.last_seq

    def messages_since(self, mark: int) -> list[SentMessage]:
        return sorted((m for m in self._received() if m.touched > mark), key=lambda m: m.seq)

    def texts_since(self, mark: int) -> list[str]:
        return [m.text for m in self.messages_since(mark)]

    def transcript(self, last: int = 12) -> str:
        """The recent chat, for a failure message."""
        lines = []
        for m in self._received()[-last:]:
            lines.append(f"  bot #{m.message_id}: {m.text[:100]!r} {m.rows or ''}")
        return "\n".join(lines) or "  (nothing received)"
