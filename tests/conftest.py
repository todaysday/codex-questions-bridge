import asyncio
import contextlib

import pytest

from cqb.codex.appserver import AppServerClient
from cqb.dao import db
from fake_appserver import FakeAppServer, wait_until


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    c.execute("INSERT INTO codex_sources VALUES ('s1','S1','app_server',1)")
    yield c
    c.close()


@contextlib.asynccontextmanager
async def connected_client(**kw):
    """A running client against a fresh fake server; yields (client, fake)."""
    fake = FakeAppServer()
    await fake.start()
    client = AppServerClient(fake.path, backoff_min=0.05, backoff_max=0.1, request_timeout=2, **kw)
    task = asyncio.create_task(client.run())
    try:
        await wait_until(lambda: client.connected)
        yield client, fake
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await fake.stop()
