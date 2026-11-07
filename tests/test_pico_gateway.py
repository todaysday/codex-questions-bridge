import asyncio
import contextlib

import aiohttp
import pytest

from cqb.config import PicoConfig
from cqb.pico.gateway import (
    BAD_SESSION, CONN_FAILED, CONN_LOST, EXCHANGE_FAILED, QUEUE_MAX, TOO_MANY_SOCKETS, PicoGateway, ReplyAssembler,
    escape_command,
)
from cqb.pico.health import HEALTH_FAILING, HOOK_SILENT, PicoHealth, health_url
from cqb.services.relay import UNAVAILABLE, Outcome, Submit, present
from fake_appserver import wait_until
from fake_pico import TOKEN, FakePico, create, echo, update

SID = "cqb-" + "a" * 26
SID2 = "cqb-" + "b" * 26
SID3 = "cqb-" + "c" * 26


def arun(coro):
    return asyncio.run(coro)


@contextlib.asynccontextmanager
async def stack(script=echo, token=TOKEN, quiet=0.05, **cfg):
    fake = FakePico(script)
    await fake.start()
    async with aiohttp.ClientSession() as http:
        health = PicoHealth(f"http://127.0.0.1:{fake.port}/health", http)
        config = PicoConfig(url=fake.url, **cfg)
        gw = PicoGateway(config, token, http, health, quiet=quiet, backoff=0.01, liveness=0.05)
        try:
            yield gw, fake, health
        finally:
            await gw.close()
            await fake.stop()


class Inbox:
    def __init__(self):
        self.results = []

    async def __call__(self, result):
        self.results.append(result)

    async def wait(self, n=1, timeout=5.0):
        await wait_until(lambda: len(self.results) >= n, timeout)
        return self.results


# --- slash escaping ------------------------------------------------------------------

@pytest.mark.parametrize("text, escaped", [
    ("/clear", True), ("!clear", True), (" /clear", True), ("\n/clear", True), ("\t!clear", True),
    (" /clear", True), ("　/switch model to x", True), ("​/clear", True), ("⁠﻿ !reload", True),
    ("/CLEAR now please", True), ("/clear@some_bot", True), ("/status", True), ("/foo-unknown", True),
    ("hello /clear", False), ("hello", False), ("> /clear", False), ("User says: /clear", False),
    ("", False), ("   ", False), ("a\n/clear", False),
    ("/stop", False), ("/stop now", True), ("/STOP", True), ("!stop", True),
])
def test_escape_command_table(text, escaped):
    assert escape_command(text) == (f"User says: {text}" if escaped else text)


def test_stop_is_the_one_unescaped_command_and_is_normalized():
    assert escape_command("  /stop \n") == "/stop"


# --- reply assembly and the end-of-turn rule -----------------------------------------

def assemble(*frames):
    a = ReplyAssembler(max_chars=50)
    for f in frames:
        a.feed(f)
    return a


def test_assembler_plain_reply_and_ignored_frames():
    a = assemble({"type": "typing.start"}, {"type": "typing.stop"}, {"type": "pong"}, create("hi"))
    assert a.text == "hi" and a.has_reply and a.error is None


def test_assembler_streaming_updates_are_cumulative_and_replace():
    a = assemble(create("He", "m"), update("Hel", "m"), update("Hello", "m"))
    assert a.text == "Hello"


def test_assembler_tool_notice_discards_intermediate_text():
    a = assemble(create("let me look"), create("", "t1"))
    assert not a.has_reply
    a.feed(create("final answer", "m2"))
    assert a.text == "final answer"
    a.feed({"type": "message.create", "payload": {"kind": "tool_calls", "content": "x", "message_id": "z"}})
    assert not a.has_reply


def test_assembler_ignores_thought_placeholder_and_empty_updates():
    a = assemble(create("reasoning...", "t", kind="thought"), create("hm", "t2", thought=True),
                 create("Thinking", "p", placeholder=True), update("", "m"))
    assert not a.has_reply
    a.feed(create("answer"))
    a.feed(update("   ", "m1"))
    assert a.text == "answer"


