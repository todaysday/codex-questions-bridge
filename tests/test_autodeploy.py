"""deploy/autodeploy.sh against a local bare repo as origin, with stub test, update and health commands. No network."""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = [ROOT / "deploy" / "autodeploy.sh", ROOT / "deploy" / "install-autodeploy.sh"]
AUTODEPLOY = SCRIPTS[0]

needs_tools = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("sh", "git", "flock", "timeout")), reason="needs sh, git, flock and timeout"
)


@pytest.mark.skipif(not shutil.which("sh"), reason="needs sh")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_are_valid_posix_sh(script):
    assert script.read_text().startswith("#!/bin/sh\n")
    subprocess.run(["sh", "-n", str(script)], check=True)


class Env:
    """origin (bare) + a work clone to push from + the deployer's directories, all under tmp_path."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        self.work = tmp / "work"
        self.ctl = tmp / "ctl"
        self.ctl.mkdir()
        self.state = tmp / "state"
        self.src = tmp / "src"
        self.git_env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null"}
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.work)], check=True, stderr=subprocess.DEVNULL)

    def git(self, *args, cwd=None):
        r = subprocess.run(["git", *args], cwd=cwd or self.work, env=self.git_env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def push(self, content, force=False):
        """Commit VERSION=content on top of the work clone and push main. Returns the sha."""
        (self.work / "VERSION").write_text(content + "\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"change {content}")
        self.git("push", "-q", *(["-f"] if force else []), "origin", "HEAD:main")
        return self.git("rev-parse", "HEAD")

    def run(self, *args, **extra):
        env = {
            **self.git_env,
            "CQB_REPO_URL": str(self.origin), "CQB_SRC": str(self.src), "CQB_STATE": str(self.state),
            "CQB_LOCK": str(self.tmp / "lock"), "CTL": str(self.ctl),
            # tests fail for a VERSION containing BADTESTS; health fails for BADHEALTH; update logs the sha it was given
            "CQB_TEST_CMD": "! grep -q BADTESTS VERSION",
            "CQB_UPDATE_CMD": 'git rev-parse HEAD >> "$CTL/updates"',
            "CQB_HEALTH_CMD": '! grep -q BADHEALTH "$CQB_SRC/VERSION"',
            **extra,
        }
        return subprocess.run(["sh", str(AUTODEPLOY), *args], env=env, capture_output=True, text=True, timeout=60)

    def updates(self):
        f = self.ctl / "updates"
        return f.read_text().split() if f.exists() else []

    def read(self, name):
        f = self.state / name
        return f.read_text().strip() if f.exists() else None

    def log_lines(self):
        f = self.state / "deploy.log"
        return f.read_text().splitlines() if f.exists() else []


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


@needs_tools
def test_first_run_clones_and_deploys_origin_main(env):
    sha = env.push("v1")
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert (env.src / ".git").is_dir()
    assert env.updates() == [sha]
    assert env.read("deployed_sha") == sha
    assert os.stat(env.state).st_mode & 0o777 == 0o700
    [line] = env.log_lines()
    assert sha[:10] in line and '"change v1"' in line and line.endswith(" deployed")


@needs_tools
def test_no_op_when_nothing_changed(env):
    env.push("v1")
    assert env.run().returncode == 0
    r = env.run()
    assert r.returncode == 0 and r.stderr == ""
    assert len(env.updates()) == 1 and len(env.log_lines()) == 1


@needs_tools
def test_assumed_deployed_commit_is_not_redeployed(env):
    sha = env.push("v1")
    env.state.mkdir()
    (env.state / "deployed_sha").write_text(sha + "\n")
    r = env.run()
    assert r.returncode == 0 and "deploying" not in r.stderr
    assert env.updates() == [] and env.log_lines() == []
    assert (env.src / ".git").is_dir()


@needs_tools
def test_new_commit_is_tested_deployed_and_logged(env):
    env.push("v1")
    env.run()
    sha2 = env.push("v2")
    r = env.run()
    assert r.returncode == 0, r.stderr
    assert env.updates()[-1] == sha2 and len(env.updates()) == 2
    assert env.read("deployed_sha") == sha2
    assert env.log_lines()[-1].endswith(" deployed") and sha2[:10] in env.log_lines()[-1]
    assert env.git("rev-parse", "HEAD", cwd=env.src) == sha2


@needs_tools
def test_failing_tests_do_not_deploy_and_the_commit_is_never_retried(env):
    sha1 = env.push("v1")
    env.run()
    bad = env.push("BADTESTS")
    r = env.run()
    assert r.returncode == 1 and "tests failed" in r.stderr
    assert env.updates() == [sha1]
    assert env.read("deployed_sha") == sha1 and env.read("bad_sha") == bad
    assert env.git("rev-parse", "HEAD", cwd=env.src) == sha1  # the checkout was restored
    assert env.log_lines()[-1].endswith(" tests-failed")
    again = env.run()
    assert again.returncode != 0 and "marked bad" in again.stderr  # still visible as failed, but nothing is retried
    assert env.updates() == [sha1] and len(env.log_lines()) == 2


@needs_tools
def test_a_newer_commit_after_a_bad_one_deploys_and_clears_bad_sha(env):
    env.push("v1")
    env.run()
    env.push("BADTESTS")
    env.run()
    fixed = env.push("v3")
    assert env.run().returncode == 0
    assert env.read("deployed_sha") == fixed and env.read("bad_sha") is None


@needs_tools
def test_failing_health_rolls_back_to_the_previous_sha(env):
    sha1 = env.push("v1")
    env.run()
    bad = env.push("BADHEALTH")
    r = env.run()
    assert r.returncode == 1 and "rolled back" in r.stderr
    assert env.updates() == [sha1, bad, sha1]  # update ran for the new commit, then again for the old one
    assert env.read("deployed_sha") == sha1 and env.read("bad_sha") == bad
    assert env.git("rev-parse", "HEAD", cwd=env.src) == sha1
    assert env.log_lines()[-1].endswith(" rolled-back")
    assert env.run().returncode != 0 and len(env.updates()) == 3


@needs_tools
def test_failing_update_command_rolls_back_too(env):
    sha1 = env.push("v1")
    env.run()
    env.push("v2")
    r = env.run(CQB_UPDATE_CMD='git rev-parse HEAD >> "$CTL/updates"; ! grep -q v2 VERSION')
    assert r.returncode == 1
    assert env.read("deployed_sha") == sha1 and env.updates()[-1] == sha1


@needs_tools
def test_failure_of_the_very_first_deploy_has_nothing_to_roll_back_to(env):
    bad = env.push("BADHEALTH")
    r = env.run()
    assert r.returncode == 1 and "nothing to roll back" in r.stderr
    assert env.read("deployed_sha") is None and env.read("bad_sha") == bad
    assert env.log_lines()[-1].endswith(" failed-no-rollback")


@needs_tools
def test_non_fast_forward_is_refused_and_force_deploys_it(env):
    env.push("v1")
    env.run()
    sha2 = env.push("v2")
    env.run()
    env.git("reset", "-q", "--hard", "HEAD~1")
    rewritten = env.push("v2-rewritten", force=True)
    r = env.run()
    assert r.returncode == 2 and "REFUSED" in r.stderr and "--force" in r.stderr
    assert env.read("deployed_sha") == sha2 and env.read("bad_sha") is None
    assert len(env.updates()) == 2 and len(env.log_lines()) == 2  # nothing written, nothing deployed
    f = env.run("--force", rewritten)
    assert f.returncode == 0, f.stderr
    assert env.read("deployed_sha") == rewritten and env.updates()[-1] == rewritten


@needs_tools
def test_force_still_runs_tests_and_health_checks(env):
    sha1 = env.push("v1")
    env.run()
    bad = env.push("BADTESTS")
    env.run()
    r = env.run("--force", bad)  # --force overrides bad_sha, not the tests
    assert r.returncode == 1 and env.updates() == [sha1]
    r = env.run("--force", "no-such-commit")
    assert r.returncode == 1 and "unknown commit" in r.stderr


@needs_tools
def test_force_accepts_an_older_commit(env):
    sha1 = env.push("v1")
    env.push("v2")
    env.run()
    assert env.run("--force", sha1[:12]).returncode == 0
    assert env.read("deployed_sha") == sha1


@needs_tools
def test_concurrent_run_is_blocked_by_the_lock(env):
    env.push("v1")
    holder = subprocess.Popen(["flock", "-n", str(env.tmp / "lock"), "sleep", "30"], start_new_session=True)
    try:
        for _ in range(100):  # wait until the holder owns the lock
            if (env.tmp / "lock").exists():
                break
            time.sleep(0.05)
        time.sleep(0.2)
        r = env.run()
        assert r.returncode == 0 and "another run is in progress" in r.stderr
        assert env.updates() == [] and not env.src.exists()
    finally:
        os.killpg(holder.pid, 9)
        holder.wait()
    assert env.run().returncode == 0 and len(env.updates()) == 1


@needs_tools
def test_unreachable_origin_is_a_quiet_retry(env):
    sha = env.push("v1")
    env.run()
    shutil.move(str(env.origin), str(env.tmp / "gone.git"))
    r = env.run()
    assert r.returncode == 0 and "will retry" in r.stderr
    assert env.read("deployed_sha") == sha and len(env.updates()) == 1


@needs_tools
def test_status_prints_state_and_the_last_log_lines(env):
    sha1 = env.push("v1")
    env.run()
    bad = env.push("BADTESTS")
    env.run()
    r = env.run("--status")
    assert r.returncode == 0
    assert f"deployed_sha: {sha1}" in r.stdout and f"bad_sha:      {bad}" in r.stdout
    assert "tests-failed" in r.stdout and " deployed" in r.stdout


@needs_tools
def test_status_works_with_no_state_and_bad_arguments_are_rejected(env):
    assert env.run("--status").returncode == 0
    assert env.run("--force").returncode == 64
    assert env.run("--nonsense").returncode == 64


@needs_tools
def test_deploy_log_keeps_the_last_200_lines(env):
    env.push("v1")
    env.state.mkdir()
    (env.state / "deploy.log").write_text("".join(f"old {i}\n" for i in range(300)))
    env.run()
    lines = env.log_lines()
    assert len(lines) == 200 and lines[-1].endswith(" deployed") and lines[0] == "old 101"
