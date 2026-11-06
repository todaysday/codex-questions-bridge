"""The PicoClaw deployment templates stay consistent with the code that depends on them."""

import json
import re
import sys
from pathlib import Path

import pytest

from cqb.config import parse_config
from cqb.mcp.tools import build_tools

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "picoclaw"
CONFIG = json.loads((DEPLOY / "config.example.json").read_text())
HOOK_SRC = (ROOT / "integrations" / "picoclaw_scope_hook.py").read_text()


def tool_names():
    return {f"mcp_cqb_{n}" for n in build_tools(None).keys()}


def frontmatter_tools(path):
    m = re.search(r"^tools:\s*\[(.*?)\]\s*$", path.read_text(), re.M)
    assert m, path
    return {t.strip() for t in m.group(1).split(",")}


def test_the_allowlists_in_agent_files_and_the_hook_match_the_servers_tools():
    expected = tool_names()
    assert len(expected) == 5
    for ws in ("ws-main", "ws-status"):
        assert frontmatter_tools(DEPLOY / ws / "AGENT.md") == expected  # nothing else, not even `delegate`
    ns = {}
    exec(compile(HOOK_SRC.split("_out_lock")[0], "hook", "exec"), ns)  # constants only
    assert set(ns["TOOLS"]) == expected


def test_template_pico_channel_is_loopback_token_less_and_origin_locked():
    assert CONFIG["gateway"]["host"] == "127.0.0.1" and CONFIG["gateway"]["hot_reload"] is False
    pico = CONFIG["channel_list"]["pico"]
    assert pico["allow_from"] == ["pico-user"] and pico["settings"]["allow_origins"] == [""]
    assert pico["settings"]["allow_token_query"] is False and "token" not in pico["settings"]
    assert set(CONFIG["channel_list"]) == {"pico"}  # Bridge owns Telegram; PicoClaw has no other channel


def test_template_uses_the_scope_hook_and_the_http_mcp_server_without_real_secrets():
    hook = CONFIG["hooks"]["processes"]["scope"]
    assert CONFIG["hooks"]["enabled"] and hook["transport"] == "stdio" and hook["command"][-1].endswith("picoclaw_scope_hook.py")
    assert set(hook["intercept"]) == {"before_tool", "approve_tool"}
    server = CONFIG["tools"]["mcp"]["servers"]["cqb"]
    assert server["type"] == "http" and server["url"] == "http://127.0.0.1:18791/mcp"
    assert server["headers"]["Authorization"] == "Bearer __CQB_MCP_TOKEN__"
    assert parse_config({"codex": {"socket": "s"}, "db": {"path": "x"}, "telegram": {"chat_id": -1, "allowed_user_ids": [1]},
                         "pico": {}}).mcp.port == 18791
    blob = (DEPLOY / "config.example.json").read_text()
    assert not re.search(r"sk-[A-Za-z0-9]{10}|api_key|\"token\"", blob)


def test_template_disables_default_tools_and_sizes_the_context():
    tools = CONFIG["tools"]
    for name in ("exec", "write_file", "edit_file", "append_file", "send_file", "message", "spawn", "subagent", "cron",
                 "install_skill", "find_skills", "web_fetch", "web", "skills"):
        assert tools[name] == {"enabled": False}, name
    d = CONFIG["agents"]["defaults"]
    assert (d["context_window"], d["summarize_token_percent"]) == (1_000_000, 80) and d["summarize_message_threshold"] >= 100_000
    assert d["max_tool_iterations"] == 8 and d["tool_feedback"] == {"enabled": False}
    ids = [a["id"] for a in CONFIG["agents"]["list"]]
    assert ids == ["main", "codex-status"] and CONFIG["agents"]["list"][0]["default"] is True


def test_consultant_prompt_states_the_security_rules():
    text = (DEPLOY / "ws-main" / "AGENT.md").read_text().lower()
    for phrase in ("read-only", "untrusted", "russian", "never reveal", "session keys", "cannot run commands"):
        assert phrase in text, phrase


def test_secrets_example_has_only_placeholders():
    text = (DEPLOY / "security.example.yml").read_text()
    assert "REPLACE_WITH_CQB_PICO_TOKEN" in text and "0600" in text
    assert not re.search(r"[A-Za-z0-9]{32,}", text)
