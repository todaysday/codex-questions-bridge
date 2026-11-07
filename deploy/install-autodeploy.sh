#!/bin/sh
# Install (or refresh) the pull-based auto-deploy on this server. Run as root from a checkout; idempotent.
# Usage: sudo deploy/install-autodeploy.sh [--assume-deployed <sha>]
#   --assume-deployed <sha>  the commit that is running right now: written as deployed_sha so the first poll does
#                            not redeploy it. Without it the first poll deploys origin/main.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
CQB_REPO_URL=${CQB_REPO_URL:-https://github.com/todaysday/codex-questions-bridge.git}
CQB_SRC=${CQB_SRC:-/opt/cqb/src}
CQB_STATE=${CQB_STATE:-/var/lib/cqb-deploy}
CI_VENV=${CQB_CI_VENV:-/opt/cqb/ci-venv}
BIN=${CQB_BIN:-/opt/cqb/bin}
UNITS=${CQB_UNIT_DIR:-/etc/systemd/system}

assume=
case "${1:-}" in
  "") ;;
  --assume-deployed) [ $# -eq 2 ] && [ -n "$2" ] || { echo "usage: $0 [--assume-deployed <sha>]" >&2; exit 64; }; assume=$2 ;;
  *) echo "usage: $0 [--assume-deployed <sha>]" >&2; exit 64 ;;
esac
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

# the deployer's own clone (root-owned, used only by the deployer)
if [ ! -d "$CQB_SRC/.git" ]; then
  mkdir -p "$CQB_SRC"
  git clone -q "$CQB_REPO_URL" "$CQB_SRC"
fi

# the deployer script is a root-owned copy: it never runs from a tree a push can change under it
install -d -m 755 "$BIN"
install -m 755 "$here/autodeploy.sh" "$BIN/autodeploy.sh"

# the venv the tests run in: pytest + the project in editable mode against the deployer's checkout
[ -x "$CI_VENV/bin/python" ] || python3 -m venv "$CI_VENV"
"$CI_VENV/bin/pip" install -q -e "$CQB_SRC[dev]"

install -d -m 700 "$CQB_STATE"
if [ -n "$assume" ]; then
  sha=$(git -C "$CQB_SRC" rev-parse --verify -q "$assume^{commit}") || { echo "unknown commit $assume" >&2; exit 1; }
  printf '%s\n' "$sha" >"$CQB_STATE/deployed_sha"
  echo "deployed_sha set to $sha"
fi

install -m 644 "$here/cqb-autodeploy.service" "$here/cqb-autodeploy.timer" "$UNITS/"
systemctl daemon-reload
systemctl enable --now cqb-autodeploy.timer
echo "auto-deploy installed: systemctl status cqb-autodeploy.timer"
