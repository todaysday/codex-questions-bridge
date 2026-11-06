"""Read-only smoke test: initialize + thread/list(limit 5) against the real daemon.

Usage: sudo -n .venv/bin/python scripts/smoke_real_daemon.py [SOCKET]
Prints thread ids and states only (no titles or previews).
"""

import asyncio
import glob
import sys

from cqb.codex.appserver import AppServerClient


async def main(socket_path: str) -> int:
    client = AppServerClient(socket_path, client_name="cqb-smoke", request_timeout=15)
    runner = asyncio.create_task(client.run())
    try:
        async with asyncio.timeout(20):
            while not client.connected:
                await asyncio.sleep(0.1)
            print("initialize: ok")
            result = await client.call("thread/list", {"limit": 5})
    except TimeoutError:
        print("FAILED: no connection / response within 20 s")
        return 1
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    for t in result.get("data", []):
        status = t.get("status") or {}
        print(t["id"], status.get("type"), ",".join(status.get("activeFlags") or []))
    print("nextCursor:", "yes" if result.get("nextCursor") else "no")
    return 0


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else next((p for p in sorted(glob.glob("/tmp/codex-daemon-0/*")) if not p.endswith(".lock")), "")
    raise SystemExit(asyncio.run(main(path)))
