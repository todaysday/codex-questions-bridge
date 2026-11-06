# Codex Questions Bridge (cqb)

A single-process asyncio service that connects as an extra client to the shared Codex
app-server daemon, inventories threads and captures live `request_user_input` questions.
It also runs the Telegram control UI: a pinned thread registry, an Inbox with question cards,
and manually created consultant topics. Messages typed in a consultant topic go to a long-running PicoClaw
gateway (the consultant) over its `pico` WebSocket channel; the consultant reads Codex history through a
read-only MCP server inside the same process. `/status` (and the registry button `ℹ️ Подробнее`) shows a deterministic status card
(no LLM, separate from the consultant).
Logs carry ids and counts, never question or message text. Design: `doc/Codex_Questions_Bridge_Design.md`.

## Run

    python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
    cp config.example.toml config.toml        # set [codex] socket, [db] path, [telegram] ids
    CQB_TELEGRAM_TOKEN=... .venv/bin/python -m cqb --config config.toml   # or CQB_CONFIG=...
    .venv/bin/pytest                          # no network, no real daemon
    cd /tmp && sudo -n /path/.venv/bin/python /path/scripts/smoke_real_daemon.py   # read-only check

Read-only boundary: the app-server client refuses every method except `thread/list`,
`thread/read`, `thread/loaded/list`, `thread/resume` (thread id only), `thread/unsubscribe` and the status reads
`thread/turns/list`, `thread/goal/get`, `account/rateLimits/read` (the status card never resumes or subscribes);
`thread/items/list` (paged history for the consultant's tools);
the only other write is the JSON-RPC response to a captured question request, and only
when the caller asks (`QuestionService.answer`). Secret questions are stored without text
and can never be answered by the Bridge.

Layers (consultant chat added): `pico/` (gateway, health) and `mcp/` (server, tools) depend on `services`; `services/relay`
and `services/context` never import aiohttp or Telegram; `integrations/picoclaw_scope_hook.py` is a standalone stdlib script.

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
`/thread` (consultant topic), `/status` (consultant topic: status card; the registry
button `ℹ️ Подробнее` posts the same card into the control topic). Root context/cache come from the tail of the
rollout file (`[codex] codex_home`, default `/root/.codex`; read-only, path-checked). Live check:
`cd /tmp && sudo -n /path/.venv/bin/python /path/scripts/smoke_status.py`. Consultants are created only by the `Создать консультанта` button.

Layout: `telegram/` holds `api` (Bot API client), `poller`, `router` (authorization and dispatch),
`control` (service topics, registry message), `inbox` (question cards), `topics` (consultant topic port),
`keys` (one-time callback ids), and the pure views `registry_view`, `inbox_view`, `consultant_view`.

## Memory and priority

One process, no threads or workers; bounded queues, pages and previews; `gc.freeze()` after
wiring; RSS is logged every 5 minutes (about 37 MiB at idle with 300 threads, mostly the
aiohttp import). `deploy/cqb.service` sets `CPUWeight`, `MemoryMax`, and has `Nice=-10`
(lower to `-20` for the highest priority) and `IOSchedulingClass=realtime` commented out.
SQLite uses WAL, a 512 KiB page cache and no mmap.

## Consultant chat

A registered, open consultant topic forwards free text to its PicoClaw session (`pico_session_key`, one WebSocket per
active session, strictly one turn at a time, at most 5 queued messages: the sender gets `В очереди`). Nothing else is
forwarded: service topics, unknown topics, archived consultants, strangers and `/status`, `/questions`, `/thread` never
reach PicoClaw. While a turn runs the topic shows the Telegram typing indicator (refreshed every 4 s, no extra messages).
There is no reply time limit (`turn_timeout_seconds = 0`); the owner can cancel a turn with exactly `/stop`, the one
command passed through unescaped. Every other text starting with `/` or `!` (after whitespace and invisible characters)
gets the visible prefix `User says: ` so PicoClaw cannot run `/clear`, `/switch`, `/reload`, `/subagents`, ...
Replies are plain text, at most 12000 characters, split below 4096 per message. If PicoClaw or its scope hook looks dead
(`GET /health` fails or the hook's heartbeat is silent) the topic answers `Консультант временно недоступен`; queued
messages survive a dropped socket and are retried on a fresh connection.

Without `[pico]` in the config the bot runs as before and consultant topics answer that the chat is not configured. With
`[pico]` the process refuses to start unless `CQB_PICO_TOKEN` and `CQB_MCP_TOKEN` (two different secrets) are set.

### The context API (MCP, loopback)

`http://127.0.0.1:<[mcp] port>/mcp`, bearer `CQB_MCP_TOKEN`, streamable HTTP (stateless, JSON responses). Tools (all
read-only; every result is redacted and bounded; failures are `{"error": code, ...}`): `get_thread_overview`,
`list_agents`, `get_recent_events` (<=100 events, <=64 KiB; no tool output), `read_event` (<=16 KiB, by the event `ref`),
`get_questions` (never the text of a secret question). Each tool takes `root_id`/`source_id`, which the scope hook
overwrites with the trusted values; the server still requires an open consultant root of the configured source and a
root-or-descendant `thread_id`. The same listener serves the hook's `GET /internal/scope/<session>` and
`POST /internal/hook/beat`.

## PicoClaw setup

Tested with PicoClaw v0.3.1 (linux x86_64). Re-check every key against any other version: the config loader rejects
unknown fields, so `config.json` cannot carry comments.

1. Install the release binary: unpack the `picoclaw` binary of the v0.3.1 release to `/opt/picoclaw/picoclaw`, create a
   system user `picoclaw` and `/var/lib/picoclaw` (mode 0700).
2. Copy `deploy/picoclaw/config.example.json` to `/var/lib/picoclaw/config.json` (mode 0600, owner `picoclaw`), and the
   two `ws-*` directories next to it (they hold each agent's `AGENT.md`: the tool allowlist, which is what actually
   removes `exec`/file/web tools, and the consultant prompt). Adjust paths, the port, and the hook path
   (`/opt/cqb/integrations/picoclaw_scope_hook.py`). Replace `__CQB_MCP_TOKEN__` with the value of `CQB_MCP_TOKEN`
   (for example `sed -i "s|__CQB_MCP_TOKEN__|$CQB_MCP_TOKEN|" config.json`): MCP headers are a plain config value, not a
   `.security.yml` field.
3. Tokens: copy `deploy/picoclaw/security.example.yml` to `/var/lib/picoclaw/.security.yml` (mode 0600) and fill in the pico
   channel token (the same value as `CQB_PICO_TOKEN` in `/etc/cqb/cqb.env`) and the model key. Put `CQB_MCP_TOKEN=...` and
   `CQB_MCP_URL=http://127.0.0.1:18791` into `/etc/picoclaw/picoclaw.env` (mode 0600): the scope hook inherits the
   gateway's environment and uses them to ask the Bridge which root a session may read.
4. Model: the template points both agents at the OmniRoute combo through `model_list` (`api_base`
   `http://127.0.0.1:20129/v1`, model `Beam`, 1M context; `context_window` 1000000, summarization at 80%). Use a key
   restricted to that combo, and an SSH tunnel if OmniRoute runs elsewhere. Set `codex-status` to a cheaper model if wanted.
5. Install `deploy/picoclaw/picoclaw-gateway.service` (own CPU/memory/task limits, hardening, `After=cqb.service` because
   PicoClaw connects to the Bridge's MCP server while it starts) and start the Bridge first. A dead hook process is never
   respawned by PicoClaw: the Bridge notices (heartbeat) and refuses consultant turns until the gateway is restarted.
   Never run `picoclaw agent` against live sessions.
6. In `config.toml` add `[pico]` and `[mcp]` (see `config.example.toml`) and set `CQB_PICO_TOKEN`, `CQB_MCP_TOKEN`.

The Status Agent narrative (`codex-status`, delegated or routed) is a later increment: its `ws-status/AGENT.md` is only a stub.

End-to-end check with the real binary (needs the git-ignored `spike/` files; loopback only, kills what it starts):
`.venv/bin/python -m pytest -q -m e2e tests/e2e`.
