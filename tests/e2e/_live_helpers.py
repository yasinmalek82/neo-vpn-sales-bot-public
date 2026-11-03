"""Helpers for the bugs a live run on real Telegram found.

Candidate for the shared harness: ``say_in_group`` (an update that arrives
from a group chat rather than from the actor's private chat).
"""

from __future__ import annotations

from typing import Any

from tests.e2e.harness.actors import Actor
from tests.e2e.harness.telegram import ChatState, SentMessage


async def say_in_group(
    actor: Actor, group: ChatState, *, thread_id: int | None = None, **fields: Any
) -> None:
    """``actor`` posts in ``group``: ``text=...`` or a service field such as
    ``new_chat_members=[...]`` / ``forum_topic_created={...}``."""
    await actor._ensure_registered()  # noqa: SLF001
    app = actor.app
    text = fields.get("text") or ""
    message_id = app.tg.record_incoming(group.id, kind="text", text=text)
    message: dict[str, Any] = {
        "message_id": message_id,
        "date": app.now_unix(),
        "chat": app.tg.chat_raw(group),
        "from": actor._user(),  # noqa: SLF001
        **fields,
    }
    if text.startswith("/"):
        command = text.split(" ", 1)[0]
        message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
    if thread_id is not None:
        message["message_thread_id"] = thread_id
        message["is_topic_message"] = True
    await app.feed({"update_id": app.next_update_id(), "message": message})


async def press_in_group(actor: Actor, target: SentMessage, callback_data: str) -> str:
    """``actor`` taps a callback button under ``target``, a message in a group."""
    await actor._ensure_registered()  # noqa: SLF001
    app = actor.app
    callback_id = app.next_callback_id()
    app.tg.open_callback(callback_id, target.chat_id, target.message_id)
    await app.feed(
        {
            "update_id": app.next_update_id(),
            "callback_query": {
                "id": callback_id,
                "from": actor._user(),  # noqa: SLF001
                "chat_instance": f"ci-{target.chat_id}",
                "message": app.tg.message_raw(target),
                "data": callback_data,
            },
        }
    )
    return callback_id


def bot_calls(app: Any) -> int:
    """How many Bot API calls the bot has made so far."""
    return len(app.tg.calls)
