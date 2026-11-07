#!/usr/bin/env python3
"""Live smoke of the PicoClaw consultant chain WITHOUT Telegram.

Opens ws://127.0.0.1:18790/pico/ws for a throwaway session id, sends one prompt, prints the reply text
and the elapsed time. The pico token is read from the environment (CQB_PICO_TOKEN), never from argv and
never printed. Run it with the bridge's env sourced, for example:
  sudo bash -c 'set -a; . /etc/cqb/cqb.env; set +a; /opt/cqb/.venv/bin/python scripts/smoke_pico.py \
      --session cqb-smoke-1 "Ответь одним словом: пинг"'
Exit code: 0 reply received, 1 error/no reply, 2 bad usage.
"""
import argparse
import asyncio
import json
import os
import sys
import time
import uuid

import aiohttp


async def run(url: str, session: str, text: str, timeout: float, idle: float) -> int:
    token = os.environ.get("CQB_PICO_TOKEN", "")
    if not token:
        print("CQB_PICO_TOKEN is not set", file=sys.stderr)
        return 2
    reply = ""
    t0 = time.monotonic()
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(url, params={"session_id": session},
                                   headers={"Authorization": f"Bearer {token}"}, heartbeat=30) as ws:
            await ws.send_json({"type": "message.send", "id": str(uuid.uuid4()), "session_id": session,
                                "payload": {"content": text}})
            deadline = t0 + timeout
            while True:
                wait = idle if reply else deadline - time.monotonic()
                if wait <= 0:
                    break
                try:
                    msg = await ws.receive(timeout=wait)
                except TimeoutError:
                    break
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                frame = json.loads(msg.data)
                payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
                if frame.get("type") == "error":
                    print("error:", payload.get("code") or "error")
                    return 1
                if frame.get("type") not in ("message.create", "message.update"):
                    continue
                if payload.get("kind") == "thought" or payload.get("placeholder"):
                    continue
                content = payload.get("content")
                if payload.get("kind") == "tool_calls" or (isinstance(content, str) and not content.strip()):
                    reply = ""
                elif isinstance(content, str):
                    reply = content
    elapsed = time.monotonic() - t0
    print(f"reply: {reply!r}")
    print(f"elapsed: {elapsed:.2f}s (includes the {idle:.1f}s quiet period that ends the turn)")
    return 0 if reply else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--session", default="cqb-smoke-1")
    ap.add_argument("--url", default="ws://127.0.0.1:18790/pico/ws")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--idle", type=float, default=1.0)
    a = ap.parse_args()
    return asyncio.run(run(a.url, a.session, a.text, a.timeout, a.idle))


if __name__ == "__main__":
    sys.exit(main())
