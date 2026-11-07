import pytest

from cqb.config import ConfigError, load_config, parse_config, read_token

TG = {"chat_id": -1001234567890, "allowed_user_ids": [123]}
OK = {"codex": {"socket": "/tmp/s"}, "db": {"path": "/tmp/x.db"}, "telegram": TG}


def test_defaults():
    c = parse_config(OK)
    assert (c.codex.source_id, c.codex.poll_interval_seconds, c.codex.recency_days) == ("local", 30, 30)
    assert c.telegram.chat_id == -1001234567890 and c.telegram.allowed_user_ids == (123,)
    assert c.telegram.control_topic_name == "📋 Треды Codex"
    assert c.telegram.registry_refresh_seconds == 5
    assert not hasattr(c.telegram, "inbox_topic_name")
    assert c.codex.codex_home == "/root/.codex"


def test_codex_home_is_configurable_and_must_be_absolute():
    assert parse_config({**OK, "codex": {"socket": "s", "codex_home": "/srv/codex"}}).codex.codex_home == "/srv/codex"
    for bad in ("relative/dir", "", 5):
        with pytest.raises(ConfigError, match="codex_home"):
            parse_config({**OK, "codex": {"socket": "s", "codex_home": bad}})


@pytest.mark.parametrize(
    "raw, msg",
    [
        ({"db": {"path": "x"}}, "missing section"),
        ({"codex": {}, "db": {"path": "x"}}, "socket is required"),
        ({"codex": {"socket": "s", "poll_interval_seconds": 1}, "db": {"path": "x"}}, "poll_interval_seconds"),
        ({"codex": {"socket": "s", "recency_days": "7"}, "db": {"path": "x"}}, "recency_days"),
        ({"codex": {"socket": "s", "source_id": "bad id!"}, "db": {"path": "x"}}, "source_id"),
        ({"codex": {"socket": "s", "typo": 1}, "db": {"path": "x"}}, "unknown key"),
        ({"codex": {"socket": "s", "api_token": "x"}, "db": {"path": "x"}}, "environment"),
        ({"codex": {"socket": "s"}, "db": {"path": "x"}, "extra": {}}, "unknown section"),
    ],
)
def test_invalid(raw, msg):
    with pytest.raises(ConfigError, match=msg):
        parse_config({"telegram": TG, **raw})


@pytest.mark.parametrize(
    "tg, msg",
    [
        (None, "missing section"),
        ({"allowed_user_ids": [1]}, "chat_id"),
        ({"chat_id": "x", "allowed_user_ids": [1]}, "chat_id"),
        ({"chat_id": 0, "allowed_user_ids": [1]}, "chat_id"),
        ({"chat_id": -100}, "allowed_user_ids"),
        ({"chat_id": -100, "allowed_user_ids": []}, "allowed_user_ids"),
        ({"chat_id": -100, "allowed_user_ids": ["1"]}, "allowed_user_ids"),
        ({"chat_id": -100, "allowed_user_ids": [True]}, "allowed_user_ids"),
        ({"chat_id": -100, "allowed_user_ids": [1], "registry_refresh_seconds": 1}, "registry_refresh_seconds"),
        ({"chat_id": -100, "allowed_user_ids": [1], "control_topic_name": ""}, "control_topic_name"),
        ({"chat_id": -100, "allowed_user_ids": [1], "bot_token": "123:abc"}, "environment"),
        ({"chat_id": -100, "allowed_user_ids": [1], "typo": 1}, "unknown key"),
    ],
)
def test_invalid_telegram(tg, msg):
    raw = {k: v for k, v in OK.items() if k != "telegram"}
    if tg is not None:
        raw["telegram"] = tg
    with pytest.raises(ConfigError, match=msg):
        parse_config(raw)


def test_token_comes_only_from_the_environment():
    assert read_token({"CQB_TELEGRAM_TOKEN": " 123:abc "}) == "123:abc"
    for env in ({}, {"CQB_TELEGRAM_TOKEN": "  "}):
        with pytest.raises(ConfigError, match="CQB_TELEGRAM_TOKEN"):
            read_token(env)


