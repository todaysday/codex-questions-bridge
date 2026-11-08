import dataclasses
import re
import time

import pytest

from cqb.domain.enums import ArchiveState, ConsultantState as S, QuestionLifecycle as L, VerificationSource
from cqb.domain.models import Question, QuestionItem, QuestionOption, Thread
from cqb.services.registry import RegistryPage, RegistryRow
from cqb.telegram.fmt import CALLBACK_LIMIT, TEXT_LIMIT, callback_key, topic_link
from cqb.telegram.card_view import SECRET_NOTICE, render_card
from cqb.telegram.keys import ActionKeys
from cqb.telegram.models import ActionSpec
from cqb.telegram.registry_view import registry_doc, render_registry
from cqb.dao.actions import ActionDao

CHAT = -1001234567890
NOW = 1_800_000_000


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def row(n=1, state=None, topic=None, pending=0, name="FoxProfile — User Access", updated=NOW - 300, working=False):
    t = Thread("s1", f"thread-{n:04d}", f"thread-{n:04d}", None, "cli", name, "/w", updated,
               ArchiveState.ACTIVE, 1, 1)
    return RegistryRow(t, state, topic, pending, working)


def page(rows, p=0, nxt=False, archived=False):
    return RegistryPage(tuple(rows), p, nxt, archived, 30)


def buttons(view):
    return [b for r in view.keyboard for b in r]


def test_deep_link_format():
    assert topic_link(-1001234567890, 17) == "https://t.me/c/1234567890/17"
    assert topic_link(-100999, 3) == "https://t.me/c/999/3"
    assert topic_link(-12345, 3) is None and topic_link(123, 3) is None


def reg(rows, p=0, pages=1, archived=False, **counts):
    nxt = p + 1 < pages
    return RegistryPage(tuple(rows), p, nxt, archived, 30, pages, counts.get("pending", 0),
                        counts.get("working", 0), counts.get("total", len(rows)))


def test_registry_page_text_and_buttons():
    view = render_registry(reg([
        row(1, None, None, 0),
        row(2, S.OPEN, 55, 2, "AVIP — Instagram Scraper", NOW - 5 * 86400),
        row(3, S.ERROR), row(4, S.ARCHIVED, 56),
    ], pages=2, total=24), CHAT, NOW)
    lines = view.text.splitlines()
    assert lines[0] == "📋 Треды Codex · стр. 1/2" and lines[1] == "всего 24"  # the page is in the text, no clock time
    assert not re.search(r"\d\d:\d\d", view.text) and "обновлено" not in view.text and "консультант" not in view.text
    assert "1. FoxProfile — User Access" in view.text and "   📁 w · 5м" in view.text
    assert "2. ❓ AVIP" in view.text and "   📁 w · 5д" in view.text
    assert "3. ⚠️ FoxProfile" in view.text and "4. 🔒 FoxProfile" in view.text  # only the exceptional consultant states
    assert "✅" not in view.text and "⚙️" not in view.text
    texts = [b.text for row_ in view.keyboard[:-2] for b in row_]
    assert texts == ["1. ➕ Создать консультанта", "2. 💬 Открыть", "3. ➕ Создать консультанта", "4. 💬 Открыть"]
    opens = {b.text: b.url for b in buttons(view) if b.url}
    assert opens["2. 💬 Открыть"] == "https://t.me/c/1234567890/55"
    assert [b.text for b in view.keyboard[-2]] == ["▶️"]  # the counter is rich only
    assert [b.text for b in view.keyboard[-1]] == ["📦 Архивные"]  # no refresh button, no recent/earlier toggle
    create = next(b for b in buttons(view) if b.text.startswith("1. ➕"))
    assert create.action == ActionSpec("create", "thread-0001")
    assert not any(b.action and b.action.action in ("status", "refresh") for b in buttons(view))


