"""Web tools of the consultant: the hook's URL and query screening, the tool allowlist, the Bridge's web_search.

The consultant reads code and threads, so a URL or a search query is a way out for private data and a fetch is a
way in to the internal network. These tests are adversarial: every spelling of a private address, every
credential-shaped URL, DNS names that resolve to private space, and the wrong agents and sessions.
"""

import asyncio
import contextlib
import importlib.util
import json
import logging
import socket
import sys
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from cqb.config import ConfigError, McpConfig, WebSearchEndpoint, parse_config, read_web_search_endpoint
from cqb.mcp.server import McpServer
from cqb.mcp.tools import build_tools
from cqb.services.context import ToolError
from cqb.services.web_search import BODY_MAX, QUERY_MAX, SNIPPET_MAX, WebSearch, parse_results
from test_mcp import AUTH, TOKEN, World, arun, call as mcp_call, rpc
from test_scope_hook import HOOK, OTHER_SID, SID, SLOT, bridge, call, converse, denied  # noqa: F401  (bridge: fixture)

spec = importlib.util.spec_from_file_location("picoclaw_scope_hook", HOOK)
hook = importlib.util.module_from_spec(spec)
sys.modules["picoclaw_scope_hook"] = hook
spec.loader.exec_module(hook)

PUBLIC = "93.184.216.34"
FETCH = "web_fetch"
SEARCH = "mcp_cqb_web_search"


@pytest.fixture
def dns(monkeypatch):
    """Name -> addresses; anything else does not resolve. The machine's own lookups are pinned too."""
    table = {"example.org": [PUBLIC], "docs.example.org": [PUBLIC, "2606:2800:220:1:248:1893:25c8:1946"]}
    real = socket.getaddrinfo

    def fake(host, port, *a, **kw):
        if host == socket.gethostname():
            return real(host, port, *a, **kw)
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "no such name")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0)) for ip in table[host]]

    monkeypatch.setattr(hook.socket, "getaddrinfo", fake)
    monkeypatch.setattr(hook, "_local_cache", [0.0, frozenset()])
    monkeypatch.setattr(hook, "local_addresses", lambda: frozenset({hook.ipaddress.ip_address("91.98.75.224")}))
    return table


def verdict(url):
    try:
        hook.check_fetch_url(url)
    except hook.Denied as why:
        return str(why)
    return None


# --- URL matrix ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://example.org/",
    "http://example.org/page?q=1&lang=en",
    "https://example.org:443/x",
    "http://example.org:80/x",
    "HTTPS://EXAMPLE.ORG/Docs",  # upper case
    "https://example.org./x",  # one trailing dot
    "https://docs.example.org/guides/configuration.md",
    "https://example.org/news/how-to-configure-the-gateway-for-production-use-in-five-steps",  # a long slug is not a key
    "https://" + "a" * 20 + ".example.org/",
    "https://93.184.216.34/",  # a public IPv4 literal
    "http://[2606:2800:220:1:248:1893:25c8:1946]/",  # a public IPv6 literal
])
def test_ordinary_public_pages_are_allowed(dns, url):
    dns["a" * 20 + ".example.org"] = [PUBLIC]
    assert verdict(url) is None


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://127.1.2.3:80/", "http://0.0.0.0/", "http://10.0.0.1/", "http://10.42.0.1/",
    "http://172.16.0.1/", "http://172.31.255.255/", "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data/",
    "http://100.64.0.1/", "http://100.100.100.200/", "http://100.127.255.254/",  # CGNAT, which holds the tailnet
    "http://100.100.100.1/", "http://100.64.0.7/",  # tailnet addresses (the gateway's model host, this host)
    "http://198.18.0.1/", "http://224.0.0.1/", "http://255.255.255.255/", "http://240.0.0.1/",
    "http://91.98.75.224/",  # this machine's own public address
    "http://[::1]/", "http://[::]/", "http://[fc00::1]/", "http://[fd7a:115c:a1e0::9134:a62c]/", "http://[fe80::1]/",
    "http://[ff02::1]/", "http://[::ffff:127.0.0.1]/", "http://[::ffff:7f00:1]/", "http://[::ffff:10.0.0.1]/",
    "http://[64:ff9b::7f00:1]/", "http://[2002:7f00:1::]/", "http://[2001:0:4136:e378:8000:63bf:3fff:fdd2]/",
    "http://[fd00:ec2::254]/", "http://[0:0:0:0:0:0:0:1]/", "http://[0000:0000:0000:0000:0000:0000:0000:0001]/",
])
def test_private_loopback_cgnat_metadata_and_own_addresses_are_denied(dns, url):
    assert verdict(url) is not None


