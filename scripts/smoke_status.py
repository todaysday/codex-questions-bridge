"""Read-only status card check against the real daemon (one thread picked by thread/list).

Usage: cd /tmp && sudo -n /path/.venv/bin/python /path/scripts/smoke_status.py [SOCKET] [CODEX_HOME]
Prints states, counts, model names, percentages and item kinds only (no titles, goal text, paths or commands),
then the card as the Bridge would post it, without the Status Agent summary. Thread and subagent names are
shown; the goal text and command heads are replaced by placeholders, message text is never read.
"""

import asyncio
import dataclasses
import sys
import time

from cqb.codex.appserver import AppServerClient
from cqb.codex.rollout_count import CompactionCounter
from cqb.codex.rollout_tail import read_usage
from cqb.codex.status_reader import DaemonStatusReader
from cqb.services.live_state import LiveState
from cqb.services.status import StatusService
from cqb.telegram.status_view import render_status


def _masked(r):
    """The report with the goal text and command heads replaced: what is safe to print from a real daemon."""
    def activity(a):
        item = a.item if a else None
        return a if item is None else dataclasses.replace(a, item=dataclasses.replace(item, detail="…" if item.detail else None))
    goal = dataclasses.replace(r.goal, objective="(текст цели скрыт)") if r.goal else None
    agents = tuple(dataclasses.replace(a, activity=activity(a.activity)) for a in r.agents)
    return dataclasses.replace(r, goal=goal, agents=agents, activity=activity(r.activity))


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
            page = await client.call("thread/list", {"limit": 5, "sortKey": "updated_at", "useStateDbOnly": True})
            roots = [t for t in page.get("data", []) if not t.get("parentThreadId")]
            root = None
            for t in roots:  # the first root that has subagents shows the most
                kids = await client.call("thread/list", {"ancestorThreadId": t["id"], "limit": 1, "useStateDbOnly": True})
                if kids.get("data"):
                    root = t
                    break
            root = root or next(iter(roots), None)
            if root is None:
                print("no root thread found")
                return 1
            service = StatusService(
                DaemonStatusReader(client), LiveState(), lambda p: read_usage(p, home), "local",
                compactions=CompactionCounter(home))
            started = time.monotonic()
            r = await service.collect("local", root["id"])
            print(f"collect took {(time.monotonic() - started) * 1000:.0f} ms (cold: nothing cached yet)")
            started = time.monotonic()
            await service.collect("local", root["id"])
            print(f"second collect took {(time.monotonic() - started) * 1000:.0f} ms (rollout counts cached)")
    except TimeoutError:
        print("FAILED: no connection / response within 30 s")
        return 1
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    u = r.usage
    print(f"state={r.state.value} model={r.model} effort={r.effort} quiet_min={r.quiet_minutes} turn_s={r.turn_elapsed_s}")
    print(f"context={u.fill_percent if u else None}% window={u.context_window if u else None} "
          f"cache_permille={u.cache_permille if u else None} last_activity_s_ago={r.now - r.last_activity if r.last_activity else None}")
    print(f"goal={'yes:' + r.goal.status if r.goal else 'none'} "
          f"agents_total={r.agents_total} more={r.agents_more} working={r.agents_working} "
          f"waiting={r.agents_waiting} idle={r.agents_idle} lines={len(r.agents)}")
    print(f"root compactions={r.compactions}")
    print(f"root now: {_now(r.activity)}")
    for a in r.agents[:3]:
        print(f"  agent state={a.state.value} model={a.model} effort={a.effort} ctx={a.context_percent} cache_permille={a.cache_permille} compactions={a.compactions} now: {_now(a.activity)}")
    print(f"limits={r.limits}")
    print("--- card (HTML as sent; goal text and command heads masked) ---")
    print(render_status(_masked(r)))
    return 0


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "/root/.codex/app-server-control/app-server-control.sock"
    raise SystemExit(asyncio.run(main(path, sys.argv[2] if len(sys.argv) > 2 else "/root/.codex")))
