#!/bin/sh
# Install the committed code (git HEAD) into the root-owned venv and restart the service.
# Usage: sudo deploy/update.sh   (run from a checkout; the service never runs from the working tree)
set -eu
repo=$(cd "$(dirname "$0")/.." && pwd)
build=$(mktemp -d)
trap 'rm -rf "$build"' EXIT
git -C "$repo" archive HEAD | tar -x -C "$build"
/opt/cqb/.venv/bin/pip install -q --upgrade --no-deps --force-reinstall "$build"
/opt/cqb/.venv/bin/pip install -q "$build"   # pulls dependency changes, if any
# the PicoClaw scope hook is a standalone script that must match the bridge version
install -D -m 755 "$build/integrations/picoclaw_scope_hook.py" /opt/cqb/integrations/picoclaw_scope_hook.py
systemctl restart cqb.service
# The gateway connects to the bridge's MCP server only while it starts, and the hook is its child: restart it after
# the bridge is listening (override the port with CQB_MCP_PORT if [mcp] uses another one).
if systemctl is-enabled --quiet picoclaw-gateway.service 2>/dev/null; then
  mcp_port=${CQB_MCP_PORT:-18791}
  for _ in $(seq 1 30); do
    ss -ltn "sport = :$mcp_port" | grep -q LISTEN && break
    sleep 1
  done
  systemctl restart picoclaw-gateway.service
fi
systemctl --no-pager --lines=5 status cqb.service