def test_load_errors(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(str(tmp_path / "none.toml"))
    bad = tmp_path / "bad.toml"
    bad.write_text("[codex\n")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(str(bad))
    good = tmp_path / "ok.toml"
    good.write_text('[codex]\nsocket="/s"\n[db]\npath="/d"\n[telegram]\nchat_id=-100\nallowed_user_ids=[1]\n')
    assert load_config(str(good)).db.path == "/d"


def test_example_config_is_valid():
    import pathlib
    example = pathlib.Path(__file__).parent.parent / "config.example.toml"
    assert load_config(str(example)).telegram.allowed_user_ids == (123456789,)


# --- consultant chat: [pico], [mcp], tokens ------------------------------------------------

from cqb.config import McpConfig, PicoConfig, read_consultant_tokens  # noqa: E402


def test_consultants_are_off_without_a_pico_section():
    c = parse_config(OK)
    assert c.pico is None and c.mcp == McpConfig(18791)


def test_pico_and_mcp_defaults_and_overrides():
    c = parse_config({**OK, "pico": {}, "mcp": {"port": 19000}})
    assert c.pico == PicoConfig("ws://127.0.0.1:18790/pico/ws", "cqb-", 120, 50, 0)  # turn timeout 0 = no limit
    assert c.mcp.port == 19000
    c = parse_config({**OK, "pico": {"url": "ws://localhost:1/x", "idle_seconds": 10, "max_connections": 90,
                                     "turn_timeout_seconds": 600, "session_prefix": "abc-"}})
    assert (c.pico.idle_seconds, c.pico.max_connections, c.pico.turn_timeout_seconds, c.pico.session_prefix) == (10, 90, 600, "abc-")


@pytest.mark.parametrize("pico, msg", [
    ({"url": "ws://example.com:18790/pico/ws"}, "loopback"),
    ({"url": "wss://127.0.0.1:18790/pico/ws"}, "loopback"),
    ({"url": "ws://127.0.0.1/pico/ws"}, "loopback"),
    ({"url": 5}, "loopback"),
    ({"idle_seconds": 1}, "idle_seconds"),
    ({"max_connections": 100}, "max_connections"),  # must stay below PicoClaw's own limit of 100
    ({"turn_timeout_seconds": -1}, "turn_timeout_seconds"),
    ({"session_prefix": "Bad_"}, "session_prefix"),
    ({"token": "x"}, "environment"),
    ({"typo": 1}, "unknown key"),
])
def test_invalid_pico(pico, msg):
    with pytest.raises(ConfigError, match=msg):
        parse_config({**OK, "pico": pico})


def test_invalid_mcp():
    for mcp in ({"port": 80}, {"port": "x"}, {"host": "0.0.0.0"}, {"token": "x"}):
        with pytest.raises(ConfigError):
            parse_config({**OK, "mcp": mcp})


def test_consultant_tokens_come_from_the_environment_and_must_differ():
    t = read_consultant_tokens({"CQB_PICO_TOKEN": " pico-secret ", "CQB_MCP_TOKEN": "mcp-secret"})
    assert (t.pico, t.mcp) == ("pico-secret", "mcp-secret") and "secret" not in repr(t)
    for env, name in (({"CQB_MCP_TOKEN": "b"}, "CQB_PICO_TOKEN"), ({"CQB_PICO_TOKEN": "a"}, "CQB_MCP_TOKEN"), ({}, "CQB_PICO_TOKEN")):
        with pytest.raises(ConfigError, match=name):
            read_consultant_tokens(env)
    with pytest.raises(ConfigError, match="different"):
        read_consultant_tokens({"CQB_PICO_TOKEN": "same", "CQB_MCP_TOKEN": "same"})


def test_inbox_topic_name_is_no_longer_a_config_key():
    with pytest.raises(ConfigError, match="inbox_topic_name"):
        parse_config({**OK, "telegram": {**TG, "inbox_topic_name": "x"}})
    assert parse_config({**OK, "telegram": {**TG, "registry_refresh_seconds": 3600}}).telegram.registry_refresh_seconds == 3600


# --- /status snapshot: [status] ------------------------------------------------------------

from cqb.config import StatusConfig  # noqa: E402


def test_status_defaults_and_overrides():
    assert parse_config(OK).status == StatusConfig(timeout_seconds=300, slots=2)
    assert parse_config({**OK, "status": {}}).status == StatusConfig(300, 2)
    c = parse_config({**OK, "status": {"timeout_seconds": 30, "slots": 8}})
    assert (c.status.timeout_seconds, c.status.slots) == (30, 8)
    assert parse_config({**OK, "status": {"timeout_seconds": 3600, "slots": 1}}).status == StatusConfig(3600, 1)


@pytest.mark.parametrize("status, msg", [
    ({"timeout_seconds": 29}, "timeout_seconds"), ({"timeout_seconds": 3601}, "timeout_seconds"),
    ({"timeout_seconds": 0}, "timeout_seconds"), ({"timeout_seconds": "300"}, "timeout_seconds"),
    ({"timeout_seconds": True}, "timeout_seconds"), ({"slots": 0}, "slots"), ({"slots": 9}, "slots"),
    ({"slots": 1.5}, "slots"), ({"typo": 1}, "unknown key"), ({"token": "x"}, "environment"),
])
def test_invalid_status(status, msg):
    with pytest.raises(ConfigError, match=msg):
        parse_config({**OK, "status": status})


def test_example_config_documents_the_status_section():
    import pathlib
    example = pathlib.Path(__file__).parent.parent / "config.example.toml"
    assert load_config(str(example)).status == StatusConfig(300, 2)


# --- agent-message questions: [questions] --------------------------------------------------

from cqb.config import QuestionsConfig  # noqa: E402


def test_questions_defaults_bounds_and_example():
    assert parse_config(OK).questions == QuestionsConfig(agent_scan_seconds=20)
    assert parse_config({**OK, "questions": {}}).questions == QuestionsConfig(20)
    for ok in (5, 600):
        assert parse_config({**OK, "questions": {"agent_scan_seconds": ok}}).questions.agent_scan_seconds == ok
    import pathlib
    example = pathlib.Path(__file__).parent.parent / "config.example.toml"
    assert load_config(str(example)).questions == QuestionsConfig(20)


@pytest.mark.parametrize("questions, msg", [
    ({"agent_scan_seconds": 4}, "agent_scan_seconds"), ({"agent_scan_seconds": 601}, "agent_scan_seconds"),
    ({"agent_scan_seconds": "20"}, "agent_scan_seconds"), ({"agent_scan_seconds": True}, "agent_scan_seconds"),
    ({"agent_scan_seconds": 20.5}, "agent_scan_seconds"), ({"typo": 1}, "unknown key"),
    ({"token": "x"}, "environment"), ("not a table", "missing section"),
])
def test_invalid_questions(questions, msg):
    with pytest.raises(ConfigError, match=msg):
        parse_config({**OK, "questions": questions})


# --- [mcp] project files: off by default, fail-fast validation ---------------------------------

def test_project_files_are_off_by_default_and_bounded():
    c = parse_config(OK).mcp
    assert (c.project_files, c.project_file_max_bytes, c.project_dir_max_entries, c.project_file_deny_extra) == (False, 16384, 200, ())
    assert parse_config({**OK, "mcp": {"port": 19000}}).mcp.project_files is False  # a [mcp] section alone does not enable it
    c = parse_config({**OK, "mcp": {"project_files": True, "project_file_max_bytes": 1024, "project_dir_max_entries": 1000,
                                    "project_file_deny_extra": ["*.private", "notes"]}}).mcp
    assert c.project_files is True and c.project_file_max_bytes == 1024 and c.project_dir_max_entries == 1000
    assert c.project_file_deny_extra == ("*.private", "notes")
    assert parse_config({**OK, "mcp": {"project_file_max_bytes": 65536}}).mcp.project_file_max_bytes == 65536


@pytest.mark.parametrize("mcp", [
    {"project_files": "yes"}, {"project_files": 1}, {"project_files": None},
    {"project_file_max_bytes": 1023}, {"project_file_max_bytes": 65537}, {"project_file_max_bytes": "16384"},
    {"project_file_max_bytes": True}, {"project_dir_max_entries": 0}, {"project_dir_max_entries": 1001},
    {"project_file_deny_extra": "*.x"}, {"project_file_deny_extra": [1]}, {"project_file_deny_extra": [""]},
    {"project_file_deny_extra": ["a/b"]}, {"project_file_deny_extra": ["x" * 101]}, {"project_file_deny_extra": ["x"] * 101},
    {"project_file_deny_extra": ["a\0"]}, {"project_files_root": "/"}, {"project_file_token": "x"},
])
def test_invalid_project_file_settings_fail_fast(mcp):
    with pytest.raises(ConfigError):
        parse_config({**OK, "mcp": mcp})


def test_example_config_documents_project_files_switched_off():
    import pathlib
    example = pathlib.Path(__file__).parent.parent / "config.example.toml"
    assert load_config(str(example)).mcp == McpConfig(18791)
    assert "project_files = false" in example.read_text()
    assert "web_search = false" in example.read_text() and not load_config(str(example)).mcp.web_search
