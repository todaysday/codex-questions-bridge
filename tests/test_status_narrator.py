"""Status Agent slots: pool, scope binding for the duration of a call, deadline, and isolation from the consultant."""

import asyncio

import pytest

from cqb.domain.status import NEXT_MAX, PROGRESS_MAX, StatusSummary
from cqb.pico.status_narrator import STATUS_PROMPT, PicoStatusNarrator, SlotPool, parse_summary, slot_names
from cqb.services.relay import DialogResult, Outcome
from cqb.services.scope import SessionScopes

CONSULTANT = "cqb-" + "a" * 26
WORKS = StatusSummary("Корень работает.")  # what the default reply parses to


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
        assert await narrator.summarize("s1", "root-7", 30) == WORKS
        [(sid, prompt, timeout)] = dialog.asked
        assert sid == "cqb-status-1" and 0 < timeout <= 30
        assert "root-7" in prompt and "{root_id}" not in prompt
        for tool in ("list_agents", "get_recent_events", "get_thread_overview", "get_questions"):
            assert tool in prompt
        for rule in ("Russian", "EXACTLY two", "В процессе:", "Дальше:", "900", "не подтверждено", "Never quote secrets",
                     "untrusted", "no agent names"):
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
        assert await asyncio.gather(first, second, third) == [WORKS] * 3
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
        assert await narrator.summarize("s1", "r3", 30) == WORKS  # the slot came back
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
        assert await narrator.summarize("s1", "r2", 30) == WORKS
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
        assert await narrator.summarize("s1", "r2", 30) == WORKS
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


WELL_FORMED = "В процессе: Правит тесты статуса, часть уже зелёная.\nДальше: прогнать e2e и обновить README."


def test_a_well_formed_reply_is_split_into_progress_and_next_steps():
    assert parse_summary(WELL_FORMED) == StatusSummary(
        "Правит тесты статуса, часть уже зелёная.", "прогнать e2e и обновить README.")
    multi = parse_summary("В процессе:\nпервая\nвторая\nтретья\nчетвёртая\nпятая\nшестая\nДальше:\n- раз\n- два\n- три\n- четыре\n- пять")
    assert multi == StatusSummary("первая\nвторая\nтретья\nчетвёртая\nпятая", "раз\nдва\nтри\nчетыре")  # five and four lines at most
    short = parse_summary("В процессе:\nпервая\nвторая\nтретья\nДальше:\n- раз\n- два")
    assert short == StatusSummary("первая\nвторая\nтретья", "раз\nдва")
    assert parse_summary("**В процессе:** идёт сборка\n**Дальше:** тесты") == StatusSummary("идёт сборка", "тесты")
    assert parse_summary("в процессе - не метка\nДАЛЬШЕ : x") == StatusSummary("в процессе - не метка", "x")


@pytest.mark.parametrize("nxt", ["—", "-", "–", " — ", "нет", "", "не подтверждено"])
def test_no_next_steps_means_none(nxt):
    assert parse_summary(f"В процессе: идёт сборка\nДальше: {nxt}") == StatusSummary("идёт сборка", None)
    assert parse_summary("В процессе: идёт сборка") == StatusSummary("идёт сборка", None)  # the part is missing


def test_a_malformed_reply_is_all_progress_up_to_the_limit_and_has_no_next_steps():
    plain = parse_summary("Корень правит файлы.\nСубагент читает логи.")
    assert plain == StatusSummary("Корень правит файлы.\nСубагент читает логи.", None)
    long = parse_summary("слово " * 200)
    assert len(long.progress) == PROGRESS_MAX == 650 and long.progress.endswith("…") and long.next_steps is None
    only_next = parse_summary("Дальше: раскатать")
    assert only_next == StatusSummary("—", "раскатать")
    assert parse_summary("Дальше: —") is None  # nothing at all


def test_each_part_has_its_own_cap_and_the_longer_summary_is_kept():
    out = parse_summary("В процессе: " + "а" * 800 + "\nДальше: " + "б" * 800)
    assert (PROGRESS_MAX, NEXT_MAX) == (650, 400)
    assert len(out.next_steps) == NEXT_MAX and out.next_steps.endswith("…")
    assert len(out.progress) == PROGRESS_MAX and out.progress.endswith("…")
    detailed = "В процессе:\n" + "\n".join(f"Шаг {i}: " + "ж" * 100 for i in range(5)) + "\nДальше:\n" + "\n".join(
        f"Пункт {i}: " + "з" * 60 for i in range(4))
    out = parse_summary(detailed)
    assert out.progress.count("\n") == 4 and out.next_steps.count("\n") == 3 and not out.progress.endswith("…")


def test_empty_replies_are_no_summary():
    for text in ("", "   ", " \n\t\n", "\x00\x07", "В процессе:", "- \n•"):
        assert parse_summary(text) is None, repr(text)


def test_the_reply_is_redacted_before_it_can_enter_the_card():
    token = "ghp_" + "b" * 36
    out = parse_summary(f"В процессе: ключ {token} найден\nДальше: пароль password=hunter2hunter2 сменить")
    assert token not in out.progress and "hunter2" not in out.next_steps
    assert "\x00" not in parse_summary("a\x00b\x07c").progress
    assert parse_summary("a\x00b\x07c").progress == "abc"


def test_empty_reply_is_no_summary():
    async def main():
        narrator, *_ = make(reply="   ")
        assert await narrator.summarize("s1", "r1", 30) is None
    run(main())


def test_the_prompt_is_english_names_only_the_four_status_tools_and_asks_for_two_short_parts():
    assert "{root_id}" in STATUS_PROMPT and "read_event" not in STATUS_PROMPT
    assert STATUS_PROMPT.count("не подтверждено") == 1  # the labels and this phrase are the only Russian
    assert "no breakdown per agent" in STATUS_PROMPT.lower() and "single dash" in STATUS_PROMPT
    assert "working or idle" in STATUS_PROMPT and "at most 10" not in STATUS_PROMPT


def test_scope_table_prefers_a_bound_slot_and_falls_back_to_consultants():
    scopes = SessionScopes(lambda sid: ("s1", "c-root") if sid == "c" else None)
    assert scopes.lookup("c") == ("s1", "c-root") and scopes.lookup("slot") is None
    scopes.bind("slot", "s1", "r9")
    assert scopes.lookup("slot") == ("s1", "r9")
    scopes.unbind("slot")
    scopes.unbind("slot")  # idempotent
    assert scopes.lookup("slot") is None
