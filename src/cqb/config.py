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
    project_files: bool = False  # read_project_file / list_project_dir: not listed and refused while False
    project_file_max_bytes: int = 16384  # output cap of one read or listing, JSON-encoded bytes
    project_dir_max_entries: int = 200
    project_file_deny_extra: tuple[str, ...] = ()  # more deny patterns; the built-in table is never reduced
    web_search: bool = False  # web_search tool (OmniRoute search endpoint): not listed and refused while False
    web_search_max_results: int = 5  # default and cap of results per call (1..10)


@dataclass(frozen=True, slots=True)
class StatusConfig:
    """The /status snapshot. Independent of the consultant chat, which has no time limit."""

    timeout_seconds: int = 300  # whole collect, Status Agent summary included; then the card goes out without it
    slots: int = 2  # Status Agent sessions cqb-status-1..N (needs a dispatch rule each, see deploy/picoclaw)


@dataclass(frozen=True, slots=True)
class QuestionsConfig:
    """Agent-message questions are found by reading thread items; this is how often the sweep reads."""

    agent_scan_seconds: int = 20  # 5..600: period of the sweep over working and recently updated roots


@dataclass(frozen=True, slots=True)
class WebSearchEndpoint:
    """Where web_search goes: the full URL of a POST {query, max_results} -> {results} search API, and its key."""

    url: str
    key: str = field(repr=False)


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
    status: StatusConfig = StatusConfig()
    questions: QuestionsConfig = QuestionsConfig()


TOKEN_ENV = "CQB_TELEGRAM_TOKEN"
PICO_TOKEN_ENV = "CQB_PICO_TOKEN"
MCP_TOKEN_ENV = "CQB_MCP_TOKEN"
WEB_SEARCH_URL_ENV = "CQB_WEB_SEARCH_URL"
WEB_SEARCH_KEY_ENV = "CQB_WEB_SEARCH_KEY"
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
    unknown = set(raw) - {"codex", "db", "telegram", "pico", "mcp", "status", "questions"}
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
        status=_status(raw["status"]) if "status" in raw else StatusConfig(),
        questions=_questions(raw["questions"]) if "questions" in raw else QuestionsConfig(),
        mcp=_mcp(raw["mcp"]) if "mcp" in raw else McpConfig(),
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


def read_web_search_endpoint(env: Mapping[str, str]) -> WebSearchEndpoint:
    """Only called when [mcp] web_search is on. The URL is operator input, so it is checked, never echoed."""
    url, key = env.get(WEB_SEARCH_URL_ENV, "").strip(), env.get(WEB_SEARCH_KEY_ENV, "").strip()
    for name, value in ((WEB_SEARCH_URL_ENV, url), (WEB_SEARCH_KEY_ENV, key)):
        if not value:
            raise ConfigError(f"environment variable {name} is not set but [mcp] web_search is on")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ConfigError(f"{WEB_SEARCH_URL_ENV} must be an http(s) URL without credentials")
    return WebSearchEndpoint(url, key)


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


def _mcp(raw: Any) -> McpConfig:
    sec = _section({"mcp": raw}, "mcp", {"port", "project_files", "project_file_max_bytes", "project_dir_max_entries",
                                          "project_file_deny_extra", "web_search", "web_search_max_results"})
    flag = sec.get("project_files", False)
    if not isinstance(flag, bool):
        raise ConfigError("[mcp] project_files must be true or false")
    web = sec.get("web_search", False)
    if not isinstance(web, bool):
        raise ConfigError("[mcp] web_search must be true or false")
    extra = sec.get("project_file_deny_extra", [])
    ok = isinstance(extra, list) and len(extra) <= 100 and all(
        isinstance(p, str) and 1 <= len(p) <= 100 and "/" not in p and "\0" not in p for p in extra)
    if not ok:
        raise ConfigError("[mcp] project_file_deny_extra must be a list (max 100) of name patterns without `/`")
    return McpConfig(
        port=_int(sec, "mcp", "port", 18791, 1024, 65535), project_files=flag,
        project_file_max_bytes=_int(sec, "mcp", "project_file_max_bytes", 16384, 1024, 65536),
        project_dir_max_entries=_int(sec, "mcp", "project_dir_max_entries", 200, 1, 1000),
        project_file_deny_extra=tuple(extra), web_search=web,
        web_search_max_results=_int(sec, "mcp", "web_search_max_results", 5, 1, 10),
    )


def _status(raw: Any) -> StatusConfig:
    sec = _section({"status": raw}, "status", {"timeout_seconds", "slots"})
    return StatusConfig(
        timeout_seconds=_int(sec, "status", "timeout_seconds", 300, 30, 3600),
        slots=_int(sec, "status", "slots", 2, 1, 8),
    )


def _questions(raw: Any) -> QuestionsConfig:
    sec = _section({"questions": raw}, "questions", {"agent_scan_seconds"})
    return QuestionsConfig(agent_scan_seconds=_int(sec, "questions", "agent_scan_seconds", 20, 5, 600))


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