@pytest.mark.parametrize("url", [
    "http://2130706433/",  # decimal 127.0.0.1
    "http://0x7f000001/", "http://0x7f.0x0.0x0.0x1/", "http://0177.0.0.1/", "http://0177.0000.0000.0001/", "http://127.1/",
    "http://127.0.1/", "http://0300.0250.0.1/", "http://3232235777/", "http://0xc0a80001/", "http://1.1/", "http://017700000001/",
    "http://[::1%25lo]/", "http://[::1%eth0]/",
])
def test_ip_literals_in_odd_encodings_are_denied(dns, url):
    assert verdict(url) is not None


@pytest.mark.parametrize("url", [
    "ftp://example.org/", "file:///etc/passwd", "gopher://example.org/", "javascript:alert(1)", "data:text/plain,hi",
    "ws://example.org/", "example.org/path", "//example.org/path", "http:example.org", "http:///example.org", "",
])
def test_only_http_and_https_with_a_host_are_allowed(dns, url):
    assert verdict(url) is not None


@pytest.mark.parametrize("url", [
    "https://user@example.org/", "https://user:pass@example.org/", "https://example.org@evil.test/",
    "https://:@example.org/", "http://example.org:80@127.0.0.1/", "http://a@b@example.org/",
])
def test_userinfo_is_denied(dns, url):
    assert "credentials" in (verdict(url) or "")


@pytest.mark.parametrize("url", [
    "http://example.org:8080/", "https://example.org:8443/", "http://example.org:22/", "http://example.org:18791/mcp",
    "http://example.org:18790/health", "http://example.org:0/", "http://example.org:65536/", "http://example.org:abc/",
])
def test_only_ports_80_and_443(dns, url):
    assert verdict(url) is not None


def test_length_limits(dns):
    ok = "https://example.org/" + "p/" * 138
    assert len(ok) <= 300 and verdict(ok) is None
    assert verdict("https://example.org/" + "p/" * 141) is not None  # over 300 characters in total
    query = "&".join(["a=b"] * 30)  # 119 characters
    assert len(query) <= 120 and verdict("https://example.org/?" + query) is None
    assert verdict("https://example.org/?" + query + "&a=b&a=b") is not None  # query string over 120


@pytest.mark.parametrize("url", [
    "https://example.org/" + "a" * 40,  # 40 characters of key-like text
    "https://example.org/?q=" + "A1b2C3d4" * 6,
    "https://example.org/" + "0123456789abcdef" * 3,  # a hex digest
    "https://example.org/x/" + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=" + "aaaa",  # base64
    "https://example.org/?q=sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "https://example.org/x/" + "%41" * 45,  # percent-encoded run
    "https://example.org/x/" + "%2541" * 45,  # double encoded
    "https://example.org/Some-Mixed-Case-Words-That-Are-Quite-Long-Indeed-Yes",  # not a lower-case slug
    "https://example.org/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbb",
    "https://example.org/a_b_c_d_e_f_g_h_i_j_k_l_m_n_o_p_q_r_s_t_u_v_w_x_y_z",
])
def test_token_like_runs_are_denied(dns, url):
    assert "secret or an identifier" in (verdict(url) or "")


