"""Runs the real cqb.main.run in its own process (so its RSS can be measured) against fakes.

usage: run_bridge.py <json spec>   env: CQB_PICO_TOKEN, CQB_MCP_TOKEN
"""

import asyncio
import json
import signal
import sys

from cqb import main as cqb_main
from cqb.config import CodexConfig, Config, ConsultantTokens, DbConfig, McpConfig, PicoConfig, TelegramConfig


async def amain(spec: dict) -> None:
    config = Config(
        CodexConfig(spec["socket"], "local", 30, 36500), DbConfig(spec["db"]),
        TelegramConfig(spec["chat_id"], "📋 Треды Codex", (spec["owner"],), 5),
        pico=PicoConfig(url=spec["pico_url"], idle_seconds=spec.get("idle", 120)), mcp=McpConfig(port=spec["mcp_port"]),
    )
    import os
    tokens = ConsultantTokens(os.environ["CQB_PICO_TOKEN"], os.environ["CQB_MCP_TOKEN"])
    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    await cqb_main.run(config, stop, token=spec["tg_token"], telegram_base_url=spec["tg_base"], consultant_tokens=tokens)


if __name__ == "__main__":
    import logging
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    asyncio.run(amain(json.loads(sys.argv[1])))
