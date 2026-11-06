import asyncio

import pytest

from conftest import connected_client
from cqb.codex.appserver import ALLOWED_METHODS, AppServerClient, MethodNotAllowed, NotConnected, RpcError
from cqb.codex.events import normalize
from fake_appserver import wait_until


def arun(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize(
    "method",
    ["turn/start", "turn/steer", "turn/interrupt", "thread/archive", "thread/start", "thread/fork",
     "thread/unarchive", "thread/name/set", "initialize", "command/exec", ""],
)
def test_whitelist_rejects_before_sending(method):
    async def main():
        async with connected_client() as (client, fake):
            with pytest.raises(MethodNotAllowed):
                await client.call(method, {})
            assert fake.calls == []
    arun(main())


def test_resume_cannot_carry_overrides():
    async def main():
        async with connected_client() as (client, fake):
            fake.add_thread("t1", 1)
            with pytest.raises(MethodNotAllowed):
                await client.call("thread/resume", {"threadId": "t1", "approvalPolicy": "never"})
            await client.call("thread/resume", {"threadId": "t1", "excludeTurns": True})
            assert fake.methods() == ["thread/resume"]
    arun(main())


def test_whitelist_content():
    assert ALLOWED_METHODS == {
        "thread/list", "thread/read", "thread/loaded/list", "thread/resume", "thread/unsubscribe",
        "thread/turns/list", "thread/goal/get", "account/rateLimits/read",
    }


@pytest.mark.parametrize("method", ["account/rateLimitResetCredit/consume", "account/usage/read", "thread/goal/set",
                                    "thread/items/list", "thread/queue/list"])
def test_status_whitelist_stays_on_reads(method):
    assert method not in ALLOWED_METHODS


def test_initialize_requests_experimental_api_and_rpc_errors():
    async def main():
        async with connected_client() as (client, fake):
            assert fake.conns[0].capabilities == {"experimentalApi": True}
            await wait_until(lambda: fake.conns[0].initialized)
            with pytest.raises(RpcError, match="no rollout"):
                await client.call("thread/resume", {"threadId": "nope"})
    arun(main())


def test_not_connected_and_timeout():
    async def main():
        client = AppServerClient("/nonexistent.sock")
        with pytest.raises(NotConnected):
            await client.call("thread/list", {})
        with pytest.raises(NotConnected):
            await client.respond(1, {})
    arun(main())


def test_reconnects_and_publishes_connection_events():
    async def main():
        async with connected_client() as (client, fake):
            assert client.inbox.get_nowait().kind == "connected"
            await fake.drop_connections()
            await wait_until(lambda: client.inbox.qsize() >= 2)
            assert client.inbox.get_nowait().kind == "disconnected"
            assert client.inbox.get_nowait().kind == "connected"
            assert client.connected
            assert (await client.call("thread/loaded/list", {}))["data"] == []
    arun(main())


def test_pending_call_fails_when_connection_drops():
    async def main():
        async with connected_client() as (client, fake):
            fake.threads["t"] = {}
            task = asyncio.create_task(client.call("thread/read", {"threadId": "t"}, timeout=5))
            await asyncio.sleep(0)
            await fake.drop_connections()
            with pytest.raises((NotConnected, RpcError)):
                await task
    arun(main())


def test_normalize_ignores_unknown_and_malformed():
    from cqb.codex.appserver import Inbound
    assert normalize(Inbound("notification", "turn/plan/updated", {})) is None
    assert normalize(Inbound("notification", "thread/status/changed", {"oops": 1})) is None
    assert normalize(Inbound("request", "item/commandExecution/requestApproval", {}, 3)) is None
