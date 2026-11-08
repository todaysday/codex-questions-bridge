"""Q numbers, the focus question of a consultant topic and the context line the consultant sees."""

import asyncio

from cqb.dao.agent_questions import AgentQuestionDao
from cqb.dao.consultants import ConsultantDao
from cqb.dao.questions import QuestionDao
from cqb.domain.models import AgentQuestion, Question, QuestionItem, QuestionOption
from cqb.domain.enums import AgentQuestionState, QuestionLifecycle as L, VerificationSource
from cqb.domain.labels import mentioned_numbers, parse_number, question_label
from cqb.services.agent_questions import AgentQuestionService
from cqb.services.focus import QuestionFocus
from cqb.services.question_context import PENDING_LINE_MAX, context_line, discussion_prefix, pick_focus
from cqb.services.questions import QuestionService
from cqb.services.relay import ConsultantRelay
from tg_stack import agent_item, make_stack, thread
from test_relay import Dialog


def run(coro):
    return asyncio.run(coro)


def item(text, *options, header="", other=False):
    return QuestionItem("i", header, text, other, tuple(QuestionOption(o) for o in options))


def rq(no, title="Which?", options=("A", "B"), *, secret=False, lifecycle=L.PENDING_CONFIRMED, items=None):
    return Question(
        question_id=f"q_{no}", source_id="s1", root_thread_id="r1", origin_thread_id="r1", turn_id="u", tool_use_id=f"t{no}",
        request_id="1", items=() if secret else (items or (item(title, *options),)), is_secret=secret, is_blocking=True,
        lifecycle=lifecycle, verification_source=VerificationSource.HOST_RPC, staged=None, delivered_to_codex=False,
        created_at=no, updated_at=no, display_no=no,
    )


def aq(no, title="Which?", options=("A", "B"), state=AgentQuestionState.PENDING):
    return AgentQuestion(
        agent_question_id=f"a_{no}", source_id="s1", thread_id="r1", root_thread_id="r1", turn_id="u", item_id=f"i{no}",
        index=0, title=title, options=tuple(options), answerable=True, state=state, staged=None, sent_at=None,
        created_at=no, updated_at=no, display_no=no,
    )


# --- labels ----------------------------------------------------------------------------------

def test_labels_and_number_parsing():
    assert question_label(3) == "Q3"
    for ok in ("Q3", "q3", " 3 ", "Q 3", "Q3.2", 3):
        assert parse_number(ok) == 3, ok
    for bad in ("Q", "abc", "", "Q0", 0, -2, True, None, 2.5, "3x", "Q3Q"):
        assert parse_number(bad) is None, bad
    assert mentioned_numbers("про Q3 и q 4, вопрос 5, Вопросы 6, iq7, q.8, Q3.1, Q") == [3, 4, 5, 6, 8, 3]
    assert mentioned_numbers("nothing, version 3, q-3") == []


# --- the context line ------------------------------------------------------------------------

def test_line_has_the_focus_with_options_and_the_others_shortened():
    qs = [rq(3, "Where to store the settings?", ("Existing table", "Separate table")),
          aq(4, "Run the control pass now? " + "word " * 30), aq(5, "Third")]
    line = context_line(qs, focus_no=3)
    assert line == ('[Context: focus Q3 "Where to store the settings?" (options: Existing table, Separate table; explicit); '
                    'other pending: Q5 "Third", Q4 "' + ("Run the control pass now? " + "word " * 30)[:59] + '…"]')
    assert len(line) <= PENDING_LINE_MAX and "\n" not in line


def test_default_focus_single_is_explicit_several_is_the_newest_marked_assumed():
    one = context_line([aq(2, "Only one", ())])
    assert one == '[Context: focus Q2 "Only one" (explicit)]'  # no options, no others
    many = context_line([rq(1, "Old"), aq(2, "Middle"), rq(7, "Newest", ("X",))])
    assert many.startswith('[Context: focus Q7 "Newest" (options: X; assumed); other pending: Q2 "Middle", Q1 "Old"')
    assert context_line([rq(1, "Old"), rq(2, "New")], focus_no=99).count("assumed") == 1  # a stale pointer is ignored


def test_the_focus_moves_on_when_its_question_is_no_longer_pending():
    qs = [rq(1, "Old"), rq(2, "Focused", lifecycle=L.RESOLVED_OBSERVED), aq(3, "Mid", state=AgentQuestionState.STALE),
          rq(4, "New")]
    focus, explicit, others = pick_focus(qs, focus_no=2)
    assert (focus.display_no, explicit, [q.display_no for q in others]) == (4, False, [1])
    assert context_line([rq(2, "Done", lifecycle=L.EXPIRED_OR_STALE)], 2) == ""  # nothing pending: no line


