"""Binds button actions to short one-time callback ids stored in telegram_actions."""

import dataclasses
import time
from collections.abc import Callable

from cqb.dao.actions import ActionDao
from cqb.domain.models import TelegramAction
from cqb.telegram.fmt import CALLBACK_LIMIT, callback_key
from cqb.telegram.models import ActionSpec, Keyboard

ACTION_TTL = 86400
# Mutations are used up by a press; navigation and info may be pressed repeatedly until expiry.
ONE_TIME = frozenset({"create", "opt", "send", "cancel", "aopt", "asend", "acancel"})


class ActionKeys:
    def __init__(self, actions: ActionDao, source_id: str, clock: Callable[[], float] = time.time) -> None:
        self._actions = actions
        self._source = source_id
        self._clock = clock

    def bind(self, keyboard: Keyboard) -> Keyboard:
        """Store each button's action and put its callback id into the button."""
        now = int(self._clock())
        rows = []
        for row in keyboard:
            bound = []
            for b in row:
                if b.action is None:
                    bound.append(b)
                    continue
                key = self._store(b.action, now)
                bound.append(dataclasses.replace(b, callback_data=key))
            rows.append(tuple(bound))
        return tuple(rows)

    def resolve(self, key: str) -> TelegramAction | None:
        """Known, unexpired and (for mutations) unused; consumes one-time actions."""
        now = int(self._clock())
        action = self._actions.get(key, now)
        if action is None or action.source_id != self._source:
            return None
        if action.action in ONE_TIME:
            return self._actions.consume(key, now)
        return action

    def purge_expired(self) -> int:
        return self._actions.delete_expired(int(self._clock()))

    def _store(self, spec: ActionSpec, now: int) -> str:
        key = callback_key(spec, self._source)
        assert len(key.encode()) <= CALLBACK_LIMIT
        self._actions.put(
            TelegramAction(key, spec.action, self._source, spec.root, spec.payload, now + ACTION_TTL)
        )
        return key
