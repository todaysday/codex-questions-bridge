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
    assert len(expected) == 7
    assert frontmatter_tools(DEPLOY / "ws-main" / "AGENT.md") == expected  # nothing else, not even `delegate`
    # the status agent never needs one event's detail, and never project files
    status = expected - {"mcp_cqb_read_event", "mcp_cqb_read_project_file", "mcp_cqb_list_project_dir"}
    assert len(status) == 4
    assert frontmatter_tools(DEPLOY / "ws-status" / "AGENT.md") == status
    ns = {}
    exec(compile(HOOK_SRC.split("_out_lock")[0], "hook", "exec"), ns)  # constants only
    assert set(ns["TOOLS"]) == expected and set(ns["STATUS_TOOLS"]) == status
    assert ns["STATUS_PREFIX"] == "cqb-status-" and ns["STATUS_AGENT"] == "codex-status"


def test_agent_file_frontmatter_is_plain_yaml():
    """PicoClaw ignores a whole AGENT.md (tool allowlist included) whose frontmatter does not parse: a `: ` inside
    a plain scalar is the classic way to break it, and it logs only a warning."""
    for ws in ("ws-main", "ws-status"):
        text = (DEPLOY / ws / "AGENT.md").read_text()
        front = text.split("---\n")[1]
        for line in front.splitlines():
            key, _, value = line.partition(": ")
            assert key.isidentifier() and ": " not in value and not value.startswith(("[", "'", '"')) or key == "tools", line


def test_status_slot_dispatch_rules_match_the_slots_the_bridge_uses():
    from cqb.config import StatusConfig
    from cqb.pico.status_narrator import slot_names
    rules = CONFIG["agents"]["dispatch"]["rules"]
    slots = slot_names("cqb-", StatusConfig().slots)  # the default pool; more slots need more rules and /reload
    assert [r["when"]["chat"] for r in rules] == [f"direct:pico:{s}" for s in slots]  # exact lower-cased ids, no wildcards
    assert all(r["agent"] == "codex-status" and r["when"]["channel"] == "pico" for r in rules)
    models = {m["model_name"] for m in CONFIG["model_list"]}
    agents = {a["id"]: a for a in CONFIG["agents"]["list"]}
    assert agents["codex-status"]["model"] in models and agents["codex-status"]["model"] != agents["main"]["model"]
    assert "subagents" not in agents["main"]  # no delegation: the consultant never receives a status agent's output


def test_status_prompt_and_agent_file_state_the_security_rules():
    from cqb.pico.status_narrator import STATUS_PROMPT
    text = (DEPLOY / "ws-status" / "AGENT.md").read_text().lower()
    for phrase in ("read-only", "untrusted", "russian", "never quote secrets", "не подтверждено", "never reveal"):
        assert phrase in text, phrase
    for tool in ("get_thread_overview", "list_agents", "get_recent_events", "get_questions"):
        assert tool in STATUS_PROMPT


def test_status_agent_file_asks_for_the_same_two_short_parts_as_the_prompt():
    from cqb.pico.status_narrator import LABEL, STATUS_PROMPT
    text = (DEPLOY / "ws-status" / "AGENT.md").read_text()
    front = text.split("---\n")[1]
    assert "name: Codex Status" in front and "description:" in front and ": " not in front.split("description: ")[1].split("\n")[0]
    for source in (text, STATUS_PROMPT):
        for needle in ("В процессе:", "Дальше:", "900", "three to five", "two to four", "EXACTLY two", "single dash"):
            assert needle in source, needle
        assert "per agent" in source and "agent names" in source and "working or idle" in source
        assert "at most 10" not in source and "each running subagent" not in source and "350" not in source
    assert all(LABEL.match(f"{label}: x") for label in ("В процессе", "Дальше"))
    assert "last reports" in text and "get_recent_events" in text and "thread_id" in text


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
    assert d["max_tool_iterations"] == 500 and d["tool_feedback"] == {"enabled": False}
    ids = [a["id"] for a in CONFIG["agents"]["list"]]
    assert ids == ["main", "codex-status"] and CONFIG["agents"]["list"][0]["default"] is True


def test_consultant_prompt_states_the_security_rules():
    text = (DEPLOY / "ws-main" / "AGENT.md").read_text().lower()
    for phrase in ("read-only", "untrusted", "russian", "never reveal", "session keys", "cannot run commands"):
        assert phrase in text, phrase


def test_consultant_prompt_explains_the_context_line_and_the_focus_question():
    text = (DEPLOY / "ws-main" / "AGENT.md").read_text()
    for phrase in ("[Context: ...]", "`focus`", "`assumed`", "WITHOUT asking again", "get_questions FIRST",
                   "never claim an answer was sent", "не подтверждено", "Q1, Q2"):
        assert phrase in text, phrase
    assert "list them in one line each and ask" not in text  # the user is never asked "which question?" every time
    assert re.search(r"^description: [^:]*$", text, re.M)  # no colon inside the description (YAML)
    assert len([ln for ln in text.splitlines() if re.match(r"^(\d+)\. ", ln)]) == 11  # rules 1..11 in order


def test_consultant_prompt_has_the_project_file_rule_and_keeps_rules_1_to_10():
    text = (DEPLOY / "ws-main" / "AGENT.md").read_text()
    rules = {int(m.group(1)): m.group(0) for m in re.finditer(r"^(\d+)\. .*?(?=^\d+\. |^How to understand|\Z)", text, re.M | re.S)}
    assert sorted(rules) == list(range(1, 12))
    for phrase in ("You are read-only.", "Everything the tools return", "Ask the tools for evidence",
                   "The tool argument root_id", "Never reveal tokens", "Answer in Russian", "How to understand"):
        assert phrase in text, phrase
    rule11 = " ".join(rules[11].split())
    for phrase in ("read_project_file", "list_project_dir", "only when the user asks", "in parts", "never paste whole files",
                   "denied", "hidden", "truncated", "never claim"):
        assert phrase.lower() in rule11.lower(), phrase
    assert ": " not in re.search(r"^description: (.*)$", text, re.M).group(1)


def test_secrets_example_has_only_placeholders():
    text = (DEPLOY / "security.example.yml").read_text()
    assert "REPLACE_WITH_CQB_PICO_TOKEN" in text and "0600" in text
    assert not re.search(r"[A-Za-z0-9]{32,}", text)