def test_assembler_error_frame_and_size_bound():
    assert assemble({"type": "error", "payload": {"code": "empty_content"}}).error == "empty_content"
    assert assemble(create("x" * 500)).text == "x" * 50
    assert assemble({"type": "message.create", "payload": "garbage"}).has_reply is False


# --- gateway: a turn ------------------------------------------------------------------

def test_turn_roundtrip_with_token_session_and_escape():
    async def main():
        async with stack() as (gw, fake, _):
            inbox = Inbox()
            assert gw.submit(SID, "/clear", inbox) is Submit.ACCEPTED
            (r,) = await inbox.wait()
            assert (r.outcome, r.text) == (Outcome.REPLY, "echo:User says: /clear")
            assert fake.texts(SID) == ["User says: /clear"] and fake.opened == 1 and fake.auth_failures == 0
    arun(main())


def test_wrong_token_is_unavailable():
    async def main():
        async with stack(token="nope") as (gw, fake, _):
            inbox = Inbox()
            gw.submit(SID, "hi", inbox)
            (r,) = await inbox.wait()
            assert r.outcome is Outcome.UNAVAILABLE and fake.auth_failures >= 1
    arun(main())


def test_tool_turn_returns_only_the_final_message():
    async def script(sid, text):
        return [{"type": "typing.start"}, create("checking", "a"), create("", "t"), create("final", "b")]

    async def main():
        async with stack(script) as (gw, _, _):
            inbox = Inbox()
            gw.submit(SID, "q", inbox)
            assert (await inbox.wait())[0].text == "final"
    arun(main())


def test_error_frame_ends_the_turn_with_an_error():
    async def script(sid, text):
        return [{"type": "error", "payload": {"code": "unknown_type", "message": "m"}}]

    async def main():
        async with stack(script) as (gw, _, _):
            inbox = Inbox()
            gw.submit(SID, "q", inbox)
            assert (await inbox.wait())[0].outcome is Outcome.ERROR
    arun(main())


# --- serialization, queue, merging ----------------------------------------------------

def test_messages_are_serialized_never_sent_concurrently_and_answered_in_order():
    async def main():
        async with stack() as (gw, fake, _):
            fake.turn_delay = 0.15
            inbox = Inbox()
            first = gw.submit(SID, "one", inbox)
            others = [gw.submit(SID, t, inbox) for t in ("two", "three")]
            assert first is Submit.ACCEPTED and others == [Submit.QUEUED, Submit.QUEUED]
            await asyncio.sleep(0.05)
            assert fake.texts(SID) == ["one"]  # the rest waits for the reply
            results = await inbox.wait(3)
            assert [r.text for r in results] == ["echo:one", "echo:two", "echo:three"]
            assert fake.merged == 0 and fake.texts(SID) == ["one", "two", "three"]
    arun(main())


