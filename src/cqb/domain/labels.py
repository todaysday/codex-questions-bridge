"""Display labels of questions: `Q3`, one number per question (an agent message with several questions gives
several numbers)."""

import re

_NUMBER = re.compile(r"(?i)\s*q?\s*(\d{1,9})(?:\.\d{1,3})?\s*")
_MENTION = re.compile(r"(?i)(?<!\w)(?:q|вопрос\w*)\s*[#№.]?\s*(\d{1,6})(?!\d)")


def question_label(number: int) -> str:
    return f"Q{number}"


def parse_number(value: object) -> int | None:
    """`Q3`, `q3`, `3`, `Q3.2` or the integer 3 -> 3; anything else (bools included) -> None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and (m := _NUMBER.fullmatch(value)):
        return int(m.group(1)) or None
    return None


def mentioned_numbers(text: str) -> list[int]:
    """Q-numbers the owner wrote in a message (`Q3`, `q3`, `вопрос 3`), in order of appearance."""
    return [n for m in _MENTION.finditer(text) if (n := int(m.group(1))) > 0]