@pytest.mark.parametrize("url", [
    "https://example.org/?token=abc", "https://example.org/?access_token=1", "https://example.org/?api_key=1",
    "https://example.org/?apikey=1", "https://example.org/?API-KEY=1", "https://example.org/?key=1",
    "https://example.org/?sig=1", "https://example.org/?password=1", "https://example.org/?secret=1",
    "https://example.org/?auth=1", "https://example.org/?Authorization=1", "https://example.org/?x=1&session_id=2",
    "https://example.org/?jwt=1", "https://example.org/?%74oken=1", "https://example.org/?a=b;token=1",
    "https://example.org/Bearer", "https://example.org/?q=bearer%20abc", "https://example.org/?q=Bearer+abc",
])
def test_credential_like_parameters_and_bearer_are_denied(dns, url):
    assert "credential-like" in (verdict(url) or "")


@pytest.mark.parametrize("url", ["https://example.org/?q=monkey&page=2&lang=en", "https://example.org/auth/login-help"])
def test_harmless_parameters_are_allowed(dns, url):
    assert verdict(url) is None


@pytest.mark.parametrize("url", [
    "http://localhost/", "http://LOCALHOST/", "http://localhost./", "http://foo.localhost/", "http://printer.local/",
    "http://metadata.google.internal/computeMetadata/v1/", "http://router.lan/", "http://box.home.arpa/",
    "http://server.tail18e75b.ts.net/", "http://intranet/", "http://example/", "http://hidden.onion/",
    "http://exa mple.org/", "http://example.org../", "http://.example.org/", "http://exa..mple.org/", "http://-a.example.org/",
    "https://example.org/a b", "https://example.org/\tx", "https://example.org/\n", "https://example.org\\@127.0.0.1/",
    "https://example.org/а", "https://пример.рф/", "https://example.org/%00",
])
def test_odd_host_names_and_characters_are_denied(dns, url):
    assert verdict(url) is not None


def test_punycode_names_work_and_unicode_is_refused(dns):
    dns["xn--e1afmkfd.xn--p1ai"] = [PUBLIC]
    assert verdict("https://xn--e1afmkfd.xn--p1ai/") is None
    assert verdict("https://пример.рф/") is not None


def test_this_machine_by_name_is_denied(dns, monkeypatch):
    monkeypatch.setattr(hook.socket, "gethostname", lambda: "Box")
    dns["box.example.org"] = [PUBLIC]
    assert "this machine" in (verdict("https://box.example.org/") or "")
    assert "this machine" in (verdict("https://BOX.example.org/") or "")
    assert verdict("https://other.example.org/") is not None  # does not resolve in the fake table


@pytest.mark.parametrize("answers", [
    ["10.1.2.3"], ["127.0.0.1"], ["100.64.0.7"], ["169.254.169.254"], ["fd7a:115c:a1e0::1"], ["::1"],
    [PUBLIC, "192.168.0.7"],  # ANY private answer denies, even next to a public one
    ["192.168.0.7", PUBLIC], ["::ffff:10.0.0.1"], ["91.98.75.224"],
])
def test_names_that_resolve_to_private_addresses_are_denied(dns, answers):
    dns["rebind.example.org"] = answers
    assert "resolves to a private" in (verdict("https://rebind.example.org/") or "")


def test_names_that_do_not_resolve_or_time_out_are_denied(dns, monkeypatch):
    assert "does not resolve" in (verdict("https://nxdomain.example.org/") or "")

    def slow(*a, **k):
        import time
        time.sleep(0.5)
        return []

    monkeypatch.setattr(hook.socket, "getaddrinfo", slow)
    monkeypatch.setattr(hook, "DNS_TIMEOUT", 0.05)
    assert "in time" in (verdict("https://slow.example.org/") or "")


def test_an_empty_dns_answer_is_denied(dns, monkeypatch):
    monkeypatch.setattr(hook.socket, "getaddrinfo", lambda *a, **k: [])
    assert verdict("https://empty.example.org/") is not None


@pytest.mark.parametrize("value", [None, 5, ["https://example.org/"], {"u": 1}, b"https://example.org/", True])
def test_the_url_must_be_a_string(dns, value):
    with pytest.raises(hook.Denied):
        hook.check_fetch_url(value)


