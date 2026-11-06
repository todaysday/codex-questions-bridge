import re
import tomllib
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any


class ConfigError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class CodexConfig:
    socket: str
    source_id: str
    poll_interval_seconds: int
    recency_days: int


@dataclass(frozen=True, slots=True)
class DbConfig:
    path: str


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    chat_id: int
    control_topic_name: str
    inbox_topic_name: str
    allowed_user_ids: tuple[int, ...]
    registry_refresh_seconds: int


@dataclass(frozen=True, slots=True)
class Config:
    codex: CodexConfig
    db: DbConfig
    telegram: TelegramConfig


TOKEN_ENV = "CQB_TELEGRAM_TOKEN"
_SOURCE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def load_config(path: str) -> Config:
    """Secrets (tokens) never belong in this file; the bot token comes from CQB_TELEGRAM_TOKEN."""
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc.strerror}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    return parse_config(raw)


def parse_config(raw: dict[str, Any]) -> Config:
    unknown = set(raw) - {"codex", "db", "telegram"}
    if unknown:
        raise ConfigError(f"unknown section(s): {', '.join(sorted(unknown))}")
    codex = _section(raw, "codex", {"socket", "source_id", "poll_interval_seconds", "recency_days"})
    db = _section(raw, "db", {"path"})
    tg = _section(raw, "telegram", {
        "chat_id", "control_topic_name", "inbox_topic_name", "allowed_user_ids",
        "registry_refresh_seconds",
    })
    return Config(
        codex=CodexConfig(
            socket=_text(codex, "codex", "socket"),
            source_id=_source_id(codex.get("source_id", "local")),
            poll_interval_seconds=_int(codex, "codex", "poll_interval_seconds", 30, 5, 3600),
            recency_days=_int(codex, "codex", "recency_days", 30, 1, 3650),
        ),
        db=DbConfig(path=_text(db, "db", "path")),
        telegram=TelegramConfig(
            chat_id=_chat_id(tg.get("chat_id")),
            control_topic_name=_topic_name(tg, "control_topic_name", "📋 Треды Codex"),
            inbox_topic_name=_topic_name(tg, "inbox_topic_name", "📥 Inbox"),
            allowed_user_ids=_user_ids(tg.get("allowed_user_ids")),
            registry_refresh_seconds=_int(tg, "telegram", "registry_refresh_seconds", 30, 5, 3600),
        ),
    )


def read_token(env: Mapping[str, str]) -> str:
    token = env.get(TOKEN_ENV, "").strip()
    if not token:
        raise ConfigError(f"environment variable {TOKEN_ENV} (Telegram bot token) is not set")
    return token


def _section(raw: dict[str, Any], name: str, allowed: set[str]) -> dict[str, Any]:
    sec = raw.get(name)
    if not isinstance(sec, dict):
        raise ConfigError(f"missing section [{name}]")
    for key in sec:
        if re.search(r"token|secret|password|key", key, re.I):
            raise ConfigError(f"[{name}] {key}: secrets must come from the environment, not the file")
        if key not in allowed:
            raise ConfigError(f"[{name}] unknown key {key}")
    return sec


def _text(sec: dict[str, Any], name: str, key: str) -> str:
    v = sec.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ConfigError(f"[{name}] {key} is required and must be a non-empty string")
    return v


def _int(sec: dict[str, Any], name: str, key: str, default: int, lo: int, hi: int) -> int:
    v = sec.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ConfigError(f"[{name}] {key} must be an integer in {lo}..{hi}")
    return v


def _source_id(v: Any) -> str:
    if not isinstance(v, str) or not _SOURCE_ID.match(v):
        raise ConfigError("[codex] source_id must match [A-Za-z0-9._-]{1,64}")
    return v


def _chat_id(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v == 0:
        raise ConfigError("[telegram] chat_id is required and must be a non-zero integer")
    return v


def _topic_name(sec: dict[str, Any], key: str, default: str) -> str:
    v = sec.get(key, default)
    if not isinstance(v, str) or not 1 <= len(v.strip()) <= 128:
        raise ConfigError(f"[telegram] {key} must be a string of 1..128 characters")
    return v.strip()


def _user_ids(v: Any) -> tuple[int, ...]:
    ok = isinstance(v, list) and 0 < len(v) <= 50
    if not ok or not all(isinstance(i, int) and not isinstance(i, bool) and i > 0 for i in v):
        raise ConfigError("[telegram] allowed_user_ids must be a non-empty list (max 50) of positive user ids")
    return tuple(v)
