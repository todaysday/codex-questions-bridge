# Auto-deploy (pull-based)

Every push to `main` goes live on the server by itself, 1 to 2 minutes later. The server **pulls**; nothing
connects to it, and no GitHub runner runs on it.

## How it works

```
GitHub main
     |  git fetch (every 60 s, systemd timer cqb-autodeploy.timer)
     v
/opt/cqb/bin/autodeploy.sh (root, flock: runs never overlap)
     |-- same sha as deployed_sha ........................ exit 0, nothing logged
     |-- same sha as bad_sha ............................. exit 2, "marked bad", nothing retried
     |-- not a descendant of deployed_sha (history rewritten) -> REFUSED, exit 2, nothing written
     v
checkout the new sha (detached) in /opt/cqb/src
     |-- run the tests (/opt/cqb/ci-venv, pytest -q) --- fail -> bad_sha = sha, log, exit 1, NOT deployed
     v
deploy/update.sh of that checkout (installs into /opt/cqb/.venv, restarts cqb and the gateway)
     v
health check (up to 60 s): cqb (and picoclaw-gateway when enabled) active, MCP port 18791 listening,
     no ERROR / Traceback line from cqb since the restart
     |-- fail -> checkout the previous sha, update.sh again, bad_sha = sha, log, exit 1
     v
success: deployed_sha = sha, one line in /var/lib/cqb-deploy/deploy.log (last 200 lines kept)
```

A run that exits non-zero leaves `cqb-autodeploy.service` in the `failed` state, which shows in
`systemctl status` and `systemctl --failed`. While `bad_sha` is still the head of `main`, every tick fails the same
way (cheap, nothing is retried) so the failure stays visible until a new commit arrives or you act. A network error
while fetching is not a failure: it is logged and retried at the next tick.

Files: `deploy/autodeploy.sh` (installed as a root-owned copy in `/opt/cqb/bin/`, so a push can never change the
script while it runs), `deploy/cqb-autodeploy.service`, `deploy/cqb-autodeploy.timer`,
`deploy/install-autodeploy.sh`. `.github/workflows/ci.yml` runs the tests on GitHub for visibility only; it is not a
deploy gate.

## Trust model

**Whoever can push to `main` controls what runs as root on this server.** The deployer installs and runs that code
with the same privileges as `update.sh`. So:

- enable two-factor authentication on every GitHub account with write access;
- consider branch protection on `main` (require pull requests, block force pushes);
- history rewrites are refused on purpose: a force-pushed `main` is not deployed until an operator reviews it and runs
  `--force`;
- the repo is public, so fork pull requests never reach the server: only `main` of this repo is fetched, and no
  secrets are involved (the deployer needs none; the repo is cloned over HTTPS read-only).

## Install

From a checkout, as root. Idempotent: run it again to refresh the script copy, the units and the CI venv.

```sh
# when the code running now is the commit you name (so the first poll does not redeploy it)
sudo deploy/install-autodeploy.sh --assume-deployed "$(git rev-parse origin/main)"
# without the option the first poll deploys origin/main
sudo deploy/install-autodeploy.sh
```

It creates the root-owned clone `/opt/cqb/src`, the CI venv `/opt/cqb/ci-venv` (`pip install -e "/opt/cqb/src[dev]"`,
needs PyPI), the state directory `/var/lib/cqb-deploy` (mode 700), copies the script to `/opt/cqb/bin/`, copies the two
units to `/etc/systemd/system`, and enables `cqb-autodeploy.timer`. After changing `autodeploy.sh` or a unit in the
repo, run the installer again: they are not updated by the deploys they perform. After a **new dependency** in
`pyproject.toml`, run it too (it refreshes the CI venv), then clear `bad_sha` if the tests had failed on the import.

## Status and logs

```sh
systemctl status cqb-autodeploy.timer cqb-autodeploy.service   # timer, last run, failed?
journalctl -u cqb-autodeploy -n 50                             # what the last runs did
sudo /opt/cqb/bin/autodeploy.sh --status                       # deployed sha, bad sha, last 10 deploy.log lines
sudo cat /var/lib/cqb-deploy/deploy.log                        # timestamp, short sha, subject, result
```

Results in `deploy.log`: `deployed`, `tests-failed`, `rolled-back`, `rollback-failed`, `failed-no-rollback`.

## Rollback

When the update command or the health check fails, the deployer checks out the previously deployed sha in
`/opt/cqb/src`, runs `update.sh` again and records the bad commit in `bad_sha`; `deployed_sha` stays on the old
commit. The next push (a new sha) is tried normally. If the rollback itself fails the log says `rollback-failed`:
the service needs you. On the very first deploy (no `deployed_sha`) there is nothing to roll back to.

## Pause, resume, deploy a specific commit

```sh
sudo systemctl stop cqb-autodeploy.timer         # pause (a run in progress finishes)
sudo systemctl start cqb-autodeploy.timer        # resume
sudo /opt/cqb/bin/autodeploy.sh --force <sha>    # deploy exactly this commit, any ancestry, even if marked bad
sudo systemctl start cqb-autodeploy.service      # poll right now instead of waiting a minute
sudo rm /var/lib/cqb-deploy/bad_sha              # let the next poll try the marked commit again
```

`--force` still runs the tests and the health check and rolls back on failure. Use it to deploy a reviewed rewrite of
`main`, or to go back to an older commit (then pause the timer, otherwise the next poll moves forward again).

## A new required config key

When a commit needs a key that `/etc/cqb/config.toml` does not have yet, cqb fails to start, the health check fails and
the deploy is rolled back and marked bad. Edit `/etc/cqb/config.toml` first, then either
`sudo /opt/cqb/bin/autodeploy.sh --force <sha>` or `sudo rm /var/lib/cqb-deploy/bad_sha` (the next poll retries).
Better: add the config before pushing the commit that requires it.

## Limits

- Polling delay is up to about a minute (the timer fires every 60 s, accuracy 10 s).
- The tests run on the server before the deploy, so a push is live after roughly 1 to 2 minutes.
- Idle cost is one `git fetch` per minute; nothing runs in between.
- One commit at a time: if several pushes land between polls, only the newest `main` is tested and deployed.
- Only the code in git is deployed. `/etc/cqb`, `/var/lib/picoclaw` (except the AGENT.md files `update.sh` installs)
  and the units of cqb itself are never touched by the deployer.
- The health check cannot prove the bot behaves: it proves the services are up, the MCP port is open and cqb logged no
  error since the restart.
