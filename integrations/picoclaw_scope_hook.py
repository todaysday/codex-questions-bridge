#!/usr/bin/env python3
"""PicoClaw process hook: pins every consultant tool call to its own Codex root (design 4.1, 10).

Spawned by the PicoClaw gateway (JSON-RPC 2.0 over stdio, one JSON object per line). Standard
library only. Intercepts `before_tool` (overwrites root_id/source_id with the trusted values)
and `approve_tool` (fail closed: anything unexpected is denied). MCP servers get no caller
identity from PicoClaw, so this hook is where the session becomes a scope.

Why ask Bridge instead of keeping a table here: consultants are created at runtime, and the
Bridge already owns the binding (SQLite). The hook calls GET <CQB_MCP_URL>/internal/scope/<sid>
on the same loopback MCP server with CQB_MCP_TOKEN, which the hook inherits from the gateway's
environment (PicoClaw passes its own environment to hook processes), so no token is stored in
config.json for the hook. A background thread posts a heartbeat so Bridge can notice a dead hook.

Session id: context.inbound.chat_id == "pico:<session id>" (lowercase). Two kinds of session map to a root
through the same endpoint: a consultant's (`cqb-<hash>`, agent `main`) and a Status Agent slot
(`cqb-status-<n>`, agent `codex-status`, bound to a root only while its /status request runs). A slot
session may be served only by the codex-status agent and only with the four status tools; a consultant
session only by `main`. The two project-file tools (read-only files of the thread's project) are for a
consultant session of the `main` agent only, never for a status slot, and take only the argument names
listed in FILE_ARGS (anything else is denied). A routed session that fell through to the wrong agent is therefore denied.
The two web tools (the Bridge's web_search and PicoClaw's own web_fetch) follow the same rule: the consultant only, known
argument names only. web_fetch is the exfiltration and SSRF risk of the consultant, so approve_tool also validates its URL
(check_fetch_url, fail closed). That check is a pre-flight: redirects and DNS answers that change after the check are
stopped by PicoClaw's own dial-time guard and the gateway's kernel egress rules (deploy/picoclaw/egress.conf.example).
Denial reasons are shown to the model verbatim: they never contain tokens or session ids.
"""

import ipaddress
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

SERVER = "cqb"  # tools.mcp.servers key; PicoClaw names tools mcp_<server>_<tool>
FILE_ARGS = {  # project-file tools and the only argument names they may carry (root_id/source_id are ours)
    f"mcp_{SERVER}_read_project_file": frozenset({"root_id", "source_id", "path", "offset_lines", "max_lines"}),
    f"mcp_{SERVER}_list_project_dir": frozenset({"root_id", "source_id", "path"}),
}
FILE_TOOLS = frozenset(FILE_ARGS)
WEB_SEARCH = f"mcp_{SERVER}_web_search"  # the Bridge's tool (OmniRoute search); gets root_id/source_id like the others
WEB_FETCH = "web_fetch"  # PicoClaw's own tool; takes no scope arguments, so none are added
WEB_ARGS = {
    WEB_SEARCH: frozenset({"root_id", "source_id", "query", "max_results"}),
    WEB_FETCH: frozenset({"url", "maxChars"}),
}
WEB_TOOLS = frozenset(WEB_ARGS)
FETCH_MAX_CHARS = 20000  # before_tool clamps maxChars to this (PicoClaw's own default is 50000)
TOOLS = FILE_TOOLS | WEB_TOOLS | frozenset(
    f"mcp_{SERVER}_{name}"
    for name in ("get_thread_overview", "list_agents", "get_recent_events", "read_event", "get_questions")
)
STATUS_PREFIX = "cqb-status-"  # Status Agent slot sessions: cqb-status-1..N
STATUS_AGENT = "codex-status"
STATUS_TOOLS = frozenset(
    f"mcp_{SERVER}_{name}" for name in ("get_thread_overview", "list_agents", "get_recent_events", "get_questions")
)
CONSULTANT_AGENT = "main"
SESSION = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
HTTP_TIMEOUT = 2.0
WORKERS = 4

_out_lock = threading.Lock()


class Denied(Exception):
    """A decision, not a failure: the reason is safe to show."""


def _setting(name, default=""):
    return os.environ.get(name, default).strip()


def _request(path, method="GET"):
    token = _setting("CQB_MCP_TOKEN")
    if not token:
        raise Denied("scope hook is not configured")
    base = _setting("CQB_MCP_URL", "http://127.0.0.1:18791").rstrip("/")
    req = urllib.request.Request(base + path, method=method, data=b"" if method == "POST" else None,
                                 headers={"Authorization": "Bearer " + token})
    return urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)