# --- queries ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("query", ["PicoClaw github", "python asyncio.timeout 3.12 documentation", "word " * 39 + "word", "как настроить nginx"])
def test_plain_queries_are_allowed(query):
    hook.check_query(query)


@pytest.mark.parametrize("query", [
    "", "   ", "a " * 101, "x" * 201, "line\nbreak", "tab\there", "\x00", "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "0123456789abcdef" * 3, "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=" + "a" * 8, None, 7, ["q"],
])
def test_bad_queries_are_denied(query):
    with pytest.raises(hook.Denied):
        hook.check_query(query)


# --- the hook as PicoClaw runs it -------------------------------------------------------------------------------

def approve(bridge, tool, args, **kw):
    got, _, _ = converse(bridge.url, [call("hook.approve_tool", tool, args, **kw)])
    return got[1]["result"]


def before(bridge, tool, args, **kw):
    got, _, _ = converse(bridge.url, [call("hook.before_tool", tool, args, **kw)])
    return got[1]["result"]


def test_web_fetch_is_allowed_for_the_consultant_without_scope_arguments(bridge, dns):
    r = before(bridge, FETCH, {"url": "https://example.org/"})
    assert r["action"] == "modify"
    assert r["call"]["arguments"] == {"url": "https://example.org/", "maxChars": hook.FETCH_MAX_CHARS}  # no root_id/source_id
    assert approve(bridge, FETCH, r["call"]["arguments"]) == {"approved": True}


def test_web_fetch_page_size_is_clamped_and_approval_requires_the_clamped_value(bridge, dns):
    for asked, got in ((50000, 20000), (5, 100), (3000.0, 3000), ("big", 20000), (True, 20000), (None, 20000)):
        r = before(bridge, FETCH, {"url": "https://example.org/", "maxChars": asked})
        assert r["call"]["arguments"]["maxChars"] == got
    denied({"result": approve(bridge, FETCH, {"url": "https://example.org/", "maxChars": 50000})}, "maxChars")


def test_web_fetch_denies_private_targets_even_for_the_consultant(bridge, dns):
    for url in ("http://127.0.0.1:18791/mcp", "http://100.64.0.7/", "http://169.254.169.254/", "http://2130706433/"):
        denied({"result": approve(bridge, FETCH, {"url": url, "maxChars": 1000})}, "not allowed")


@pytest.mark.parametrize("extra", [{"root_id": "r1"}, {"headers": {"X": "1"}}, {"method": "POST"}, {"body": "x"}, {"timeout": 5}])
def test_web_fetch_rejects_argument_names_it_does_not_know(bridge, dns, extra):
    denied({"result": before(bridge, FETCH, {"url": "https://example.org/", **extra})}, "unknown argument")
    denied({"result": approve(bridge, FETCH, {"url": "https://example.org/", "maxChars": 1000, **extra})}, "unknown argument")


def test_web_search_is_pinned_to_the_root_and_screened(bridge):
    r = before(bridge, SEARCH, {"query": "PicoClaw github", "root_id": "r2", "source_id": "evil"})
    assert r["action"] == "modify"
    assert r["call"]["arguments"] == {"query": "PicoClaw github", "root_id": "r1", "source_id": "s1"}
    ok = {"query": "PicoClaw github", "max_results": 3, "root_id": "r1", "source_id": "s1"}
    assert approve(bridge, SEARCH, ok) == {"approved": True}
    denied({"result": approve(bridge, SEARCH, {**ok, "root_id": "r2"})}, "outside the consultant's scope")
    denied({"result": approve(bridge, SEARCH, {**ok, "query": "x" * 201})}, "query")
    denied({"result": approve(bridge, SEARCH, {**ok, "query": "0123456789abcdef" * 3})}, "secret")
    denied({"result": approve(bridge, SEARCH, {**ok, "max_results": 11})}, "max_results")
    denied({"result": approve(bridge, SEARCH, {**ok, "max_results": "3"})}, "max_results")
    denied({"result": approve(bridge, SEARCH, {**ok, "max_results": True})}, "max_results")
    denied({"result": approve(bridge, SEARCH, {**ok, "url": "https://example.org/"})}, "unknown argument")
    denied({"result": before(bridge, SEARCH, {"query": "q", "headers": {}})}, "unknown argument")