def test_without_serialization_the_fake_would_merge():
    async def main():  # proves the fake detects concurrency, so merged == 0 above means something
        async with stack() as (gw, fake, _):
            fake.turn_delay = 0.2
            async with aiohttp.ClientSession() as http:
                async with http.ws_connect(fake.url, params={"session_id": SID2}, headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
                    for t in ("a", "b"):
                        await ws.send_json({"type": "message.send", "id": t, "session_id": SID2, "payload": {"content": t}})
                    await asyncio.sleep(0.4)
            assert fake.merged == 1
    arun(main())


def test_queue_is_bounded():
    async def main():
        async with stack() as (gw, fake, _):
            fake.turn_delay = 0.05
            inbox = Inbox()
            outcomes = [gw.submit(SID, f"m{i}", inbox) for i in range(QUEUE_MAX + 3)]
            assert outcomes[0] is Submit.ACCEPTED
            # the first is taken by the worker only when it runs, so the queue holds QUEUE_MAX before refusing
            assert outcomes.count(Submit.QUEUE_FULL) == 3
            await inbox.wait(QUEUE_MAX, 10)
    arun(main())


def test_sessions_do_not_block_each_other():
    async def main():
        async with stack() as (gw, fake, _):
            fake.turn_delay = 0.1
            a, b = Inbox(), Inbox()
            gw.submit(SID, "a", a)
            gw.submit(SID2, "b", b)
            await a.wait()
            await b.wait()
            assert a.results[0].text == "echo:a" and b.results[0].text == "echo:b" and fake.opened == 2
    arun(main())


def test_invalid_or_foreign_session_ids_are_refused():
    async def main():
        async with stack() as (gw, fake, _):
            for sid in ("", "other-" + "a" * 26, "cqb-UPPER", "cqb-" + "a" * 80, "cqb- space"):
                assert gw.submit(sid, "hi", Inbox()) is Submit.UNAVAILABLE
            assert fake.opened == 0
    arun(main())


# --- /stop ---------------------------------------------------------------------------

def test_stop_cancels_a_running_turn_unescaped_and_queued_messages_still_run():
    async def main():
        async with stack() as (gw, fake, _):
            fake.turn_delay = 5.0  # a long turn: no time limit applies
            inbox = Inbox()
            gw.submit(SID, "slow work", inbox)
            await wait_until(lambda: fake.texts(SID) == ["slow work"])
            assert gw.submit(SID, "next", inbox) is Submit.QUEUED
            assert gw.submit(SID, "/stop", inbox) is Submit.STOPPED
            (r,) = await inbox.wait(1)
            assert r.outcome is Outcome.REPLY and "Task stopped" in r.text
            fake.turn_delay = 0
            await inbox.wait(2)
            assert fake.texts(SID) == ["slow work", "/stop", "next"] and inbox.results[1].text == "echo:next"
    arun(main())


def test_stop_with_nothing_running_is_an_ordinary_unescaped_message():
    async def main():
        async with stack() as (gw, fake, _):
            inbox = Inbox()
            assert gw.submit(SID, "/stop", inbox) is Submit.ACCEPTED
            assert (await inbox.wait())[0].text == "No active task to stop." and fake.texts(SID) == ["/stop"]
            assert gw.submit(SID, "/stop now", inbox) is Submit.ACCEPTED
            await inbox.wait(2)
            assert fake.texts(SID)[-1] == "User says: /stop now"
    arun(main())


# --- no time limit, optional limit ----------------------------------------------------

def test_a_slow_turn_is_not_cut_off_by_default():
    async def main():
        async with stack(quiet=0.05) as (gw, fake, _):
            fake.turn_delay = 0.4  # far longer than the quiet period: the end rule must wait for a reply
            inbox = Inbox()
            gw.submit(SID, "slow", inbox)
            assert (await inbox.wait(1, 5))[0].text == "echo:slow"
    arun(main())


def test_optional_turn_timeout_abandons_the_turn_and_sends_stop():
    async def main():
        async with stack(turn_timeout_seconds=0.3) as (gw, fake, _):
            fake.turn_delay = 5
            inbox = Inbox()
            gw.submit(SID, "never", inbox)
            (r,) = await inbox.wait(1, 5)
            assert r.outcome is Outcome.TIMEOUT and fake.texts(SID)[-1] == "/stop"
            await wait_until(lambda: gw.open_sockets == 0)  # the socket was closed with the turn
    arun(main())


# --- sockets: idle, LRU, in-flight protection, reconnect -----------------------------

def test_idle_socket_is_closed_but_a_running_turn_never_is():
    async def main():
        async with stack(idle_seconds=0.1) as (gw, fake, _):
            sweeper = asyncio.create_task(gw.run())
            try:
                inbox = Inbox()
                gw.submit(SID, "quick", inbox)
                await inbox.wait()
                assert gw.open_sockets == 1
                await wait_until(lambda: gw.open_sockets == 0 and fake.open_count == 0, 3)  # idle: closed
                fake.turn_delay = 0.35  # longer than the idle timeout: several sweeps pass meanwhile
                gw.submit(SID2, "long", inbox)
                await inbox.wait(2, 5)
                assert inbox.results[1].text == "echo:long" and fake.opened == 2  # never closed mid-turn
                # and a closed session reopens lazily
                gw.submit(SID, "again", inbox)
                await inbox.wait(3)
                assert fake.opened >= 3
            finally:
                sweeper.cancel()
    arun(main())


def test_connection_cap_closes_least_recently_used_idle_socket():
    async def main():
        async with stack(max_connections=2) as (gw, fake, _):
            inbox = Inbox()
            for sid in (SID, SID2):
                gw.submit(sid, "x", inbox)
                await inbox.wait(len(inbox.results) + 1)
            gw.submit(SID, "touch", inbox)  # SID becomes most recent, SID2 least recent
            await inbox.wait(3)
            gw.submit(SID3, "third", inbox)
            await inbox.wait(4)
            assert fake.max_open <= 2 and gw.open_sockets == 2
            assert fake.sockets.get(SID2, []) == [] and len(fake.sockets[SID]) == 1 and len(fake.sockets[SID3]) == 1
    arun(main())


def test_cap_with_every_socket_busy_reports_busy_instead_of_closing_a_turn():
    async def main():
        async with stack(max_connections=1) as (gw, fake, _):
            fake.turn_delay = 0.3
            inbox = Inbox()
            gw.submit(SID, "long", inbox)
            await wait_until(lambda: fake.texts(SID))
            gw.submit(SID2, "other", inbox)
            await inbox.wait(2, 5)
            by_text = {r.outcome for r in inbox.results}
            assert Outcome.BUSY in by_text and any(r.text == "echo:long" for r in inbox.results)
    arun(main())


def test_server_down_reports_unavailable_after_bounded_attempts():
    async def main():
        async with stack() as (gw, fake, _):
            await fake.stop()
            inbox = Inbox()
            gw.submit(SID, "hi", inbox)
            (r,) = await inbox.wait(1, 5)
            assert r.outcome is Outcome.UNAVAILABLE
            assert gw.open_sockets == 0
    arun(main())


def test_socket_lost_mid_turn_reports_unavailable_and_keeps_queued_messages():
    async def main():
        async with stack() as (gw, fake, _):
            fake.turn_delay = 5
            inbox = Inbox()
            gw.submit(SID, "lost", inbox)
            await wait_until(lambda: fake.texts(SID) == ["lost"])
            gw.submit(SID, "kept", inbox)  # queued behind the running turn
            fake.turn_delay = 0
            await fake.drop_connections()
            await inbox.wait(2, 8)
            assert inbox.results[0].outcome is Outcome.UNAVAILABLE  # the reply of "lost" is gone, the user is told
            assert inbox.results[1].text == "echo:kept"  # the queued one was not lost: new socket, same session
            # the lost one was not resent, and the orphaned turn was cancelled on the new socket first
            assert fake.texts(SID) == ["lost", "/stop", "kept"]
    arun(main())


def test_unhealthy_gateway_refuses_new_messages_and_recovers():
    async def main():
        async with stack() as (gw, fake, health):
            health.ok = False
            assert gw.submit(SID, "hi", Inbox()) is Submit.UNAVAILABLE
            runner = asyncio.create_task(gw.run())
            try:
                await wait_until(lambda: health.ok, 3)  # the probe notices the recovery by itself
                inbox = Inbox()
                assert gw.submit(SID, "hi", inbox) is Submit.ACCEPTED
                await inbox.wait()
            finally:
                runner.cancel()
    arun(main())


def test_delivery_failure_does_not_kill_the_worker():
    async def main():
        async with stack() as (gw, _, _):
            async def broken(result):
                raise RuntimeError("telegram down")

            inbox = Inbox()
            gw.submit(SID, "a", broken)
            gw.submit(SID, "b", inbox)
            assert (await inbox.wait())[0].text == "echo:b"
    arun(main())


# --- health: gateway and hook liveness ------------------------------------------------

def test_health_url_derivation():
    assert health_url("ws://127.0.0.1:18790/pico/ws") == "http://127.0.0.1:18790/health"
    assert health_url("ws://[::1]:9/pico/ws") == "http://[::1]:9/health"


def test_health_detects_dead_gateway_and_silent_hook(caplog):
    class Clock:
        now = 1000.0

        def __call__(self):
            return self.now

    async def main():
        clock = Clock()
        fake = FakePico()
        await fake.start()
        async with aiohttp.ClientSession() as http:
            h = PicoHealth(f"http://127.0.0.1:{fake.port}/health", http, beat_max_age=30, clock=clock)
            assert await h.check()  # inside the startup grace period
            clock.now += 31
            assert not await h.check() and "hook" in h.reason  # no heartbeat for too long: dead hook process
            assert "error" in {r.levelname.lower() for r in caplog.records}
            h.beat()
            assert await h.check()
            await fake.stop()
            assert not await h.check() and "not reachable" in h.reason
    caplog.set_level("INFO")
    arun(main())


# --- ask: one request on a named session with its own deadline (the Status Agent's way in) ---------

STATUS_SID = "cqb-status-1"


def test_ask_returns_the_reply_of_one_request():
    async def main():
        async with stack() as (gw, fake, _):
            r = await gw.ask(STATUS_SID, "what now", 5)
            assert (r.outcome, r.text) == (Outcome.REPLY, "echo:what now") and fake.texts(STATUS_SID) == ["what now"]
    arun(main())


def test_ask_deadline_stops_the_turn_and_reports_timeout_whatever_the_config_says():
    async def main():
        async with stack(turn_timeout_seconds=0) as (gw, fake, _):  # consultants: no limit; this call: its own
            fake.turn_delay = 5
            r = await asyncio.wait_for(gw.ask(STATUS_SID, "slow", 0.3), 4)
            assert r.outcome is Outcome.TIMEOUT
            await wait_until(lambda: fake.texts(STATUS_SID)[-1:] == ["/stop"])
            await wait_until(lambda: gw.open_sockets == 0)
    arun(main())


def test_ask_cancelled_by_the_caller_stops_the_abandoned_turn():
    async def main():
        async with stack() as (gw, fake, _):
            fake.turn_delay = 5
            task = asyncio.create_task(gw.ask(STATUS_SID, "slow", 60))
            await wait_until(lambda: fake.texts(STATUS_SID) == ["slow"])
            await asyncio.sleep(0.05)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await wait_until(lambda: fake.texts(STATUS_SID)[-1:] == ["/stop"])
    arun(main())


def test_ask_on_a_foreign_session_or_a_dead_gateway_is_unavailable():
    async def main():
        async with stack() as (gw, fake, health):
            assert (await gw.ask("other-" + "x" * 5, "hi", 1)).outcome is Outcome.UNAVAILABLE
            health.ok = False
            assert (await gw.ask(STATUS_SID, "hi", 1)).outcome is Outcome.UNAVAILABLE
            assert fake.sent == []
    arun(main())


def test_consultant_turns_still_use_the_configured_timeout_not_the_ask_timeout():
    async def main():
        async with stack(turn_timeout_seconds=0.3) as (gw, fake, _):
            fake.turn_delay = 5
            inbox = Inbox()
            gw.submit(SID, "x", inbox)
            assert (await inbox.wait(1, 5))[0].outcome is Outcome.TIMEOUT
    arun(main())


# --- the reason shown to the owner with every "unavailable" / error line ------------------------------

def shown(result):
    return present(result)


def test_reason_connection_refused_or_rejected():
    async def main():
        async with stack(token="nope") as (gw, fake, _):
            inbox = Inbox()
            gw.submit(SID, "hi", inbox)
            (r,) = await inbox.wait()
            assert (r.outcome, r.text) == (Outcome.UNAVAILABLE, CONN_FAILED)
            assert shown(r) == f"{UNAVAILABLE} ({CONN_FAILED})" and "nope" not in shown(r) and SID not in shown(r)
    arun(main())


def test_reason_socket_lost_mid_turn():
    async def main():
        async with stack() as (gw, fake, _):
            fake.turn_delay = 5
            inbox = Inbox()
            gw.submit(SID, "lost", inbox)
            await wait_until(lambda: fake.texts(SID) == ["lost"])
            await fake.drop_connections()
            (r,) = await inbox.wait(1, 8)
            assert (r.outcome, r.text) == (Outcome.UNAVAILABLE, CONN_LOST)
            assert shown(r) == "Консультант временно недоступен (соединение с PicoClaw оборвано)"
    arun(main())


def test_reason_unhealthy_gateway_and_silent_hook_and_bad_session():
    class Clock:
        now = 1000.0

        def __call__(self):
            return self.now

    async def main():
        clock = Clock()
        fake = FakePico()
        await fake.start()
        async with aiohttp.ClientSession() as http:
            health = PicoHealth(f"http://127.0.0.1:{fake.port}/health", http, beat_max_age=30, clock=clock)
            gw = PicoGateway(PicoConfig(url=fake.url), TOKEN, http, health, quiet=0.05, backoff=0.01, liveness=0.05)
            assert gw.submit("other-" + "a" * 26, "hi", Inbox()) is Submit.UNAVAILABLE
            assert gw.unavailable_reason() == BAD_SESSION and SID not in BAD_SESSION
            clock.now += 31
            assert not await health.check()  # no heartbeat: the hook process is silent
            assert gw.submit(SID, "hi", Inbox()) is Submit.UNAVAILABLE
            assert gw.unavailable_reason() == HOOK_SILENT == "хук не отвечает"
            health.beat()
            await fake.stop()
            assert not await health.check()  # the gateway itself does not answer
            assert gw.submit(SID, "hi", Inbox()) is Submit.UNAVAILABLE
            assert gw.unavailable_reason() == HEALTH_FAILING == "health-проверка PicoClaw не проходит"
            await gw.close()
    arun(main())


def test_reason_health_answering_with_an_error_status_names_the_status():
    async def main():
        from aiohttp import web
        app = web.Application()

        async def bad(request):
            return web.Response(status=503)
        app.router.add_get("/health", bad)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        async with aiohttp.ClientSession() as http:
            health = PicoHealth(f"http://127.0.0.1:{port}/health", http)
            assert not await health.check()
            assert health.user_reason == f"{HEALTH_FAILING}, ответ 503"
        await runner.cleanup()
    arun(main())


def test_reason_model_error_frame_is_shown_cut_and_redacted():
    async def script(sid, text):
        return [{"type": "error", "payload": {"code": "model_error", "message":
                 "upstream said:\n  rate limit exceeded api_key=abcdefgh12345678 " + "z " * 300}}]

    async def main():
        async with stack(script) as (gw, _, _):
            inbox = Inbox()
            gw.submit(SID, "q", inbox)
            (r,) = await inbox.wait()
            assert r.outcome is Outcome.ERROR and r.text.startswith("модель вернула ошибку: upstream said: rate limit exceeded")
            assert len(r.text) <= len("модель вернула ошибку: ") + 200 and r.text.endswith("…")
            assert "abcdefgh12345678" not in r.text and "\n" not in r.text
            assert shown(r) == f"Консультант вернул ошибку ({r.text})"
    arun(main())


def test_reason_model_error_frame_without_a_message_falls_back_to_its_code():
    async def script(sid, text):
        return [{"type": "error", "payload": {"code": "unknown_type"}}]

    async def main():
        async with stack(script) as (gw, _, _):
            inbox = Inbox()
            gw.submit(SID, "q", inbox)
            assert (await inbox.wait())[0].text == "модель вернула ошибку: unknown_type"
    arun(main())


def test_reason_connection_cap_and_exchange_failure(monkeypatch):
    async def main():
        async with stack(max_connections=1) as (gw, fake, _):
            fake.turn_delay = 0.3
            inbox = Inbox()
            gw.submit(SID, "long", inbox)
            await wait_until(lambda: fake.texts(SID))
            gw.submit(SID2, "other", inbox)
            await inbox.wait(2, 5)
            busy = next(r for r in inbox.results if r.outcome is Outcome.BUSY)
            assert busy.text == TOO_MANY_SOCKETS and shown(busy) == f"Консультант занят, повторите чуть позже ({TOO_MANY_SOCKETS})"
        async with stack() as (gw, fake, _):
            async def boom(*a, **k):
                raise RuntimeError("private /srv/x")
            monkeypatch.setattr(gw, "_await_reply", boom)
            inbox = Inbox()
            gw.submit(SID, "hi", inbox)
            (r,) = await inbox.wait()
            assert (r.outcome, r.text) == (Outcome.ERROR, EXCHANGE_FAILED) and "private" not in shown(r)
    arun(main())
