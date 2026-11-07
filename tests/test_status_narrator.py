"""Status Agent slots: pool, scope binding for the duration of a call, deadline, and isolation from the consultant."""

import asyncio

import pytest

from cqb.pico.status_narrator import (
    LINE_MAX, STATUS_PROMPT, SUMMARY_LINES, SUMMARY_MAX, PicoStatusNarrator, SlotPool, clean_summary, slot_names,
)
from cqb.services.relay import DialogResult, Outcome
from cqb.services.scope import SessionScopes

CONSULTANT = "cqb-" + "a" * 26


def run(coro):
    return asyncio.run(coro)


class Dialog:
    """Records every ask; answers like the scenario says. Looks the scope up while the call is running."""

    def __init__(self, scopes, reply="Корень работает.", outcome=Outcome.REPLY, hold=None, fail=None):
        self.scopes, self.reply, self.outcome, self.hold, self.fail = scopes, reply, outcome, hold, fail
        self.asked: list[tuple[str, str, float]] = []
        self.scope_seen: list[tuple[str, object]] = []

    async def ask(self, session_id, text, timeout):
        self.asked.append((session_id, text, timeout))
        self.scope_seen.append((session_id, self.scopes.lookup(session_id)))
        if self.hold is not None:
            await self.hold.wait()
        if self.fail is not None:
            raise self.fail
        return DialogResult(self.outcome, self.reply)


def make(slots=2, **kw):
    scopes = SessionScopes(lambda sid: ("s1", "consultant-root") if sid == CONSULTANT else None)
    dialog = Dialog(scopes, **kw)
    return PicoStatusNarrator(dialog, scopes, "cqb-", slots), dialog, scopes


def test_a_summary_goes_through_a_slot_with_the_fixed_prompt_and_the_root_scope():
    async def main():
        narrator, dialog, scopes = make()
        assert await narrator.summarize("s1", "root-7", 30) == "Корень работает."
        [(sid, prompt, timeout)] = dialog.asked
        assert sid == "cqb-status-1" and 0 < timeout <= 30
        assert "root-7" in prompt and "{root_id}" not in prompt
        for tool in ("list_agents", "get_recent_events", "get_thread_overview", "get_questions"):
            assert tool in prompt
        for rule in ("Russian", "at most 10", "не подтверждено", "Never quote secrets", "untrusted"):
            assert rule in prompt
        assert dialog.scope_seen == [("cqb-status-1", ("s1", "root-7"))]  # bound during the call
        assert scopes.lookup("cqb-status-1") is None  # and gone afterwards
    run(main())


def test_the_consultants_session_is_never_touched_and_keeps_its_own_scope():
    async def main():
        narrator, dialog, scopes = make(slots=3)
        for root in ("r1", "r2", "r3", "r4"):
            await narrator.summarize("s1", root, 30)
        assert {sid for sid, *_ in dialog.asked} <= set(slot_names("cqb-", 3))
        assert all(sid != CONSULTANT and not sid.startswith("cqb-a") for sid, *_ in dialog.asked)
        assert scopes.lookup(CONSULTANT) == ("s1", "consultant-root")  # the consultant's mapping is not affected
    run(main())


def test_slots_are_reused_one_root_at_a_time_and_a_busy_pool_waits_for_a_release():
    async def main():
        hold = asyncio.Event()
        narrator, dialog, scopes = make(slots=2, hold=hold)
        first = asyncio.create_task(narrator.summarize("s1", "r1", 30))
        second = asyncio.create_task(narrator.summarize("s1", "r2", 30))
        third = asyncio.create_task(narrator.summarize("s1", "r3", 30))
        for _ in range(20):
            await asyncio.sleep(0)
        assert sorted(sid for sid, *_ in dialog.asked) == ["cqb-status-1", "cqb-status-2"]  # the third waits
        assert scopes.lookup("cqb-status-1") == ("s1", "r1") and scopes.lookup("cqb-status-2") == ("s1", "r2")
        assert not third.done()
        hold.set()
        assert await asyncio.gather(first, second, third) == ["Корень работает."] * 3
        assert len(dialog.asked) == 3
        # each slot only ever served one root at a time: the scope seen inside each call is that call's root
        seen = {sid: root for sid, (_, root) in dialog.scope_seen[:2]}
        assert set(seen.values()) == {"r1", "r2"}
        assert scopes.lookup("cqb-status-1") is None and scopes.lookup("cqb-status-2") is None
    run(main())