@pytest.mark.parametrize("tool,args", [
    (FETCH, {"url": "https://example.org/", "maxChars": 1000}),
    (SEARCH, {"query": "PicoClaw github", "root_id": "r7", "source_id": "s1"}),
])
def test_web_tools_are_denied_to_status_slots_in_both_phases(bridge, dns, tool, args):
    for phase in ("hook.before_tool", "hook.approve_tool"):
        got, _, _ = converse(bridge.url, [call(phase, tool, args, chat=f"pico:{SLOT}", agent="codex-status")])
        denied(got[1], "not available to the status agent")
        got, _, _ = converse(bridge.url, [call(phase, tool, args, chat=f"pico:{SLOT}", agent="main")])
        denied(got[1])  # a slot that fell through to the consultant agent gets nothing either


@pytest.mark.parametrize("tool,args", [
    (FETCH, {"url": "https://example.org/", "maxChars": 1000}),
    (SEARCH, {"query": "PicoClaw github", "root_id": "r1", "source_id": "s1"}),
])
def test_web_tools_are_denied_for_other_agents_missing_agent_and_unregistered_sessions(bridge, dns, tool, args):
    for phase in ("hook.before_tool", "hook.approve_tool"):
        got, _, _ = converse(bridge.url, [call(phase, tool, args, agent="codex-status")])
        denied(got[1])
        got, _, _ = converse(bridge.url, [call(phase, tool, args, agent="other")])
        denied(got[1])
        no_agent = call(phase, tool, args)
        no_agent["params"]["meta"] = {}
        got, _, _ = converse(bridge.url, [no_agent])
        denied(got[1], "main consultant agent only")  # an explicit `main` is required
        got, _, _ = converse(bridge.url, [call(phase, tool, args, chat="pico:cqb-" + "c" * 26)])
        denied(got[1], "unknown or closed")
        got, _, _ = converse(bridge.url, [call(phase, tool, args, chat="telegram:1")])
        denied(got[1], "not a consultant session")


def test_web_fetch_fails_closed_when_the_bridge_is_down(bridge, dns):
    got, _, _ = converse("http://127.0.0.1:1", [call("hook.approve_tool", FETCH, {"url": "https://example.org/", "maxChars": 1000})])
    denied(got[1], "scope service unavailable")


def test_native_web_search_and_other_web_like_tools_stay_unavailable(bridge, dns):
    for tool in ("web_search", "mcp_cqb_web_fetch", "browser", "fetch", "WEB_FETCH"):
        denied({"result": approve(bridge, tool, {"url": "https://example.org/", "query": "x"})}, "not available")


def test_denial_reasons_never_echo_the_url_or_secrets(bridge, dns):
    url = "http://127.0.0.1/private-path-marker"
    r = approve(bridge, FETCH, {"url": url, "maxChars": 1000})
    assert "private-path-marker" not in r["reason"] and "127.0.0.1" not in r["reason"]


# --- Bridge: config -------------------------------------------------------------------------------------------

OK = {
    "codex": {"socket": "/tmp/s"}, "db": {"path": "/tmp/db"},
    "telegram": {"chat_id": -1001, "allowed_user_ids": [1]},
}


def test_web_search_is_off_by_default_and_validated():
    assert parse_config({**OK, "mcp": {"port": 19000}}).mcp.web_search is False
    c = parse_config({**OK, "mcp": {"web_search": True, "web_search_max_results": 10}}).mcp
    assert c.web_search is True and c.web_search_max_results == 10
    assert McpConfig().web_search is False and McpConfig().web_search_max_results == 5
    for bad in ({"web_search": "yes"}, {"web_search": 1}, {"web_search_max_results": 0}, {"web_search_max_results": 11},
                {"web_search_max_results": "5"}, {"web_search_key": "x"}, {"web_search_url": "http://x"}):
        with pytest.raises(ConfigError):
            parse_config({**OK, "mcp": bad})