def test_registry_row_markers_question_working_and_both():
    view = render_registry(reg([
        row(1, pending=1), row(2, working=True), row(3, pending=2, working=True), row(4),
    ], pending=2, working=2), CHAT, NOW)
    lines = [ln for ln in view.text.splitlines() if ln[:2] in ("1.", "2.", "3.", "4.")]
    assert lines[0].startswith("1. ❓ ") and "🟢" not in lines[0]
    assert lines[1].startswith("2. 🟢 ") and "❓" not in lines[1]
    assert lines[2].startswith("3. ❓🟢 ")
    assert lines[3].startswith("4. FoxProfile") and "❓" not in lines[3] and "🟢" not in lines[3]
    assert "❓ 2 · 🟢 2 · всего 4" in view.text


def test_archived_row_without_consultant_has_no_buttons_and_no_empty_keyboard_row():
    view = render_registry(reg([row(1)], archived=True), CHAT, NOW)
    assert len(view.keyboard) == 1 and all(len(r) > 0 for r in view.keyboard)  # only the toggles


def test_registry_pagination_and_the_archive_toggle():
    middle = render_registry(reg([row(1)], p=2, pages=5), CHAT, NOW)
    nav = {b.text: b.action for b in middle.keyboard[-2]}
    assert nav["◀️"].payload == "1:0" and nav["▶️"].payload == "3:0" and set(nav) == {"◀️", "▶️"}  # the counter is rich only
    toggles = {b.text: b.action for b in middle.keyboard[-1]}
    assert list(toggles) == ["📦 Архивные"] and toggles["📦 Архивные"].payload == "0:1"
    arch = render_registry(reg([row(1)], archived=True), CHAT, NOW)
    assert arch.text.startswith("📦 Архивные треды Codex") and "ранее" not in arch.text
    assert not any("Создать" in b.text for b in buttons(arch))
    assert [(b.text, b.action.payload) for b in arch.keyboard[-1]] == [("📋 Активные", "0:0")]
    assert "Нет тредов" in render_registry(reg([]), CHAT, NOW).text
    only = render_registry(reg([row(1)]), CHAT, NOW)  # one page: no page row at all
    assert [[b.text for b in r] for r in only.keyboard][-2:] == [["1. ➕ Создать консультанта"], ["📦 Архивные"]]


def test_registry_limits_with_hostile_names():
    rows = [row(i, S.OPEN, 5, 99, "Ж" * 5000) for i in range(25)]
    view = render_registry(reg(rows, pages=2), CHAT, NOW)
    assert len(view.text) <= TEXT_LIMIT
    assert all(len(b.text) <= 64 for b in buttons(view))


def test_fingerprint_follows_content_and_coarse_ages_not_the_clock():
    a = registry_doc(reg([row(1)]), CHAT, NOW)
    assert a.fingerprint() == registry_doc(reg([row(1)]), CHAT, NOW + 60).fingerprint()  # same age bucket
    assert a.fingerprint() != registry_doc(reg([row(1)]), CHAT, NOW + 3600).fingerprint()  # 5м became 1ч
    assert a.fingerprint() != registry_doc(reg([row(1, S.OPEN, 5)]), CHAT, NOW).fingerprint()


def test_relative_ages():
    from cqb.telegram.fmt import age
    assert [age(NOW - d, NOW) for d in (0, 299, 300, 899, 3599, 3600, 7300, 86399, 86400, 172799, 172800, 3 * 86400 + 5)] == [
        "сейчас", "сейчас", "5м", "10м", "55м", "1ч", "2ч", "23ч", "вчера", "вчера", "2д", "3д"]
    assert age(NOW + 100, NOW) == "сейчас"


def test_callback_data_is_short_deterministic_and_stored(conn):
    keys = ActionKeys(ActionDao(conn), "s1", clock=lambda: NOW)
    rows = [row(i, None, None, 0, "n" * 300) for i in range(1, 6)]
    bound = keys.bind(render_registry(reg(rows, p=9, pages=10), CHAT, NOW).keyboard)
    data = [b.callback_data for r in bound for b in r if b.action]
    assert data and all(d.startswith("a:") and len(d.encode()) <= CALLBACK_LIMIT for d in data)
    assert bound == keys.bind(render_registry(reg(rows, p=9, pages=10), CHAT, NOW).keyboard)
    long_root = ActionSpec("create", "019f" + "e" * 400, "p" * 400)
    assert len(callback_key(long_root, "s" * 64).encode()) <= CALLBACK_LIMIT
    assert conn.execute("SELECT COUNT(*) FROM telegram_actions").fetchone()[0] == len(set(data))


