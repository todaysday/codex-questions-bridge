import asyncio

from conftest import connected_client
from cqb.dao.threads import ThreadDao
from cqb.domain.enums import ArchiveState
from cqb.services.inventory import InventoryService

NOW = 1_000_000
DAY = 86400


def make(client, conn, **kw):
    kw.setdefault("poll_interval", 30)
    kw.setdefault("recency_days", 30)
    return InventoryService(client, ThreadDao(conn), "s1", clock=lambda: NOW, **kw)


def test_roots_descendants_and_archive_states(conn):
    async def main():
        async with connected_client() as (client, fake):
            fake.add_thread("root", NOW - 10)
            fake.add_thread("child", NOW - 5, parent="root")
            fake.add_thread("grand", NOW - 4, parent="child")
            fake.add_thread("old", NOW - 20, archived=True)
            inv = make(client, conn)
            assert await inv.poll_once() == 4
            dao = ThreadDao(conn)
            assert dao.get("s1", "root").is_root and dao.get("s1", "root").root_thread_id == "root"
            assert dao.get("s1", "grand").root_thread_id == "root"
            assert dao.get("s1", "root").archive_state is ArchiveState.ACTIVE
            assert dao.get("s1", "old").archive_state is ArchiveState.ARCHIVED
            assert [t.thread_id for t in dao.list_descendants("s1", "root", 10)] == ["grand", "child"]
    asyncio.run(main())


def test_provisional_root_is_fixed_when_parent_arrives(conn):
    async def main():
        async with connected_client() as (client, fake):
            fake.add_thread("child", NOW - 5, parent="root")
            inv = make(client, conn, page_size=1)
            await inv.poll_once()
            assert ThreadDao(conn).get("s1", "child").root_thread_id == "root"  # parent not known yet
            fake.add_thread("root", NOW - 6)
            await inv.poll_once()
            assert ThreadDao(conn).get("s1", "root").is_root
    asyncio.run(main())


def test_missing_thread_is_never_archived(conn):
    async def main():
        async with connected_client() as (client, fake):
            fake.add_thread("t1", NOW - 1)
            inv = make(client, conn)
            await inv.poll_once()
            del fake.threads["t1"]
            await inv.poll_once()
            t = ThreadDao(conn).get("s1", "t1")
            assert t.archive_state is ArchiveState.ACTIVE
    asyncio.run(main())


def test_archive_events_update_state_only_for_known_threads(conn):
    from cqb.codex.events import ThreadArchiveChanged
    async def main():
        async with connected_client() as (client, fake):
            fake.add_thread("t1", NOW - 1)
            inv = make(client, conn)
            await inv.poll_once()
            await inv.handle(ThreadArchiveChanged("t1", True))
            await inv.handle(ThreadArchiveChanged("ghost", True))
            dao = ThreadDao(conn)
            assert dao.get("s1", "t1").archive_state is ArchiveState.ARCHIVED
            assert dao.get("s1", "ghost") is None
    asyncio.run(main())


def test_pagination_bounds_and_recency_cutoff(conn):
    async def main():
        async with connected_client() as (client, fake):
            for i in range(10):
                fake.add_thread(f"t{i}", NOW - i)
            inv = make(client, conn, page_size=2, max_pages=3)
            assert await inv.poll_once() == 6  # 3 pages x 2, then stops
            list_calls = [p for m, p in fake.calls if m == "thread/list" and not p["archived"]]
            assert len(list_calls) == 3 and all(p["limit"] == 2 for p in list_calls)

            fake.calls.clear()
            fake.add_thread("stale", NOW - 40 * DAY)
            inv = make(client, conn, page_size=50, recency_days=30)
            await inv.poll_once()
            assert ThreadDao(conn).get("s1", "stale") is None
            assert len([1 for m, _ in fake.calls if m == "thread/list"]) == 2  # one page per list
    asyncio.run(main())


def test_waiting_threads_are_reported(conn):
    async def main():
        async with connected_client() as (client, fake):
            fake.add_thread("w", NOW - 1)
            fake.add_thread("i", NOW - 2)
            fake.threads["w"]["status"] = {"type": "active", "activeFlags": ["waitingOnUserInput"]}
            seen = []

            async def on_waiting(tid):
                seen.append(tid)
            await make(client, conn, on_waiting=on_waiting).poll_once()
            assert seen == ["w"]
    asyncio.run(main())


def test_poll_while_disconnected_raises_not_connected(conn):
    import pytest
    from cqb.codex.appserver import AppServerClient, NotConnected
    async def main():
        with pytest.raises(NotConnected):
            await make(AppServerClient("/nonexistent"), conn).poll_once()
    asyncio.run(main())