def test_the_search_endpoint_comes_from_the_environment_and_is_checked():
    env = {"CQB_WEB_SEARCH_URL": "http://100.1.2.3:20129/v1/search", "CQB_WEB_SEARCH_KEY": "k-secret-1"}
    ep = read_web_search_endpoint(env)
    assert ep.url == env["CQB_WEB_SEARCH_URL"] and ep.key == "k-secret-1"
    assert "k-secret-1" not in repr(ep)
    for bad in ({}, {"CQB_WEB_SEARCH_URL": env["CQB_WEB_SEARCH_URL"]}, {"CQB_WEB_SEARCH_KEY": "k"},
                {**env, "CQB_WEB_SEARCH_URL": "ftp://h/x"}, {**env, "CQB_WEB_SEARCH_URL": "http://u:p@h/x"},
                {**env, "CQB_WEB_SEARCH_URL": "http:///x"}, {**env, "CQB_WEB_SEARCH_URL": "http://h/x#f"},
                {**env, "CQB_WEB_SEARCH_KEY": "  "}):
        with pytest.raises(ConfigError) as err:
            read_web_search_endpoint(bad)
        assert "k-secret-1" not in str(err.value) and "u:p" not in str(err.value)


# --- Bridge: the tool against a fake endpoint -------------------------------------------------------------------

KEY = "fake-search-key-123"


def answer(n=3, **over):
    return {"id": "search-1", "provider": "x", "query": "q", "answer": None, "usage": {}, "metrics": {}, "errors": [],
            "results": [{"title": f"Title {i}", "url": f"https://example.org/p{i}", "display_url": "example.org",
                         "snippet": f"Snippet {i}", "position": i, "content": {"format": "text", "text": "FULL-TEXT" * 500}}
                        for i in range(n)], **over}


class FakeSearch:
    def __init__(self):
        self.calls = []
        self.status = 200
        self.reply = answer()
        self.raw: bytes | None = None
        self.delay = 0.0
        self.redirect: str | None = None

    async def handle(self, request):
        self.calls.append((request.headers.get("Authorization"), await request.json()))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.redirect:
            raise web.HTTPFound(self.redirect)
        if self.raw is not None:
            return web.Response(status=self.status, body=self.raw)
        return web.json_response(self.reply, status=self.status)


@contextlib.asynccontextmanager
async def search_stack(conn, enabled=True, **cfg):
    fake = FakeSearch()
    app = web.Application()
    app.router.add_post("/v1/search", fake.handle)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    w = World(conn)
    w.scopes = {"cqb-r1": ("s1", "r1")}
    http = aiohttp.ClientSession()
    endpoint = WebSearchEndpoint(f"http://127.0.0.1:{port}/v1/search", KEY)
    svc = WebSearch(McpConfig(web_search=enabled, **cfg), w.ctx.check_scope, endpoint, http)
    srv = McpServer(TOKEN, build_tools(w.ctx, None, svc), w.scopes.get, lambda: None, port=0)
    await srv.start()
    w.http, w.srv, w.url, w.fake, w.svc = http, srv, f"http://127.0.0.1:{srv.port}", fake, svc
    try:
        yield w
    finally:
        await srv.stop()
        await http.close()
        await runner.cleanup()


async def tool_call(w, **args):
    body, is_error = await mcp_call(w, "web_search", **{"root_id": "r1", **args})
    return {**body, "isError": is_error}


def test_tool_is_not_listed_and_is_refused_while_the_flag_is_off(conn):
    async def main():
        async with search_stack(conn, enabled=False) as w:
            _, b = await rpc(w, "tools/list")
            assert "web_search" not in {t["name"] for t in b["result"]["tools"]}
            body = await tool_call(w, query="PicoClaw github")
            assert body["isError"] and body["error"] == "disabled"
            assert w.fake.calls == []
    arun(main())


