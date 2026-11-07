#!/bin/sh
# Pull-based auto-deploy: when origin/main moved, test it, deploy it, health-check it, roll back on failure.
# Runs as root from cqb-autodeploy.timer. Usage: autodeploy.sh [--status | --force <sha>]
# See deploy/AUTODEPLOY.md. Every path and command can be overridden by an environment variable (the tests do).
set -eu

CQB_REPO_URL=${CQB_REPO_URL:-https://github.com/todaysday/codex-questions-bridge.git}
CQB_BRANCH=${CQB_BRANCH:-main}
CQB_SRC=${CQB_SRC:-/opt/cqb/src}
CQB_STATE=${CQB_STATE:-/var/lib/cqb-deploy}
CQB_LOCK=${CQB_LOCK:-$CQB_STATE/lock}
CQB_TEST_CMD=${CQB_TEST_CMD:-/opt/cqb/ci-venv/bin/python -m pytest -q}
CQB_UPDATE_CMD=${CQB_UPDATE_CMD:-$CQB_SRC/deploy/update.sh}
CQB_HEALTH_CMD=${CQB_HEALTH_CMD:-}   # empty: the built-in check (health_default)
CQB_TEST_TIMEOUT=${CQB_TEST_TIMEOUT:-600}
CQB_FETCH_TIMEOUT=${CQB_FETCH_TIMEOUT:-60}
export CQB_SRC CQB_STATE GIT_TERMINAL_PROMPT=0 PYTHONDONTWRITEBYTECODE=1

log() { printf '%s autodeploy: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }
die() { log "$*"; exit 1; }
src() { git -C "$CQB_SRC" "$@"; }
read_state() { cat "$CQB_STATE/$1" 2>/dev/null || true; }
write_state() { printf '%s\n' "$2" >"$CQB_STATE/$1.tmp" && mv "$CQB_STATE/$1.tmp" "$CQB_STATE/$1"; }

# One line per attempt: timestamp, short sha, subject, result. Only the last 200 lines are kept.
record() { # sha result
  _subject=$(src log -1 --format=%s "$1" 2>/dev/null | tr -d '\n' | cut -c1-100 || true)
  printf '%s %s "%s" %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$(printf %s "$1" | cut -c1-10)" "$_subject" "$2" \
    >>"$CQB_STATE/deploy.log"
  tail -n 200 "$CQB_STATE/deploy.log" >"$CQB_STATE/deploy.log.tmp" && mv "$CQB_STATE/deploy.log.tmp" "$CQB_STATE/deploy.log"
}

checkout() { src checkout -q -f --detach "$1" && src clean -fdq; }

# The commands run from the checkout; their output goes to stderr (the journal).
run_cmd() { # seconds command
  (cd "$CQB_SRC" && timeout "$1" sh -c "$2") >&2
}

health_up() {
  why=
  systemctl is-active --quiet cqb.service || { why="cqb.service is not active"; return 1; }
  if systemctl is-enabled --quiet picoclaw-gateway.service 2>/dev/null; then
    systemctl is-active --quiet picoclaw-gateway.service || { why="picoclaw-gateway.service is not active"; return 1; }
  fi
  ss -ltn "sport = :${CQB_MCP_PORT:-18791}" | grep -q LISTEN || { why="MCP port ${CQB_MCP_PORT:-18791} is not listening"; return 1; }
}

# Services active, MCP port listening, and no ERROR/Traceback lines from cqb since the restart.
health_default() {
  deadline=$(( $(date +%s) + ${CQB_HEALTH_TIMEOUT:-60} ))
  until health_up; do
    [ "$(date +%s)" -lt "$deadline" ] || { log "health: $why"; return 1; }
    sleep 2
  done
  sleep "${CQB_HEALTH_SETTLE:-5}"   # a crash loop passes a single check
  health_up || { log "health: $why after settling"; return 1; }
  bad=$(journalctl -u cqb.service --since "${CQB_DEPLOY_SINCE:-1 min ago}" --no-pager -q 2>/dev/null \
    | grep -E ' (ERR|ERROR|CRITICAL) |Traceback' | head -n 5 || true)
  [ -z "$bad" ] || { log "health: cqb logged errors since the restart:"; printf '%s\n' "$bad" >&2; return 1; }
}

health() {
  if [ -n "$CQB_HEALTH_CMD" ]; then run_cmd 120 "$CQB_HEALTH_CMD"; else health_default; fi
}

mark_bad() { write_state bad_sha "$1"; }

# deploy <new sha> <previous deployed sha or empty>
deploy() {
  new=$1 prev=$2
  short=$(printf %s "$new" | cut -c1-10)
  checkout "$new" || die "cannot check out $short"
  log "testing $short"
  if ! run_cmd "$CQB_TEST_TIMEOUT" "$CQB_TEST_CMD"; then
    mark_bad "$new"; record "$new" tests-failed
    [ -z "$prev" ] || checkout "$prev" || true
    die "tests failed for $short: not deployed, marked bad"
  fi
  CQB_DEPLOY_SINCE=$(date '+%Y-%m-%d %H:%M:%S'); export CQB_DEPLOY_SINCE
  log "deploying $short"
  if run_cmd 900 "$CQB_UPDATE_CMD" && health; then
    write_state deployed_sha "$new"
    rm -f "$CQB_STATE/bad_sha"
    record "$new" deployed
    log "deployed $short"
    return 0
  fi
  mark_bad "$new"
  if [ -z "$prev" ]; then
    record "$new" failed-no-rollback
    die "deploy of $short failed and there is no previous deployed sha: nothing to roll back to"
  fi
  log "deploy of $short failed: rolling back to $(printf %s "$prev" | cut -c1-10)"
  CQB_DEPLOY_SINCE=$(date '+%Y-%m-%d %H:%M:%S'); export CQB_DEPLOY_SINCE
  if checkout "$prev" && run_cmd 900 "$CQB_UPDATE_CMD" && health; then
    record "$new" rolled-back
    die "deploy of $short failed: rolled back to $(printf %s "$prev" | cut -c1-10), marked bad"
  fi
  record "$new" rollback-failed
  die "deploy of $short failed and the rollback failed too: the service needs the operator"
}

status() {
  echo "deployed_sha: $(read_state deployed_sha)"
  echo "bad_sha:      $(read_state bad_sha)"
  echo "last log lines ($CQB_STATE/deploy.log):"
  tail -n 10 "$CQB_STATE/deploy.log" 2>/dev/null || echo "  (none)"
}

usage() { echo "usage: $0 [--status | --force <sha>]" >&2; exit 64; }

force=
case "${1:-}" in
  "") ;;
  --status) status; exit 0 ;;
  --force) [ $# -eq 2 ] && [ -n "$2" ] || usage; force=$2 ;;
  *) usage ;;