def test_no_line_when_nothing_is_pending():
    assert context_line([]) == "" and pick_focus([], 3) is None
    assert context_line([aq(1, state=AgentQuestionState.ANSWERED)]) == ""


def test_secret_questions_never_show_text_in_the_line():
    secret_focus = context_line([rq(5, secret=True), rq(4, "Public")], focus_no=5)
    assert secret_focus == '[Context: focus Q5 (secret input, not shown; explicit); other pending: Q4 "Public"]'
    secret_other = context_line([rq(5, secret=True), rq(6, "Public")])
    assert secret_other.endswith("other pending: Q5 (secret input, not shown)]")
    stored = rq(5, "LEAKED title", ("LEAKED",), secret=True, items=(item("LEAKED title", "LEAKED"),))
    assert "LEAKED" not in context_line([stored]) and "LEAKED" not in discussion_prefix(stored)


def test_line_caps_questions_options_and_total_size_and_redacts():
    huge = tuple(f"option number {i} " + "w" * 40 for i in range(10))
    qs = [rq(n, "title " * 60, huge) for n in range(1, 9)]
    line = context_line(qs)
    assert len(line) <= PENDING_LINE_MAX and line.startswith("[Context: focus Q8 ") and line.endswith("]")
    assert line.count("option number") <= 4  # at most 4 options of the focus
    assert line.count(' "') <= 4  # the focus plus at most 3 others
    tight = context_line([rq(1, "t " * 100, ("o " * 60,) * 4), rq(2, "u " * 100)], focus_no=1)
    assert len(tight) <= PENDING_LINE_MAX and "Q2" in tight
    leaked = context_line([rq(1, "use api_key=abcdefgh12345678 now", ("Bearer abcdefgh12345678",))])
    assert "abcdefgh12345678" not in leaked and "(REDACTED)" in leaked  # the marker keeps its meaning, the brackets become parentheses
    bracket = context_line([rq(1, 'say "hi" [now]')])
    assert bracket == "[Context: focus Q1 \"say 'hi' (now)\" (options: A, B; explicit)]"
    several = context_line([rq(3, items=(item("First?", "Yes"), item("Second?")))])
    assert '1. First? / 2. Second?' in several and "options" not in several  # several items (one request): titles only


def test_the_reply_prefix_is_bounded_secret_safe_and_notes_closed_questions():
    assert discussion_prefix(rq(3, "Pick", ("A", "B"))) == "[Context: the user replies about question Q3 (Pick; options: A, B)] "
    assert discussion_prefix(rq(3, "Pick", lifecycle=L.RESOLVED_OBSERVED)).startswith(
        "[Context: the user replies about question Q3 (already answered) (Pick; options: A, B)] ")
    assert discussion_prefix(rq(3, secret=True)) == "[Context: the user replies about question Q3 (secret input, details are not shown)] "
    long = discussion_prefix(aq(9, "t" * 500, tuple(f"opt{i}" for i in range(9))))
    assert len(long) < 450 and long.count("opt") - 1 == 6  # "options:" is the extra match


# --- numbers through the services -------------------------------------------------------------