def q(**kw):
    base = dict(
        question_id="q_1", source_id="s1", root_thread_id="thread-0001", origin_thread_id="thread-0001",
        turn_id="u1", tool_use_id="i1", request_id="7",
        items=(QuestionItem("a", "Storage", "Where store profile settings?", False,
                            (QuestionOption("Existing table", "reuse"), QuestionOption("Separate table"))),),
        is_secret=False, is_blocking=True, lifecycle=L.PENDING_CONFIRMED,
        verification_source=VerificationSource.HOST_RPC, staged=None, delivered_to_codex=False,
        created_at=NOW, updated_at=NOW, display_no=3,
    )
    return Question(**{**base, **kw})


def test_card_pending_has_option_buttons():
    v = render_card(q(), "FoxProfile")
    assert v.header == "❓ Q3 · FoxProfile"  # no clock time: Telegram shows the message time
    assert "request_user_input" not in v.text and "q_1" not in v.text and "pending" not in v.text  # no internal noise
    assert "Storage: Where store profile settings?" in v.text and "A. Existing table — reuse" in v.text
    assert "B. Separate table" in v.text  # the option text is in the body, in full
    texts = [b.text for b in buttons(v)]
    assert texts == ["A", "B"]  # buttons carry only the short label
    assert "обсужда" in v.text  # a plain reply is a discussion, not an answer
    assert buttons(v)[0].action == ActionSpec("opt", "thread-0001", "q_1:0")


def test_card_other_offers_reply_hint():
    v = render_card(q(items=(QuestionItem("a", "", "Name?", True, ()),)), "T")
    assert [b.text for b in buttons(v)] == ["✍️ Ответить текстом"]
    assert "следующий ответ" not in v.text and "консультанту" in v.text
    armed = render_card(q(items=(QuestionItem("a", "", "Name?", True, ()),)), "T", armed=True)
    assert [b.text for b in buttons(armed)] == ["↩️ Отмена"] and buttons(armed)[0].action.action == "unarm"
    assert "следующий ответ на это сообщение уйдёт в Codex" in armed.text


def test_card_secret_has_no_text_and_no_buttons():
    secret = q(is_secret=True, items=())
    v = render_card(secret, "FoxProfile")
    assert v.body == SECRET_NOTICE and v.keyboard == ()
    assert "Where" not in v.text and "q_1" not in v.text


def test_card_staged_asks_for_confirmation():
    v = render_card(q(lifecycle=L.DECISION_STAGED, staged={"a": ("Separate table",)}), "T")
    assert "Выбрано: Separate table" in v.text and "Отправить этот ответ в Codex?" in v.text
    assert [b.text for b in buttons(v)][:2] == ["✅ Отправить в Codex", "↩️ Отмена"]


def test_card_delivered_only_after_resolved():
    sent = q(lifecycle=L.DECISION_STAGED, staged={"a": ("A",)}, delivered_to_codex=True)
    v = render_card(sent, "T")
    assert "ждём подтверждения" in v.text and "Доставлено" not in v.text
    assert not any(b.action and b.action.action in ("opt", "send") for b in buttons(v))
    done = render_card(dataclasses.replace(sent, lifecycle=L.RESOLVED_OBSERVED), "T")
    assert "✅ Доставлено" in done.text and done.keyboard == ()


def test_card_answered_elsewhere_and_stale():
    elsewhere = render_card(q(lifecycle=L.RESOLVED_OBSERVED), "T")
    assert "отвечено в другом месте" in elsewhere.text and elsewhere.keyboard == ()
    assert "Доставлено" not in elsewhere.text
    stale = render_card(q(lifecycle=L.EXPIRED_OR_STALE), "T")
    assert "устарел" in stale.text and stale.keyboard == ()


