"""The user message that answers agent-message questions, in the exact form the Codex app writes.

Observed on the real daemon (userMessage items that follow an agentMessage with `questions`):

    <send_user_message_question_reply>\\n
    [{"question": <title>, "answer": <chosen option or free text>,
      "questionItemId": "[\\"request_user_input_async\\",\\"<agentMessage item id>\\",<question index>]"}, ...]\\n
    </send_user_message_question_reply>

The array is compact UTF-8 JSON (no spaces, no \\\\u escapes). `questionItemId` is itself a compact JSON
string: a fixed tag, the id of the agentMessage item and the zero-based index of the question. The app
sends one entry per answered question, and one message may carry entries of several questions.
"""

import json
from collections.abc import Mapping

from cqb.domain.models import AgentQuestionItem

TAG = "send_user_message_question_reply"
ITEM_TAG = "request_user_input_async"


def build_reply(item_id: str, items: tuple[AgentQuestionItem, ...], answers: Mapping[int, str]) -> str:
    entries = [
        {
            "question": item.title,
            "answer": answers[index],
            "questionItemId": _compact([ITEM_TAG, item_id, index]),
        }
        for index, item in enumerate(items)
    ]
    return f"<{TAG}>\n{_compact(entries)}\n</{TAG}>"


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