def test_numbers_are_stable_across_restarts_and_resolution_and_cover_both_kinds(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox"), thread("r2")) as st:
            await st.open_consultant("r1")
            await st.ask("r1", "i1", request_id=1)
            await st.ask("r1", "i2", request_id=2)
            st.agent_client.items["r1"] = [agent_item("m1", [{"title": "Agent one?", "options": ["Y", "N"]}])]
            await st.agent.scan_thread("r1")
            await st.ask("r2", "j1", request_id=9)  # another thread counts on its own
            await st.settle()
            numbers = {r[0]: r[1] for r in conn.execute("SELECT tool_use_id, display_no FROM questions")}
            assert numbers == {"i1": 1, "i2": 2, "j1": 1}
            assert st.agent.open_questions("r1")[0].display_no == 3
            header = st.fake.messages[st.card_of(st.qid_of("i2"))]["text"].splitlines()[0]
            assert header == "❓ Q2 · Fox"
            assert st.fake.messages[st.agent.open_questions("r1")[0].card_message_id]["text"].startswith("❓ Q3 · Fox")

            await st.resolve("r1", 1)  # Q1 resolves: its number is not handed out again
            await st.ask("r1", "i3", request_id=3)
            assert st.question_dao.get(st.qid_of("i3")).display_no == 4

            # a restart: fresh services on the same database keep counting and keep the numbers
            again = QuestionService(st.client, QuestionDao(conn), st.threads, "s1")
            assert [q.display_no for q in sorted(again.open_questions("r1"), key=lambda q: q.display_no)] == [2, 4]
            await st.ask("r1", "i4", request_id=4)
            assert st.question_dao.get(st.qid_of("i4")).display_no == 5
            await st.ask("r1", "i4", request_id=4)  # a replay of the same question keeps its number
            assert st.question_dao.get(st.qid_of("i4")).display_no == 5
            st.agent_client.items["r1"].append(agent_item("m2", [{"title": "Agent two?", "options": None}]))
            await st.agent.scan_thread("r1")
            assert max(q.display_no for q in st.agent.open_questions("r1")) == 6
    run(main())


def test_each_question_of_an_agent_message_has_its_own_number_and_questions_lists_everything_in_order(conn):
    async def main():
        async with make_stack(conn, thread("r1", name="Fox")) as st:
            topic = await st.open_consultant("r1")
            await st.ask("r1", "i1", request_id=1, items=(item("First?", "Yes"), item("Second?", "No")))  # one request, Q1
            st.agent_client.items["r1"] = [agent_item("m1", [{"title": "A1?", "options": ["Y"]}, {"title": "A2?", "options": None}])]
            await st.agent.scan_thread("r1")
            await st.settle()
            card = st.fake.messages[st.card_of(st.qid_of("i1"))]["text"]
            assert "1. First?" in card and "2. Second?" in card and "Q1.1" not in card  # several items of ONE request
            agent = sorted(st.agent.open_questions("r1"), key=lambda q: q.display_no)
            assert [(q.display_no, q.index, q.title) for q in agent] == [(2, 0, "A1?"), (3, 1, "A2?")]
            texts = [st.fake.messages[q.card_message_id]["text"] for q in agent]
            assert texts[0].startswith("❓ Q2 · Fox") and "A1?" in texts[0] and "A2?" not in texts[0]
            assert texts[1].startswith("❓ Q3 · Fox") and "A2?" in texts[1] and "A1?" not in texts[1]
            old = [st.card_of(st.qid_of("i1")), *(q.card_message_id for q in agent)]
            st.fake.push_message("/questions", topic)
            await st.pump()
            assert all(st.fake.messages[m].get("deleted") for m in old)  # the old cards are gone, fresh ones follow in order
            fresh = [m for m in st.fake.live_messages(topic) if m["text"].startswith("❓ Q")]
            assert [m["text"].split("\n")[0] for m in fresh] == ["❓ Q1 · Fox", "❓ Q2 · Fox", "❓ Q3 · Fox"]
    run(main())


# --- the focus through the router, the relay and the database -------------------------------------

async def chat(st, text, topic, **kw):
    st.fake.push_message(text, topic, **kw)
    await st.pump()


def line_of(dialog, n=-1):
    return dialog.calls[n][1].splitlines()[0]


async def wired(st, root="r1"):
    dialog = Dialog(results=())
    st.router._relay = ConsultantRelay(dialog, st.tg_topics, typing_seconds=0.01, context=st.focus)
    topic = await st.open_consultant(root)
    return dialog, topic


def test_default_focus_one_pending_then_several_assumed_then_no_line(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            dialog, topic = await wired(st)
            await chat(st, "hello", topic)
            assert dialog.calls[-1][1] == "hello"  # nothing pending: no line at all
            await st.ask("r1", "i1", request_id=1, items=(item("First one?", "A"),))
            await chat(st, "what is this?", topic)
            assert line_of(dialog) == '[Context: focus Q1 "First one?" (options: A; explicit)]'
            await st.ask("r1", "i2", request_id=2, items=(item("Second one?", "B"),))
            await chat(st, "and this?", topic)
            assert line_of(dialog) == ('[Context: focus Q2 "Second one?" (options: B; assumed); other pending: Q1 "First one?"]')
            assert dialog.calls[-1][1].endswith("\nand this?")
            await st.resolve("r1", 1)
            await st.resolve("r1", 2)
            await chat(st, "done?", topic)
            assert dialog.calls[-1][1] == "done?"
    run(main())


def test_focus_is_set_by_a_reply_a_q_mention_and_a_button_and_moves_when_it_resolves(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            dialog, topic = await wired(st)
            for n in (1, 2, 3):
                await st.ask("r1", f"i{n}", request_id=n, items=(item(f"Question {n}?", "Yes", "No"),))
            await st.settle()
            q = {n: st.qid_of(f"i{n}") for n in (1, 2, 3)}
            await chat(st, "hm", topic)
            assert "focus Q3" in line_of(dialog) and "assumed" in line_of(dialog)

            # a reply to a card points at it; the reply itself carries the card prefix (one line, no duplicate)
            await chat(st, "what does this mean?", topic, reply_to=st.card_of(q[1]))
            assert dialog.calls[-1][1].startswith("[Context: the user replies about question Q1 (Question 1?")
            assert dialog.calls[-1][1].count("[Context") == 1
            await chat(st, "and the risks?", topic)  # follow-up: no reply needed, the focus is kept
            assert line_of(dialog).startswith('[Context: focus Q1 "Question 1?" (options: Yes, No; explicit); other pending: Q3')

            # a mention updates the focus first, then the line is built
            await chat(st, "а вопрос 2 про что?", topic)
            assert line_of(dialog).startswith('[Context: focus Q2 "Question 2?"') and "explicit" in line_of(dialog)
            await chat(st, "compare q3 and Q1", topic)  # the first mention that is pending wins
            assert line_of(dialog).startswith("[Context: focus Q3 ")
            await chat(st, "Q9 and Q77 do not exist", topic)
            assert line_of(dialog).startswith("[Context: focus Q3 ")  # unknown numbers change nothing
            await chat(st, "/clear Q1", topic)  # commands never move the focus
            assert st.consultant_dao.get("s1", "r1").focus_no == 3

            # a button press points at its card
            st.fake.push_callback(st.fake.button(st.card_of(q[2]), "A")["callback_data"], st.card_of(q[2]))
            await st.pump()
            assert st.consultant_dao.get("s1", "r1").focus_no == 2

            # the focus resolves: the newest remaining pending question takes over, as an assumption
            await st.resolve("r1", 2)
            await chat(st, "ok then", topic)
            assert line_of(dialog).startswith('[Context: focus Q3 "Question 3?"') and "assumed" in line_of(dialog)
            await st.resolve("r1", 3)
            await chat(st, "and now?", topic)
            assert line_of(dialog).startswith('[Context: focus Q1 "Question 1?"') and "other pending" not in line_of(dialog)
    run(main())


def test_text_button_points_the_focus_and_a_closed_or_foreign_card_does_not(conn):
    async def main():
        async with make_stack(conn, thread("r1"), thread("r2")) as st:
            dialog, topic = await wired(st)
            await st.open_consultant("r2")
            await st.ask("r1", "i1", request_id=1, items=(item("Mine A?", "A", other=True),))
            await st.ask("r1", "i2", request_id=2, items=(item("Mine B?", "B"),))
            await st.ask("r2", "j1", request_id=3, items=(item("Foreign?", "F"),))
            await st.settle()
            q1, foreign = st.qid_of("i1"), st.qid_of("j1")
            st.fake.push_callback(st.fake.button(st.card_of(q1), "Ответить текстом")["callback_data"], st.card_of(q1))
            await st.pump()
            assert st.consultant_dao.get("s1", "r1").focus_no == 1
            await chat(st, "reply to a foreign card", topic, reply_to=st.card_of(foreign))
            assert st.consultant_dao.get("s1", "r1").focus_no == 1 and "Foreign" not in dialog.calls[-1][1]
            await st.resolve("r1", 1)
            await chat(st, "reply to the closed card", topic, reply_to=st.card_of(q1))
            assert dialog.calls[-1][1].startswith("[Context: the user replies about question Q1 (already answered)")
            assert st.consultant_dao.get("s1", "r1").focus_no == 1  # stored, but ignored: its question is closed
            await chat(st, "next", topic)
            assert line_of(dialog).startswith('[Context: focus Q2 "Mine B?"')
    run(main())


def test_focus_survives_a_restart_and_secret_cards_stay_hidden(conn):
    async def main():
        async with make_stack(conn, thread("r1")) as st:
            dialog, topic = await wired(st)
            await st.ask("r1", "i1", request_id=1, items=(item("Visible?", "A"),))
            await st.ask("r1", "i2", request_id=2, secret=True)
            await st.ask("r1", "i3", request_id=3, items=(item("Newest?", "N"),))
            await st.settle()
            await chat(st, "про вопрос 2", topic)  # the secret one becomes the focus, without any text
            assert line_of(dialog).startswith("[Context: focus Q2 (secret input, not shown; explicit); other pending: Q3 ")
            assert "hunter" not in repr(dialog.calls)
            await chat(st, "now Q1", topic)
            # restart: new objects on the same database give the same line
            fresh = QuestionFocus(
                ConsultantDao(conn), QuestionService(st.client, QuestionDao(conn), st.threads, "s1"),
                AgentQuestionService(st.agent_client, AgentQuestionDao(conn), st.threads, "s1"), "s1")
            assert fresh.line("r1") == st.focus.line("r1")
            assert fresh.line("r1").startswith('[Context: focus Q1 "Visible?" (options: A; explicit)')
            assert fresh.line("unknown-root") == ""
    run(main())
