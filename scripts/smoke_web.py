#!/usr/bin/env python3
"""Live smoke of the consultant's web tools WITHOUT Telegram or a model.

1. web_search: the Bridge's real WebSearch service against the real search endpoint (OmniRoute /v1/search).
2. web_fetch: the scope hook's URL screening on a harmless public page (allowed, with real DNS) and on private targets
   (denied), then one plain GET of the allowed page (status only). The gateway's own fetch is covered by
   tests/e2e/test_web_e2e.py; the kernel egress guard is checked on the server with the steps in the deploy notes.
Secrets come from the environment, never from argv, and are never printed. The endpoint is CQB_WEB_SEARCH_URL and
CQB_WEB_SEARCH_KEY, or else OMNIROUTE_BASE_URL (+ /search) and OMNIROUTE_API_KEY, for example:
  sudo bash -c 'set -a; . /srv/projects/secrets/omniroute-picoclaw.env; set +a; .venv/bin/python scripts/smoke_web.py'
Prints statuses, counts and the titles and URLs of public search results only. Exit code: 0 all good, 1 a check failed.
"""
import asyncio
import importlib.util
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cqb.config import McpConfig, WebSearchEndpoint, read_web_search_endpoint  # noqa: E402
from cqb.services.context import ToolError  # noqa: E402
from cqb.services.web_search import WebSearch  # noqa: E402

PAGE = "https://example.org/"
DENIED = (
    "http://127.0.0.1:18791/mcp", "http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/", "http://100.64.0.1/",
    "http://10.0.0.1/", "http://192.168.1.1/", "http://2130706433/", "http://[::1]/", "http://localhost/",
    "https://example.org:8443/", "https://user@example.org/", "https://example.org/?api_key=x",
)


def load_hook():
    spec = importlib.util.spec_from_file_location("scope_hook", ROOT / "integrations" / "picoclaw_scope_hook.py")
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    return hook


def endpoint() -> WebSearchEndpoint:
    env = dict(os.environ)
    if "CQB_WEB_SEARCH_URL" not in env and env.get("OMNIROUTE_BASE_URL"):
        env["CQB_WEB_SEARCH_URL"] = env["OMNIROUTE_BASE_URL"].rstrip("/") + "/search"
        env.setdefault("CQB_WEB_SEARCH_KEY", env.get("OMNIROUTE_API_KEY", ""))
    return read_web_search_endpoint(env)


async def search(query: str) -> bool:
    async with aiohttp.ClientSession() as http:
        svc = WebSearch(McpConfig(web_search=True), lambda root, source: None, endpoint(), http)
        try:
            found = await svc.search({"root_id": "r", "query": query, "max_results": 3})
        except ToolError as err:
            print(f"search: FAILED code={err.code}")
            return False
        results = found["results"]
        print(f"search: ok results={len(results)} fields={sorted(results[0]) if results else []}")
        for item in results:
            print(f"  - {item['title'][:70]!r} {item['url'][:90]}")
        return bool(results)


def fetch_checks() -> bool:
    hook = load_hook()
    ok = True
    try:
        hook.check_fetch_url(PAGE)
        print(f"fetch: hook allows {PAGE}")
    except hook.Denied as why:
        print(f"fetch: hook DENIED {PAGE}: {why}")
        ok = False
    for url in DENIED:
        try:
            hook.check_fetch_url(url)
            print(f"fetch: hook ALLOWED {url}  <-- wrong")
            ok = False
        except hook.Denied as why:
            print(f"fetch: hook denies {url}: {why}")
    try:
        req = urllib.request.Request(PAGE, headers={"User-Agent": "Mozilla/5.0 (smoke)"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"fetch: GET {PAGE} -> {resp.status}")
    except urllib.error.URLError as exc:
        print(f"fetch: GET {PAGE} FAILED {type(exc).__name__}")
        ok = False
    return ok


def main() -> int:
    try:
        good = asyncio.run(search("PicoClaw github"))
    except Exception as exc:  # a missing variable or an unreachable host: say which kind, never print values
        print(f"search: not run ({type(exc).__name__})")
        good = False
    good = fetch_checks() and good
    print("smoke:", "ok" if good else "FAILED")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