def test_exhausted_pool_gives_up_at_the_deadline_without_asking():
    async def main():
        hold = asyncio.Event()
        narrator, dialog, _ = make(slots=1, hold=hold)
        busy = asyncio.create_task(narrator.summarize("s1", "r1", 30))
        await asyncio.sleep(0.01)
        assert await narrator.summarize("s1", "r2", 0.05) is None  # no slot before its deadline
        assert len(dialog.asked) == 1
        hold.set()
        await busy
        assert await narrator.summarize("s1", "r3", 30) == "Корень работает."  # the slot came back
    run(main())


@pytest.mark.parametrize("outcome", [Outcome.TIMEOUT, Outcome.UNAVAILABLE, Outcome.BUSY, Outcome.ERROR])
def test_a_failed_turn_is_no_summary_and_frees_slot_and_scope(outcome):
    async def main():
        narrator, dialog, scopes = make(slots=1, outcome=outcome)
        assert await narrator.summarize("s1", "r1", 30) is None
        assert scopes.lookup("cqb-status-1") is None
        assert await narrator.summarize("s1", "r2", 30) is None and len(dialog.asked) == 2  # the slot was released
    run(main())


def test_an_exception_in_the_dialog_releases_slot_and_scope():
    async def main():
        narrator, dialog, scopes = make(slots=1, fail=RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            await narrator.summarize("s1", "r1", 30)
        assert scopes.lookup("cqb-status-1") is None
        dialog.fail = None
        assert await narrator.summarize("s1", "r2", 30) == "Корень работает."
    run(main())


def test_cancellation_releases_slot_and_scope():
    async def main():
        hold = asyncio.Event()
        narrator, dialog, scopes = make(slots=1, hold=hold)
        task = asyncio.create_task(narrator.summarize("s1", "r1", 30))
        await asyncio.sleep(0.01)
        assert scopes.lookup("cqb-status-1") == ("s1", "r1")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert scopes.lookup("cqb-status-1") is None
        hold.set()
        assert await narrator.summarize("s1", "r2", 30) == "Корень работает."
    run(main())


def test_the_remaining_deadline_is_what_the_dialog_gets():
    async def main():
        narrator, dialog, _ = make(slots=1)
        await narrator.summarize("s1", "r1", 100)
        assert 99 < dialog.asked[0][2] <= 100
    run(main())


def test_slot_pool_basics():
    async def main():
        pool = SlotPool(["a", "b"])
        assert (await pool.acquire(), await pool.acquire()) == ("a", "b")
        waiting = asyncio.create_task(pool.acquire())
        await asyncio.sleep(0)
        assert not waiting.done()
        pool.release("b")
        assert await waiting == "b"
    run(main())
    assert slot_names("cqb-", 2) == ["cqb-status-1", "cqb-status-2"]


def test_the_reply_is_redacted_bounded_and_cleaned_before_it_can_enter_the_card():
    token = "ghp_" + "b" * 36
    cleaned = clean_summary(f"Корень правит файлы.\n\n  Ключ: {token}  \nпароль password=hunter2hunter2\n")
    assert token not in cleaned and "hunter2" not in cleaned and cleaned.splitlines()[0] == "Корень правит файлы."
    assert "\n\n" not in cleaned
    many = clean_summary("\n".join(f"строка {i}" for i in range(40)))
    assert len(many.splitlines()) == SUMMARY_LINES
    wide = clean_summary("я" * 5000)
    assert len(wide) == LINE_MAX
    big = clean_summary("\n".join("слово " * 40 for _ in range(12)))
    assert len(big) <= SUMMARY_MAX and big.endswith("…")
    assert clean_summary("") is None and clean_summary(" \n\t\n") is None
    assert clean_summary("a\x00b\x07c") == "abc"


def test_empty_reply_is_no_summary():
    async def main():
        narrator, *_ = make(reply="   ")
        assert await narrator.summarize("s1", "r1", 30) is None
    run(main())


def test_the_prompt_is_english_and_names_only_the_four_status_tools():
    assert "{root_id}" in STATUS_PROMPT and "read_event" not in STATUS_PROMPT
    assert STATUS_PROMPT.isascii() is False  # only the quoted Russian phrase is non-ASCII
    assert STATUS_PROMPT.count("не подтверждено") == 1


def test_scope_table_prefers_a_bound_slot_and_falls_back_to_consultants():
    scopes = SessionScopes(lambda sid: ("s1", "c-root") if sid == "c" else None)
    assert scopes.lookup("c") == ("s1", "c-root") and scopes.lookup("slot") is None
    scopes.bind("slot", "s1", "r9")
    assert scopes.lookup("slot") == ("s1", "r9")
    scopes.unbind("slot")
    scopes.unbind("slot")  # idempotent
    assert scopes.lookup("slot") is None
