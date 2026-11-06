import pytest

from cqb.config import ConfigError, load_config, parse_config, read_token

TG = {"chat_id": -1001234567890, "allowed_user_ids": [123]}
OK = {"codex": {"socket": "/tmp/s"}, "db": {"path": "/tmp/x.db"}, "telegram": TG}


def test_defaults():
    c = parse_config(OK)
    assert (c.codex.source_id, c.codex.poll_interval_seconds, c.codex.recency_days) == ("local", 30, 30)
    assert c.telegram.chat_id == -1001234567890 and c.telegram.allowed_user_ids == (123,)
    assert (c.telegram.control_topic_name, c.telegram.inbox_topic_name) == ("📋 Треды Codex", "📥 Inbox")
    assert c.telegram.registry_refresh_seconds == 30
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