def test_card_without_live_request_offers_no_answer_buttons():
    v = render_card(q(lifecycle=L.CAPTURED), "T")
    assert not any(b.action and b.action.action == "opt" for b in buttons(v))
    assert "ответ пока невозможен" in v.text


def test_card_multiple_items_is_read_only():
    items = tuple(QuestionItem(str(i), "", f"Q{i}?", False, (QuestionOption("A"),)) for i in range(2))
    v = render_card(q(items=items), "T")
    assert "ответьте в Codex" in v.text and not any(b.action.action == "opt" for b in buttons(v) if b.action)


def test_card_limits_with_huge_question():
    opts = tuple(QuestionOption("L" * 500, "D" * 2000) for _ in range(26))
    huge = q(items=(QuestionItem("a", "H" * 900, "T" * 7000, True, opts),))
    v = render_card(huge, "N" * 900)
    assert "…" not in v.body and "T" * 7000 in v.text and v.text.count("L" * 500) == 26 and v.text.count("D" * 2000) == 26
    assert all(len(b.text) <= 2 for b in buttons(v) if b.action.action == "opt")  # buttons never carry option text
    assert len([b for b in buttons(v) if b.action.action == "opt"]) == 26 and len(v.header) < 120  # a long name is clipped


def test_card_header_has_a_state_tag_only_when_not_pending():
    assert render_card(q(), "T").header == "❓ Q3 · T"
    done = render_card(q(lifecycle=L.RESOLVED_OBSERVED, delivered_to_codex=True), "T").header
    elsewhere = render_card(q(lifecycle=L.RESOLVED_OBSERVED), "T").header
    stale = render_card(q(lifecycle=L.EXPIRED_OR_STALE), "T").header
    assert done.endswith("· ✅ отвечено") and elsewhere.endswith("· отвечено в другом месте") and stale.endswith("· устарел")
    assert render_card(q(lifecycle=L.DECISION_STAGED, staged={"a": ("A",)}), "T").header == "❓ Q3 · T"


def test_card_spacing_classic_and_rich_one_empty_line_after_the_header_and_before_the_options():
    from cqb.telegram.card_view import card_doc
    from cqb.telegram.render import to_html
    v = render_card(q(), "FoxProfile")
    assert v.header == "❓ Q3 · FoxProfile"
    assert v.body.startswith("Storage: Where store profile settings?\n\nA. Existing table — reuse\nB. Separate table\n\n")  # options tight
    assert v.text.startswith("❓ Q3 · FoxProfile\n\nStorage:")
    keyed = card_doc(q(), "FoxProfile").mapped(lambda b: dataclasses.replace(b, callback_data="k:1") if b.action else b)
    html = to_html(keyed)
    assert html.count("<p>&nbsp;</p>") == 2 and "</p><p>&nbsp;</p><p>Storage:" in html and "settings?</p><p>&nbsp;</p><p>A. " in html
    assert "</tg-button></p><p>B. Separate" in html  # no blank between options
    secret = render_card(q(is_secret=True, items=()), "FoxProfile")
    assert secret.text == f"❓ Q3 · FoxProfile\n\n{SECRET_NOTICE}"
    two = tuple(QuestionItem(str(i), "", f"Q{i}?", False, (QuestionOption("X"),)) for i in range(2))
    assert render_card(q(items=two), "T").body.startswith("1. Q0?\n\nA. X\n\n2. Q1?\n\nA. X")
    items = (QuestionItem("a", "", "Name?", True, ()),)
    assert render_card(q(items=items), "T").body.startswith("Name?\n\n")  # the status line follows, still one empty line
    bound = card_doc(q(items=items), "T").mapped(lambda b: dataclasses.replace(b, callback_data="k:1") if b.action else b)
    assert to_html(bound).count("<p>&nbsp;</p>") == 1  # only after the header: no options, no second empty line
