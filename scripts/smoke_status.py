"""Read-only status card check against the real daemon (one thread picked by thread/list).

Usage: cd /tmp && sudo -n /path/.venv/bin/python /path/scripts/smoke_status.py [SOCKET] [CODEX_HOME]
Prints states, counts, model names, percentages and item kinds only (no titles, goal text, paths or commands).
"""

import asyncio
import glob
import sys

from cqb.codex.appserver import AppServerClient
from cqb.codex.rollout_tail import read_usage
from cqb.codex.status_reader import DaemonStatusReader
from cqb.services.live_state import LiveState
from cqb.services.status import StatusService


def _now(activity) -> str:
    """Kinds and flags only: never a command head, message text or output."""
    if activity is None:
        return "no data"
    item = activity.item
    return f"turn_running={activity.turn_running} item={(item.kind.value, item.finished) if item else None}"


async def main(socket_path: str, home: str) -> int:
    client = AppServerClient(socket_path, client_name="cqb-smoke", request_timeout=15)
    runner = asyncio.create_task(client.run())
    try:
        async with asyncio.timeout(30):
            while not client.connected:
                await asyncio.sleep(0.1)
            page = await client.call("thread/list", {"limit": 5, "sortKey": "updated_at"})
            root = next((t for t in page.get("data", []) if not t.get("parentThreadId")), None)
            if root is None:
                print("no root thread found")
                return 1
            service = StatusService(DaemonStatusReader(client), LiveState(), lambda p: read_usage(p, home), "local")
            r = await service.collect("local", root["id"])
    except TimeoutError:
        print("FAILED: no connection / response within 30 s")
        return 1
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    u = r.usage
    print(f"state={r.state.value} model={r.model} effort={r.effort} quiet_min={r.quiet_minutes} turn_s={r.turn_elapsed_s}")
    print(f"context={u.fill_percent if u else None}% window={u.context_window if u else None} "
          f"cache={u.cache_percent if u else None}% last_activity_s_ago={r.now - r.last_activity if r.last_activity else None}")
    print(f"goal={'yes:' + r.goal.status if r.goal else 'none'} "
          f"agents_total={r.agents_total} more={r.agents_more} working={r.agents_working} "
          f"waiting={r.agents_waiting} idle={r.agents_idle} lines={len(r.agents)}")
    print(f"root now: {_now(r.activity)}")
    for a in r.agents[:3]:
        print(f"  agent state={a.state.value} model={a.model} effort={a.effort} ctx={a.context_percent} now: {_now(a.activity)}")
    print(f"limits={r.limits}")
    return 0


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "/root/.codex/app-server-control/app-server-control.sock"
    raise SystemExit(asyncio.run(main(path, sys.argv[2] if len(sys.argv) > 2 else "/root/.codex")))
