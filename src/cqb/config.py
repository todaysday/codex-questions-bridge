import re
import tomllib
from dataclasses import dataclass, field
from urllib.parse import urlsplit
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
    codex_home: str = "/root/.codex"  # rollout files are read (token_count only) under its sessions dirs


@dataclass(frozen=True, slots=True)
class DbConfig:
    path: str


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    chat_id: int
    control_topic_name: str
    allowed_user_ids: tuple[int, ...]
    registry_refresh_seconds: int


DEFAULT_PICO_URL = "ws://127.0.0.1:18790/pico/ws"
DEFAULT_SESSION_PREFIX = "cqb-"


@dataclass(frozen=True, slots=True)
class PicoConfig:
    """Bridge side of PicoClaw's pico channel. Present in the file = consultants are enabled."""

    url: str = DEFAULT_PICO_URL
    session_prefix: str = DEFAULT_SESSION_PREFIX  # must match ConsultantService.session_key; the gateway refuses other ids
    idle_seconds: int = 120  # an idle session's WebSocket is closed after this long
    max_connections: int = 50  # hard cap, below PicoClaw's channel limit of 100
    turn_timeout_seconds: int = 0  # 0 = no limit: only the end-of-turn rule or a dropped socket ends a turn


@dataclass(frozen=True, slots=True)
class McpConfig:
    port: int = 18791  # always bound to 127.0.0.1


@dataclass(frozen=True, slots=True)
class ConsultantTokens:
    pico: str = field(repr=False)
    mcp: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class Config:
    codex: CodexConfig
    db: DbConfig
    telegram: TelegramConfig
    pico: PicoConfig | None = None
    mcp: McpConfig = McpConfig()


TOKEN_ENV = "CQB_TELEGRAM_TOKEN"
PICO_TOKEN_ENV = "CQB_PICO_TOKEN"
MCP_TOKEN_ENV = "CQB_MCP_TOKEN"
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
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
    unknown = set(raw) - {"codex", "db", "telegram", "pico", "mcp"}
    if unknown:
        raise ConfigError(f"unknown section(s): {', '.join(sorted(unknown))}")
    codex = _section(raw, "codex", {"socket", "source_id", "poll_interval_seconds", "recency_days", "codex_home"})
    db = _section(raw, "db", {"path"})
    tg = _section(raw, "telegram", {
        "chat_id", "control_topic_name", "allowed_user_ids",
        "registry_refresh_seconds",
    })
    return Config(
        pico=_pico(raw["pico"]) if "pico" in raw else None,
        mcp=McpConfig(port=_int(_section(raw, "mcp", {"port"}), "mcp", "port", 18791, 1024, 65535)) if "mcp" in raw
        else McpConfig(),
        codex=CodexConfig(
            socket=_text(codex, "codex", "socket"),
            source_id=_source_id(codex.get("source_id", "local")),
            poll_interval_seconds=_int(codex, "codex", "poll_interval_seconds", 30, 5, 3600),
            recency_days=_int(codex, "codex", "recency_days", 30, 1, 3650),
            codex_home=_abs_path(codex.get("codex_home", "/root/.codex")),
        ),
        db=DbConfig(path=_text(db, "db", "path")),
        telegram=TelegramConfig(
            chat_id=_chat_id(tg.get("chat_id")),
            control_topic_name=_topic_name(tg, "control_topic_name", "📋 Треды Codex"),
            allowed_user_ids=_user_ids(tg.get("allowed_user_ids")),
            registry_refresh_seconds=_int(tg, "telegram", "registry_refresh_seconds", 5, 5, 3600),
        ),
    )


def read_consultant_tokens(env: Mapping[str, str]) -> ConsultantTokens:
    """Only called when [pico] is configured: a half-configured consultant chat must not start."""
    pico, mcp = env.get(PICO_TOKEN_ENV, "").strip(), env.get(MCP_TOKEN_ENV, "").strip()
    for name, value in ((PICO_TOKEN_ENV, pico), (MCP_TOKEN_ENV, mcp)):
        if not value:
            raise ConfigError(f"environment variable {name} is not set but [pico] is configured")
    if pico == mcp:
        raise ConfigError(f"{PICO_TOKEN_ENV} and {MCP_TOKEN_ENV} must be different secrets")
    return ConsultantTokens(pico, mcp)


def read_token(env: Mapping[str, str]) -> str:
    token = env.get(TOKEN_ENV, "").strip()
    if not token:
        raise ConfigError(f"environment variable {TOKEN_ENV} (Telegram bot token) is not set")
    return token


def _pico(raw: Any) -> PicoConfig:
    sec = _section({"pico": raw}, "pico", {"url", "session_prefix", "idle_seconds", "max_connections",
                                            "turn_timeout_seconds"})
    url = sec.get("url", DEFAULT_PICO_URL)
    parts = urlsplit(url) if isinstance(url, str) else None
    if parts is None or parts.scheme != "ws" or parts.hostname not in _LOOPBACK or parts.port is None:
        raise ConfigError("[pico] url must be ws://127.0.0.1:<port>/... (loopback only: the token is sent in clear)")
    prefix = sec.get("session_prefix", DEFAULT_SESSION_PREFIX)
    if not isinstance(prefix, str) or not re.fullmatch(r"[a-z0-9-]{2,16}", prefix):
        raise ConfigError("[pico] session_prefix must be 2..16 characters of [a-z0-9-] (PicoClaw lowercases ids)")
    return PicoConfig(
        url=url, session_prefix=prefix,
        idle_seconds=_int(sec, "pico", "idle_seconds", 120, 10, 3600),
        max_connections=_int(sec, "pico", "max_connections", 50, 1, 90),
        turn_timeout_seconds=_int(sec, "pico", "turn_timeout_seconds", 0, 0, 86400),
    )


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


def _abs_path(v: Any) -> str:
    if not isinstance(v, str) or not v.startswith("/"):
        raise ConfigError("[codex] codex_home must be an absolute path")
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