def test_listing_marks_the_tool_open_world_and_describes_it(conn):
    async def main():
        async with search_stack(conn) as w:
            _, b = await rpc(w, "tools/list")
            t = {t["name"]: t for t in b["result"]["tools"]}["web_search"]
            assert t["annotations"]["openWorldHint"] is True and t["annotations"]["readOnlyHint"] is True
            assert {"query", "max_results", "root_id"} <= set(t["inputSchema"]["properties"])
            others = [x for x in b["result"]["tools"] if x["name"] != "web_search"]
            assert all(x["annotations"]["openWorldHint"] is False for x in others)
    arun(main())


def test_search_sends_the_key_and_a_bounded_body_and_returns_only_title_url_snippet(conn):
    async def main():
        async with search_stack(conn) as w:
            body = await tool_call(w, query="  PicoClaw   github ", max_results=2)
            assert not body.get("isError"), body
            assert w.fake.calls == [(f"Bearer {KEY}", {"query": "PicoClaw github", "max_results": 2})]
            assert [set(r) for r in body["results"]] == [{"title", "url", "snippet"}] * 2  # at most 2, three fields only
            assert body["results"][0] == {"title": "Title 0", "url": "https://example.org/p0", "snippet": "Snippet 0"}
            assert "untrusted" in body["note"] and "FULL-TEXT" not in json.dumps(body) and KEY not in json.dumps(body)
            body = await tool_call(w, query="x")
            assert w.fake.calls[-1][1]["max_results"] == 5 and len(body["results"]) == 3
    arun(main())


def test_the_result_cap_follows_the_config(conn):
    async def main():
        async with search_stack(conn, web_search_max_results=2) as w:
            body = await tool_call(w, query="x")
            assert w.fake.calls[-1][1]["max_results"] == 2 and len(body["results"]) == 2
            body = await tool_call(w, query="x", max_results=3)
            assert body["error"] == "limit_exceeded"
    arun(main())


@pytest.mark.parametrize("args,code", [
    ({}, "invalid_arguments"), ({"query": 5}, "invalid_arguments"), ({"query": ["a"]}, "invalid_arguments"),
    ({"query": ""}, "query_rejected"), ({"query": "   "}, "query_rejected"), ({"query": "x" * (QUERY_MAX + 1)}, "query_rejected"),
    ({"query": "a\x00b"}, "query_rejected"), ({"query": "0123456789abcdef" * 3}, "query_rejected"),
    ({"query": "x", "max_results": 0}, "invalid_arguments"), ({"query": "x", "max_results": "3"}, "invalid_arguments"),
    ({"query": "x", "max_results": True}, "invalid_arguments"), ({"query": "x", "max_results": 11}, "limit_exceeded"),
    ({"query": "x", "url": "https://example.org"}, "invalid_arguments"), ({"query": "x", "headers": {}}, "invalid_arguments"),
])
def test_bad_arguments_never_reach_the_endpoint(conn, args, code):
    async def main():
        async with search_stack(conn) as w:
            body = await tool_call(w, **args)
            assert body["isError"] and body["error"] == code, body
            assert w.fake.calls == []
    arun(main())


def test_the_root_must_be_an_open_consultant(conn):
    async def main():
        async with search_stack(conn) as w:
            for root in ("r3", "nope", "", None):
                body = await tool_call(w, root_id=root, query="x")
                assert body["isError"] and body["error"] in ("scope_denied", "invalid_arguments")
            assert w.fake.calls == []
    arun(main())


