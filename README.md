# Codex Questions Bridge (cqb)

A single-process asyncio service that connects as an extra client to the shared Codex
app-server daemon, inventories threads and captures live `request_user_input` questions.
It also runs the Telegram control UI: a pinned thread registry, an Inbox with question cards,
and manually created consultant topics. There is no PicoClaw gateway, MCP/context API or `/status`
yet: messages typed in a consultant topic go to a no-op relay that logs a counter only.
Logs carry ids and counts, never question or message text. Design: `doc/Codex_Questions_Bridge_Design.md`.

## Run

    python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
    cp config.example.toml config.toml        # set [codex] socket, [db] path, [telegram] ids
    CQB_TELEGRAM_TOKEN=... .venv/bin/python -m cqb --config config.toml   # or CQB_CONFIG=...
    .venv/bin/pytest                          # no network, no real daemon
    cd /tmp && sudo -n /path/.venv/bin/python /path/scripts/smoke_real_daemon.py   # read-only check

Read-only boundary: the app-server client refuses every method except `thread/list`,
`thread/read`, `thread/loaded/list`, `thread/resume` (thread id only), `thread/unsubscribe`;
the only other write is the JSON-RPC response to a captured question request, and only
when the caller asks (`QuestionService.answer`). Secret questions are stored without text
and can never be answered by the Bridge.

Layers: `domain` (DTOs) <- `dao` (SQLite) and `codex` (daemon client) <- `services` <- `telegram` <- `main`.
`domain`, `dao` and the consultant/registry services never import network code; `telegram/api.py`
is the only Bot API wire code (one shared aiohttp session).

## Telegram setup

1. Create a bot with @BotFather; put its token only in the environment: `CQB_TELEGRAM_TOKEN`
   (systemd: `/etc/cqb/cqb.env`, mode 0600). The process refuses to start without it.
2. Create a private supergroup, enable Topics (forum mode), and add the bot as an admin with
   "Manage topics" (and "Pin messages" for the pinned registry).
3. Free text in consultant topics reaches the bot only if it is an admin or privacy mode is off
   (@BotFather `/setprivacy` -> Disable). Making the bot admin is enough.
4. Set `[telegram] chat_id` (the `-100...` id) and `allowed_user_ids` in `config.toml`; everyone else,
   forwarded and anonymous-admin messages are ignored.
5. Check without side effects: `CQB_TELEGRAM_TOKEN=... .venv/bin/python scripts/smoke_telegram.py config.toml`
   (calls only getMe, getChat, getChatMember).

Commands: `/threads` (control topic: refresh now), `/questions` (Inbox: all; consultant topic: that thread),
`/thread` (consultant topic). Consultants are created only by the `Создать консультанта` button.

Layout: `telegram/` holds `api` (Bot API client), `poller`, `router` (authorization and dispatch),
`control` (service topics, registry message), `inbox` (question cards), `topics` (consultant topic port),
`keys` (one-time callback ids), and the pure views `registry_view`, `inbox_view`, `consultant_view`.

## Memory and priority

One process, no threads or workers; bounded queues, pages and previews; `gc.freeze()` after
wiring; RSS is logged every 5 minutes (about 37 MiB at idle with 300 threads, mostly the
aiohttp import). `deploy/cqb.service` sets `CPUWeight`, `MemoryMax`, and has `Nice=-10`
(lower to `-20` for the highest priority) and `IOSchedulingClass=realtime` commented out.
SQLite uses WAL, a 512 KiB page cache and no mmap.
