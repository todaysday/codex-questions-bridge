"""Card message ids stored with a question row: the first message and, for a long card, its continuations."""


def extra_ids_to_text(ids: list[int]) -> str | None:
    return ",".join(map(str, ids)) or None


def extra_ids_from_text(text: str | None) -> tuple[int, ...]:
    return tuple(int(x) for x in text.split(",")) if text else ()


# SQL condition: `?` is a message id that is the first message or one of the continuations of a card
CARD_MATCH = "(card_message_id=? OR instr(',' || COALESCE(card_extra_ids, '') || ',', ',' || ? || ',') > 0)"
