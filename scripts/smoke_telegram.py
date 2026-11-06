"""Telegram setup check: getMe, getChat and getChatMember for the bot only. Sends nothing.

Usage: CQB_TELEGRAM_TOKEN=... .venv/bin/python scripts/smoke_telegram.py [CONFIG]
Prints ok/FAIL facts; neither the token nor message data is printed.
"""

import asyncio
import os
import sys

from cqb.config import ConfigError, load_config, read_token
from cqb.telegram.api import BotApi, TelegramError


def fact(label: str, ok: bool, note: str = "") -> bool:
    print(f"{'ok  ' if ok else 'FAIL'} {label}{' - ' + note if note else ''}")
    return ok


async def main(config_path: str) -> int:
    try:
        config = load_config(config_path)
        token = read_token(os.environ)
    except ConfigError as exc:
        print(f"FAIL config: {exc}")
        return 2
    chat_id = config.telegram.chat_id
    api = BotApi(token)
    await api.start()
    good = True
    try:
        me = await api.get_me()
        good &= fact("token accepted", True, f"@{me.username}")
        # Privacy mode only matters when the bot is not an admin; admins see all messages.
        privacy_off = me.can_read_all_group_messages
        chat = await api.get_chat(chat_id)
        good &= fact("chat is a supergroup", chat.type == "supergroup", chat.type)
        good &= fact("chat is a forum (topics enabled)", chat.is_forum)
        member = await api.get_chat_member(chat_id, me.user_id)
        is_admin = member.status in ("administrator", "creator")
        good &= fact("bot is admin", is_admin, member.status)
        good &= fact("can_manage_topics", member.can_manage_topics)
        fact("can_pin_messages (registry is pinned when true)", member.can_pin_messages)
        sees_text = privacy_off or is_admin
        good &= fact(
            "bot sees free text in consultant topics", sees_text,
            f"privacy mode off={privacy_off}, admin={is_admin}",
        )
    except TelegramError as exc:
        print(f"FAIL telegram: {exc}")
        return 1
    finally:
        await api.close()
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CQB_CONFIG", "config.toml"))))