esac

mkdir -p "$CQB_STATE" && chmod 700 "$CQB_STATE"
exec 9>>"$CQB_LOCK"
flock -n 9 || { log "another run is in progress"; exit 0; }

if [ ! -d "$CQB_SRC/.git" ]; then
  log "cloning $CQB_REPO_URL"
  mkdir -p "$CQB_SRC"
  timeout 300 git clone -q "$CQB_REPO_URL" "$CQB_SRC" || die "clone failed"
fi

fetched=1
timeout "$CQB_FETCH_TIMEOUT" git -C "$CQB_SRC" fetch -q origin "$CQB_BRANCH" || fetched=0
cur=$(read_state deployed_sha)
bad=$(read_state bad_sha)

if [ -n "$force" ]; then
  new=$(src rev-parse --verify -q "$force^{commit}" 2>/dev/null) \
    || { timeout "$CQB_FETCH_TIMEOUT" git -C "$CQB_SRC" fetch -q origin "$force" 2>/dev/null || true
         new=$(src rev-parse --verify -q "$force^{commit}" 2>/dev/null) || die "unknown commit $force"; }
  log "forced deploy of $new"
  deploy "$new" "$cur"
  exit 0
fi

# A network blip is not an alarm: try again on the next tick.
[ "$fetched" = 1 ] || { log "fetch of origin/$CQB_BRANCH failed, will retry"; exit 0; }
new=$(src rev-parse --verify -q 'FETCH_HEAD^{commit}') || die "cannot resolve origin/$CQB_BRANCH"

[ "$new" != "$cur" ] || exit 0
if [ "$new" = "$bad" ]; then
  # Stays failed (visible in systemctl) until a newer commit arrives or the operator acts.
  log "origin/$CQB_BRANCH $(printf %s "$new" | cut -c1-10) is marked bad (see --status); waiting for a new commit"
  exit 2
fi
if [ -n "$cur" ] && ! src merge-base --is-ancestor "$cur" "$new" 2>/dev/null; then
  log "REFUSED: origin/$CQB_BRANCH $(printf %s "$new" | cut -c1-10) does not descend from the deployed $(printf %s "$cur" | cut -c1-10) (history rewritten?). Nothing changed. Review it, then run: autodeploy.sh --force <sha>"
  exit 2
fi
deploy "$new" "$cur"