def scope_of(params, tool=None):
    """(source_id, root_id) trusted for this call, or raise Denied."""
    inbound = ((params.get("context") or {}).get("inbound") or {})
    chat = inbound.get("chat_id")
    if not isinstance(chat, str) or not chat.startswith("pico:"):
        raise Denied("not a consultant session")
    sid = chat[len("pico:"):].lower()
    if not SESSION.match(sid):
        raise Denied("not a consultant session")
    agent = (params.get("meta") or {}).get("AgentID")
    if sid.startswith(STATUS_PREFIX):
        if agent != STATUS_AGENT:
            raise Denied("this agent may not use status sessions")
        if tool not in STATUS_TOOLS:
            raise Denied("this tool is not available to the status agent")
    elif agent is not None and agent != CONSULTANT_AGENT:
        raise Denied("this agent may not use consultant tools")
    if tool in FILE_TOOLS and agent != CONSULTANT_AGENT:  # explicit `main`: no agent id is not good enough here
        raise Denied("project files are available to the main consultant agent only")
    if tool in WEB_TOOLS and agent != CONSULTANT_AGENT:
        raise Denied("web tools are available to the main consultant agent only")
    try:
        with _request("/internal/scope/" + sid) as r:
            found = json.loads(r.read(4096))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise Denied("unknown or closed consultant session") from None
        raise Denied("scope service refused the request") from None
    except (OSError, ValueError):
        raise Denied("scope service unavailable") from None
    if not isinstance(found, dict) or not isinstance(found.get("root_id"), str) \
            or not isinstance(found.get("source_id"), str):
        raise Denied("scope service gave an unusable answer")
    return found["source_id"], found["root_id"]


def check_tool(params):
    tool = params.get("tool")
    if tool not in TOOLS:
        raise Denied("this tool is not available to the consultant")
    return tool


def check_arguments(tool, args):
    """Project-file and web tools reject any argument name they do not know."""
    allowed = FILE_ARGS.get(tool) or WEB_ARGS.get(tool)
    if allowed is not None and set(args) - allowed:
        raise Denied("unknown argument for this tool")


# --- web tools: what may leave this machine ----------------------------------------------------------
# The consultant reads code and threads, so a URL or a query is a way to carry them out (exfiltration), and a
# fetch is a way to reach the internal network (SSRF). Everything here is a pre-flight and fails closed.
# It cannot see redirects or a DNS answer that changes after the check: those are stopped by PicoClaw's own
# dial-time guard (private ranges refused on every connection, 5 redirects at most) and by the gateway's
# kernel egress rules (deploy/picoclaw/egress.conf.example). The DNS check is best effort (rebinding).
URL_MAX = 300
URL_QUERY_MAX = 120
SEARCH_QUERY_MAX = 200
FETCH_PORTS = (80, 443)
DNS_TIMEOUT = 2.5  # PicoClaw gives approve_tool 5 s
MIN_FETCH_CHARS = 100
_URL_CHARS = re.compile(r"^[\x21-\x7e]+$")  # printable ASCII, no spaces; IDN hosts must be sent as punycode
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TOKEN_RUN = re.compile(r"[A-Za-z0-9+=_-]{40,}")  # '/' separates path parts, so it is not part of a run
_SLUG = re.compile(r"^[a-z0-9]{1,15}(?:-[a-z0-9]{1,15})+$")  # hyphenated lowercase words: an article slug, not a key
_SECRET_PARAM = re.compile(
    r"(?i)bearer|api[-_]?key|access[-_]?token|token|secret|passw|pwd|auth|credential|signature|session|jwt"
    r"|(?:^|[-_])(?:key|sig|sid)(?:$|[-_])"
)
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?)+$")
_BAD_SUFFIX = (".localhost", ".local", ".internal", ".lan", ".home", ".corp", ".intranet", ".arpa", ".ts.net", ".onion")
_BLOCKED = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24",
    "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
    "224.0.0.0/4", "240.0.0.0/4",  # IPv4: private, loopback, link-local (metadata), CGNAT (tailnet), documentation, multicast
    "::/96", "::ffff:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48", "100::/64", "2001::/32", "2001:db8::/32", "2002::/16",
    "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8",  # IPv6: unspecified/loopback, mapped, NAT64, Teredo, 6to4, ULA, link-local, multicast
))
_dns_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dns")
_local_cache = [0.0, frozenset()]