def test_results_are_cleaned_and_bounded(conn):
    async def main():
        async with search_stack(conn, web_search_max_results=10) as w:
            w.fake.reply = {"results": [
                {"title": "T\n\x00itle  " + "tt " * 150, "url": "https://example.org/a", "snippet": "s\r\n" + "word " * 200},
                {"title": "javascript", "url": "javascript:alert(1)", "snippet": "x"},
                {"title": "file", "url": "file:///etc/passwd", "snippet": "x"},
                {"title": "nohost", "url": "http:///x", "snippet": "x"},
                {"title": "long", "url": "https://example.org/" + "a" * 300, "snippet": "x"},
                {"title": "space", "url": "https://example.org/a b", "snippet": "x"},
                {"title": "newline", "url": "https://example.org/a\nb", "snippet": "x"},
                "not a dict", None, {"title": 5, "url": 5, "snippet": 5},
                {"title": "secret", "url": "https://example.org/b", "snippet": "token=hunter2hunter2 and ghp_" + "a" * 30},
                {"url": "https://example.org/c"},
            ]}
            body = await tool_call(w, query="x", max_results=10)
            urls = [r["url"] for r in body["results"]]
            assert urls == ["https://example.org/a", "https://example.org/b", "https://example.org/c"]
            first = body["results"][0]
            assert "\n" not in first["title"] and "\x00" not in first["title"] and len(first["title"]) <= 200
            assert len(first["snippet"]) <= SNIPPET_MAX and first["snippet"].startswith("s word word")
            text = json.dumps(body)
            assert "hunter2hunter2" not in text and "ghp_" not in text  # redacted on the way out
            assert body["results"][2] == {"title": "", "url": "https://example.org/c", "snippet": ""}
    arun(main())


@pytest.mark.parametrize("raw", [b"<html>", b"[]", b'"x"', b"null", b'{"results": "x"}', b'{"results": {"a": 1}}', b"{}", b""])
def test_unusable_answers_are_a_structured_error(conn, raw):
    async def main():
        async with search_stack(conn) as w:
            w.fake.raw = raw
            body = await tool_call(w, query="x")
            assert body["isError"] and body["error"] == "bad_answer"
    arun(main())


def test_http_errors_timeouts_oversize_and_redirects_say_nothing_about_the_endpoint(conn, caplog, monkeypatch):
    async def main():
        async with search_stack(conn) as w:
            for status in (401, 403, 429, 500, 502):
                w.fake.status, w.fake.raw = status, f"upstream says key {KEY} is bad".encode()
                body = await tool_call(w, query="a-secret-query-marker")
                assert body["isError"] and body["error"] == "unavailable"
                assert KEY not in json.dumps(body) and "upstream says" not in json.dumps(body)
            w.fake.status, w.fake.raw = 200, b'{"results": [], "pad": "' + b"x" * (BODY_MAX + 10) + b'"}'
            assert (await tool_call(w, query="x"))["error"] == "unavailable"  # larger than the read cap
            w.fake.raw = None
            w.fake.redirect = "http://127.0.0.1:9/elsewhere"  # the key must not follow a redirect
            assert (await tool_call(w, query="x"))["error"] == "unavailable"
            w.fake.redirect = None
            monkeypatch.setattr("cqb.services.web_search.TIMEOUT", aiohttp.ClientTimeout(total=0.1))
            w.fake.delay = 0.5
            assert (await tool_call(w, query="x"))["error"] == "unavailable"
    with caplog.at_level(logging.DEBUG):
        arun(main())
    log = caplog.text
    assert KEY not in log and "a-secret-query-marker" not in log and "upstream says" not in log


def test_an_unreachable_endpoint_is_unavailable(conn):
    async def main():
        async with search_stack(conn) as w:
            w.svc._endpoint = WebSearchEndpoint("http://127.0.0.1:1/v1/search", KEY)
            body = await tool_call(w, query="x")
            assert body["isError"] and body["error"] == "unavailable"
    arun(main())


def test_without_an_endpoint_the_flag_alone_does_nothing(conn):
    w = World(conn)
    svc = WebSearch(McpConfig(web_search=True), w.ctx.check_scope, None, None)
    assert svc.enabled is False and not build_tools(w.ctx, None, svc)["web_search"].listed
    with pytest.raises(ToolError) as err:
        arun(svc.search({"root_id": "r1", "query": "x"}))
    assert err.value.code == "disabled"


def test_parse_results_limits_to_the_asked_count():
    body = json.dumps(answer(8)).encode()
    assert len(parse_results(body, 3)) == 3 and len(parse_results(body, 10)) == 8
