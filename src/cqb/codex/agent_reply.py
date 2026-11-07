"""The user message that answers agent-message questions, in the exact form the Codex app writes.

Observed on the real daemon (userMessage items that follow an agentMessage with `questions`):

    <send_user_message_question_reply>\\n
    [{"question": <title>, "answer": <chosen option or free text>,
      "questionItemId": "[\\"request_user_input_async\\",\\"<agentMessage item id>\\",<question index>]"}, ...]\\n
    </send_user_message_question_reply>

The array is compact UTF-8 JSON (no spaces, no \\\\u escapes). `questionItemId` is itself a compact JSON
string: a fixed tag, the id of the agentMessage item and the zero-based index of the question. The app
sends one message per answered question (a message may carry entries of several); the Bridge does the same.
"""

import json
import re

TAG = "send_user_message_question_reply"
ITEM_TAG = "request_user_input_async"
_BLOCK = re.compile(rf"<{TAG}>\s*(.*?)\s*</{TAG}>", re.DOTALL)


def build_reply(item_id: str, index: int, title: str, answer: str) -> str:
    """The message that answers ONE question, as the app writes it (one entry)."""
    entry = {"question": title, "answer": answer, "questionItemId": _compact([ITEM_TAG, item_id, index])}
    return f"<{TAG}>\n{_compact([entry])}\n</{TAG}>"


def answered_questions(user_message_text: str) -> set[tuple[str, int]] | None:
    """(agentMessage item id, question index) pairs a user message answers; None when it names none.

    A message that names none (plain text, or a reply block that cannot be read) answers every question before it."""
    found: set[tuple[str, int]] = set()
    for block in _BLOCK.findall(user_message_text):
        try:
            entries = json.loads(block)
            for entry in entries:
                tag, item_id, index = json.loads(entry["questionItemId"])
                if tag == ITEM_TAG and isinstance(item_id, str) and isinstance(index, int) and not isinstance(index, bool):
                    found.add((item_id, index))
        except (ValueError, TypeError, KeyError):
            continue
    return found or None


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