def clamp_fetch_chars(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return FETCH_MAX_CHARS
    return int(min(max(value, MIN_FETCH_CHARS), FETCH_MAX_CHARS))


def token_like(text):
    """A 40+ character run of key-like characters (also after percent-decoding), slugs of lowercase words excepted."""
    seen, decoded = set(), text
    for _ in range(3):  # raw, once and twice percent-decoded
        if decoded in seen:
            break
        seen.add(decoded)
        if any(not _SLUG.match(run) for run in _TOKEN_RUN.findall(decoded)):
            return True
        decoded = urllib.parse.unquote(decoded)
    return False


def check_query(query):
    if not isinstance(query, str):
        raise Denied("query is required")
    text = query.strip()
    if not text or len(text) > SEARCH_QUERY_MAX or _CONTROL.search(text):
        raise Denied("query must be 1..200 characters of plain text")
    if token_like(text):
        raise Denied("query looks like a secret or an identifier: never put private data into a query")


def local_addresses():
    """Addresses of this machine (best effort, Linux): a fetch must never talk to ourselves, public address included."""
    now = time.monotonic()
    if now - _local_cache[0] < 60 and _local_cache[1]:
        return _local_cache[1]
    found = set()
    try:
        last = None
        with open("/proc/net/fib_trie") as f:
            for line in f:
                line = line.strip()
                if line.startswith("|--"):
                    last = line[3:].strip()
                elif line.startswith("/32 host LOCAL") and last:
                    found.add(ipaddress.ip_address(last))
    except (OSError, ValueError):
        pass
    try:
        with open("/proc/net/if_inet6") as f:
            for line in f:
                raw = line.split()[0]
                found.add(ipaddress.ip_address(":".join(raw[i:i + 4] for i in range(0, 32, 4))))
    except (OSError, ValueError, IndexError):
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            found.add(ipaddress.ip_address(info[4][0].split("%")[0]))
    except (OSError, ValueError):
        pass
    _local_cache[0], _local_cache[1] = now, frozenset(found)
    return _local_cache[1]


def _unsafe_address(ip):
    return any(ip in net for net in _BLOCKED) or not ip.is_global or ip in local_addresses()


def _literal_ip(host):
    """The address if host is an IP literal, None for a name; a legacy spelling (decimal, hex, octal, short) is denied."""
    if ":" in host:
        if "%" in host:
            raise Denied("URL is not allowed: IPv6 zone id")
        try:
            return ipaddress.IPv6Address(host)
        except ValueError:
            raise Denied("URL is not allowed: malformed host") from None
    try:
        return ipaddress.IPv4Address(host)
    except ValueError:
        pass
    try:
        socket.inet_aton(host)
    except OSError:
        return None
    raise Denied("URL is not allowed: non-canonical IP address")


def _resolve(host, port):
    future = _dns_pool.submit(lambda: socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
    try:
        infos = future.result(timeout=DNS_TIMEOUT)
    except FutureTimeout:
        raise Denied("URL is not allowed: the host name did not resolve in time") from None
    except OSError:
        raise Denied("URL is not allowed: the host name does not resolve") from None
    try:
        found = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]
    except ValueError:
        raise Denied("URL is not allowed: unusable address") from None
    if not found:
        raise Denied("URL is not allowed: the host name does not resolve")
    return found


def check_fetch_url(url):
    """Deny anything but a plain public http(s) page address (fail closed)."""
    if not isinstance(url, str) or not url:
        raise Denied("url is required")
    if len(url) > URL_MAX:
        raise Denied("URL is too long (max 300 characters)")
    if not _URL_CHARS.fullmatch(url) or "\\" in url:
        raise Denied("URL is not allowed: only printable ASCII (use punycode and percent-encoding)")
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError:
        raise Denied("URL is not allowed: malformed") from None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise Denied("only http and https URLs are allowed")
    if "@" in parts.netloc:
        raise Denied("URL is not allowed: no credentials in URLs")
    if port is not None and port not in FETCH_PORTS:
        raise Denied("URL is not allowed: only ports 80 and 443")
    if len(parts.query) > URL_QUERY_MAX:
        raise Denied("URL query string is too long (max 120 characters)")
    if not host:
        raise Denied("URL is not allowed: no host")
    if token_like(url):
        raise Denied("URL looks like it carries a secret or an identifier: never put private data into a URL")
    decoded = urllib.parse.unquote(url)
    if _CONTROL.search(decoded):
        raise Denied("URL is not allowed: control characters")
    names = [pair.split("=", 1)[0] for pair in re.split(r"[&;]", urllib.parse.unquote(parts.query)) if pair]
    if re.search(r"(?i)\bbearer\b", decoded) or any(_SECRET_PARAM.search(name) for name in names):
        raise Denied("URL is not allowed: credential-like parameter")
    host = host.lower()
    if host.endswith(".") and not host.endswith(".."):
        host = host[:-1]
    ip = _literal_ip(host)
    if ip is not None:
        if _unsafe_address(ip):
            raise Denied("URL is not allowed: private, local or reserved address")
        return
    if not _HOST.match(host) or host.endswith(_BAD_SUFFIX):
        raise Denied("URL is not allowed: host name")
    own = socket.gethostname().lower()
    if host == own or host.split(".")[0] == own:
        raise Denied("URL is not allowed: this machine")
    for address in _resolve(host, port or (443 if parts.scheme.lower() == "https" else 80)):
        if _unsafe_address(address):  # ANY private answer denies, even next to public ones
            raise Denied("URL is not allowed: the host resolves to a private, local or reserved address")


def before_tool(params):
    tool = check_tool(params)
    source_id, root_id = scope_of(params, tool)
    args = params.get("arguments")
    args = dict(args) if isinstance(args, dict) else {}
    check_arguments(tool, args)
    if tool == WEB_FETCH:  # not a Bridge tool: no scope arguments, but a bounded page size
        args["maxChars"] = clamp_fetch_chars(args.get("maxChars"))
    else:
        args["root_id"], args["source_id"] = root_id, source_id
    return {"action": "modify", "call": {**params, "arguments": args}}


def approve_tool(params):
    tool = check_tool(params)
    source_id, root_id = scope_of(params, tool)
    args = params.get("arguments")
    if not isinstance(args, dict):
        raise Denied("arguments are outside the consultant's scope")
    if tool != WEB_FETCH and (args.get("root_id") != root_id or args.get("source_id") != source_id):
        raise Denied("arguments are outside the consultant's scope")
    check_arguments(tool, args)
    if tool == WEB_FETCH:
        check_fetch_url(args.get("url"))
        if args.get("maxChars") != clamp_fetch_chars(args.get("maxChars")):
            raise Denied("maxChars is out of range")
    elif tool == WEB_SEARCH:
        check_query(args.get("query"))
        max_results = args.get("max_results")
        if max_results is not None and (isinstance(max_results, bool) or not isinstance(max_results, int)
                                        or not 1 <= max_results <= 10):
            raise Denied("max_results must be 1..10")
    return {"approved": True}


def decide(method, params):
    """Result object for a request; None for a method this hook does not implement."""
    if method == "hook.hello":
        return {"ok": True, "name": "cqb-scope"}
    handler = {"hook.before_tool": before_tool, "hook.approve_tool": approve_tool}.get(method)
    if handler is None:
        return None
    try:
        return handler(params)
    except Denied as why:
        reason = str(why)
    except Exception:  # fail closed on any bug or malformed input
        reason = "scope check failed"
    if method == "hook.before_tool":
        return {"action": "deny_tool", "reason": reason}
    return {"approved": False, "reason": reason}


def handle(line):
    """One input line -> the response line to write, or None."""
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    if not isinstance(msg, dict) or "id" not in msg or not isinstance(msg.get("method"), str):
        return None  # notifications (runtime events) need no answer
    params = msg.get("params")
    result = decide(msg["method"], params if isinstance(params, dict) else {})
    if result is None:
        return json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "method not found"}})
    return json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result})


def _reply(line):
    out = handle(line)
    if out is not None:
        with _out_lock:
            sys.stdout.write(out + "\n")
            sys.stdout.flush()


def _beat_forever():
    interval = float(_setting("CQB_HOOK_BEAT_SECONDS", "10") or 10)
    while True:
        try:
            _request("/internal/hook/beat", "POST").close()
        except Exception:
            pass  # Bridge may be restarting; the next beat will get through
        time.sleep(interval)


def main():
    threading.Thread(target=_beat_forever, daemon=True).start()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for line in sys.stdin:  # EOF means the gateway is gone: leave
            if line.strip():
                pool.submit(_reply, line)
    sys.stdout.flush()
    os._exit(0)  # do not tear the interpreter down under the heartbeat thread


if __name__ == "__main__":
    main()
