"""Read-only project files for the consultant: confinement, deny table, caps, redaction, flag, MCP wiring.

The Bridge runs as root and the reader is an LLM, so these tests are adversarial: every path trick, every
file type, every deny pattern, races (tested on the fd-walk logic, no threads), memory bounds, no leaks of
paths or contents into logs.
"""

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import shutil
import socket
import stat
import tracemalloc

import aiohttp
import pytest

from cqb import projectfs
from cqb.config import McpConfig
from cqb.mcp.server import McpServer
from cqb.mcp.tools import RESULT_MAX_BYTES, build_tools
from cqb.projectfs import DENY_PATTERNS, DenyList, FsError
from cqb.security import PrivateKeyLines
from cqb.services.context import ToolError
from cqb.services.project_files import CWD_CACHE_MAX, CWD_TTL, ProjectFiles
from test_mcp import AUTH, TOKEN, World, arun, call, rpc

OUTSIDE_TEXT = "OUTSIDE-FILE-CONTENT-MUST-NEVER-LEAK"


class Cwds:
    """The daemon reader as far as this feature sees it: thread id -> cwd (or None)."""

    def __init__(self, **cwds):
        self.map = dict(cwds)
        self.calls = []

    async def cwd(self, thread_id):
        self.calls.append(thread_id)
        return self.map.get(thread_id)


@pytest.fixture
def world(tmp_path):
    """tmp/proj (the project root), tmp/outside (a sibling with a file that must never be reachable)."""
    root, outside = tmp_path / "proj", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "o.txt").write_text(OUTSIDE_TEXT)
    (outside / "dir").mkdir()
    (outside / "dir" / "x.txt").write_text(OUTSIDE_TEXT)
    (root / "README.md").write_text("# hello\nsecond line\n")
    (root / "sub").mkdir()
    (root / "sub" / "a.txt").write_text("inside a\n")
    return root, outside


def service(root, clock=None, **cfg):
    src = Cwds(r1=str(root))
    kw = {"clock": clock} if clock else {}
    return ProjectFiles(McpConfig(project_files=True, **cfg), lambda r, s: None, src, **kw), src


def read(svc, path, **kw):
    return arun(svc.read({"root_id": "r1", "path": path, **kw}))


def listing(svc, path=None):
    return arun(svc.listing({"root_id": "r1", **({} if path is None else {"path": path})}))


def code_of(fn, *a, **kw):
    with pytest.raises(ToolError) as e:
        fn(*a, **kw)
    return e.value.code


def lines_of(chunk):
    return [] if not chunk.text else chunk.text.split("\n")


def wire(dto):
    return json.dumps(dataclasses.asdict(dto), ensure_ascii=False)


# --- happy path ------------------------------------------------------------------------

def test_reads_a_text_file_with_line_numbers(world):
    root, _ = world
    c = read(service(root)[0], "README.md")
    assert c.text == "1: # hello\n2: second line" and c.total_lines == 2 and not c.truncated and c.next_offset_lines is None
    assert (c.path, c.root_id, c.offset_lines, c.returned_lines, c.size) == ("README.md", "r1", 0, 2, 20)
    assert read(service(root)[0], "./sub//a.txt").text == "1: inside a"  # `.` and empty components are harmless


def test_file_shapes_empty_no_trailing_newline_crlf_and_one_line(world):
    root, _ = world
    svc, _ = service(root)
    (root / "empty").write_text("")
    (root / "nonl").write_text("a\nb")
    (root / "crlf").write_bytes(b"a\r\nb\r\n")
    (root / "one").write_text("x\n")
    assert (read(svc, "empty").text, read(svc, "empty").total_lines) == ("", 0)
    assert read(svc, "nonl").text == "1: a\n2: b" and read(svc, "nonl").total_lines == 2
    assert read(svc, "crlf").text == "1: a\n2: b" and read(svc, "crlf").total_lines == 2
    assert read(svc, "one").total_lines == 1


# --- path tricks -----------------------------------------------------------------------

BAD_PATHS = [
    "..", "../o.txt", "../outside/o.txt", "sub/../../outside/o.txt", "sub/..", "a/../../o.txt", "./../o.txt",
    "/etc/passwd", "/", "//etc/passwd", f"/{'a'}", "\\..\\outside\\o.txt", "sub\\a.txt", "..\\o.txt", "a\\b",
    "a\0b", "\0", "README.md\0.png", "sub/\0", "", "\n", "sub/a.txt\n", "sub/\ta", "\x1b[31m", "\x7f",
    "a" * 300, "sub/" + "b" * 256, "x" * 1025, "/".join(["d"] * 41),
    "．．/outside/o.txt", "‥/o.txt", "sub/．．/o.txt", "／etc／passwd", "\u202eexe.txt", "READ\u200bME.md", "\ud800",
    "..\u200b", ".\u200b.",
]


@pytest.mark.parametrize("path", BAD_PATHS)
def test_hostile_paths_are_refused_and_nothing_outside_is_read(world, path):
    root, _ = world
    svc, _ = service(root)
    for fn in (read, listing):
        with pytest.raises(ToolError) as e:
            fn(svc, path)
        assert e.value.code in {"invalid_path", "not_found", "denied", "not_a_file", "not_a_directory"}, (path, e.value.code)
        assert OUTSIDE_TEXT not in e.value.message


@pytest.mark.parametrize("path", ["%2e%2e/outside/o.txt", "%2e%2e%2foutside%2fo.txt", "..%2fo.txt", "%00", "sub/%2e%2e/a.txt",
                                  "....//o.txt", "...", ". ./o.txt", ".. /o.txt", "~/o.txt", "$HOME/x", "C:/o.txt",
                                  "outside/o.txt", "proj/README.md"])
def test_encoded_and_lookalike_paths_are_just_names_that_do_not_exist(world, path):
    root, _ = world
    assert code_of(read, service(root)[0], path) in {"not_found", "invalid_path"}


def test_non_string_paths_and_missing_path_are_invalid_arguments_or_paths(world):
    root, _ = world
    svc, _ = service(root)
    for bad in (123, ["README.md"], {"a": 1}, True, 1.5):
        assert code_of(read, svc, bad) == "invalid_path"
        assert code_of(listing, svc, bad) == "invalid_path"
    assert code_of(lambda: arun(svc.read({"root_id": "r1"}))) == "invalid_arguments"
    assert code_of(lambda: arun(svc.read({"root_id": "r1", "path": None}))) == "invalid_arguments"


def test_directory_versus_file_confusion(world):
    root, _ = world
    svc, _ = service(root)
    for p in ("sub", "sub/", ".", "./"):
        assert code_of(read, svc, p) == "not_a_file"
    assert code_of(listing, svc, "README.md") == "not_a_directory"
    assert code_of(listing, svc, "README.md/x") == "not_a_directory"
    assert code_of(read, svc, "README.md/x") == "not_a_directory"
    assert code_of(read, svc, "sub/nope.txt") == "not_found" and code_of(read, svc, "nodir/x.txt") == "not_found"
    assert [e.name for e in listing(svc, ".").entries] == ["sub", "README.md"] and listing(svc, "sub/").path == "sub"


# --- symlinks --------------------------------------------------------------------------

def test_every_symlink_is_refused_whatever_it_points_to(world):
    root, outside = world
    svc, _ = service(root)
    os.symlink(outside / "o.txt", root / "to_outside_file")
    os.symlink(outside / "dir", root / "to_outside_dir")
    os.symlink("README.md", root / "to_inside_file")  # even inside the root: refused, and it says so
    os.symlink("sub", root / "to_inside_dir")
    os.symlink("nowhere", root / "dangling")
    os.symlink("to_inside_file", root / "chain")
    os.symlink("..", root / "up")
    os.symlink("/", root / "slash")
    os.symlink(outside, root / "sub" / "deep_link")
    for p in ("to_outside_file", "to_outside_dir/x.txt", "to_inside_file", "to_inside_dir/a.txt", "dangling", "chain",
              "up/outside/o.txt", "slash/etc/passwd", "sub/deep_link/o.txt", "sub/deep_link/dir/x.txt"):
        assert code_of(read, svc, p) == "symlink_refused", p
    for p in ("to_outside_dir", "to_inside_dir", "up", "slash", "sub/deep_link", "to_outside_dir/dir"):
        assert code_of(listing, svc, p) == "symlink_refused", p
    types = {e.name: e.type for e in listing(svc, ".").entries}
    assert types["to_outside_file"] == types["to_inside_dir"] == types["dangling"] == "symlink" and types["sub"] == "dir"
    assert all(e.size is None for e in listing(svc, ".").entries if e.type == "symlink")


def test_the_root_itself_may_be_a_symlink_but_is_resolved_once(world, tmp_path):
    root, outside = world
    link = tmp_path / "rootlink"
    os.symlink(root, link)
    svc, _ = service(link)
    assert read(svc, "README.md").total_lines == 2
    os.symlink("/etc", tmp_path / "evil_root")  # resolves to a broad root: refused
    assert code_of(read, service(tmp_path / "evil_root")[0], "passwd") == "cwd_too_broad"


# --- races (the fd walk, no threads) ---------------------------------------------------

def test_directory_swapped_for_a_symlink_after_the_name_checks_is_refused(world):
    root, outside = world
    (outside / "sub").mkdir()
    (outside / "sub" / "a.txt").write_text(OUTSIDE_TEXT)
    deny = DenyList()
    parts = projectfs.split_relpath("sub/a.txt")
    deny.check(parts)  # the "check" passed ...
    shutil.rmtree(root / "sub")
    os.symlink(outside / "sub", root / "sub")  # ... then the directory is swapped
    fd = projectfs.open_root(str(root))
    try:
        with pytest.raises(FsError) as e:
            projectfs.read_file(fd, parts, deny, root_id="r1", offset=0, max_lines=10, budget=4096)
        assert e.value.code == "symlink_refused"
    finally:
        os.close(fd)


@pytest.mark.parametrize("swap", ["symlink_out", "fifo"])
def test_file_swapped_after_lstat_is_caught_by_nofollow_and_fstat(world, monkeypatch, swap):
    root, outside = world
    target = root / "a.txt"
    target.write_text("fine\n")
    real_stat, done = os.stat, []

    def stat_then_swap(path, *a, **k):
        result = real_stat(path, *a, **k)
        if path == "a.txt" and k.get("follow_symlinks") is False and not done:
            done.append(1)
            target.unlink()
            if swap == "fifo":
                os.mkfifo(target)
            else:
                os.symlink(outside / "o.txt", target)
        return result

    monkeypatch.setattr(projectfs.os, "stat", stat_then_swap)
    fd = projectfs.open_root(str(root))
    try:
        with pytest.raises(FsError) as e:
            projectfs.read_file(fd, ["a.txt"], DenyList(), root_id="r1", offset=0, max_lines=10, budget=4096)
        assert e.value.code == ("not_a_file" if swap == "fifo" else "symlink_refused")
    finally:
        os.close(fd)


def test_an_open_root_fd_stays_confined_when_the_directory_is_replaced_afterwards(world, tmp_path):
    root, outside = world
    fd = projectfs.open_root(str(root))
    try:
        moved = tmp_path / "moved"
        root.rename(moved)
        os.symlink(outside, root)  # the old path now leads outside; the fd still means the original directory
        c = projectfs.read_file(fd, ["README.md"], DenyList(), root_id="r1", offset=0, max_lines=10, budget=4096)
        assert "hello" in c.text and OUTSIDE_TEXT not in c.text
        with pytest.raises(FsError):
            projectfs.read_file(fd, ["o.txt"], DenyList(), root_id="r1", offset=0, max_lines=10, budget=4096)
    finally:
        os.close(fd)


def test_root_swapped_between_realpath_and_open_is_refused(world, monkeypatch):
    root, _ = world
    monkeypatch.setattr(projectfs.os, "readlink", lambda p: "/somewhere/else")
    assert pytest.raises(FsError, projectfs.open_root, str(root)).value.code == "cwd_unavailable"


# --- special files ---------------------------------------------------------------------

def test_fifo_socket_and_device_targets_are_never_read_or_blocked_on(world):
    root, _ = world
    svc, _ = service(root)
    os.mkfifo(root / "pipe")
    dfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    s = socket.socket(socket.AF_UNIX)
    try:
        s.bind(f"/proc/self/fd/{dfd}/sock")
        assert code_of(read, svc, "pipe") == "not_a_file"  # no writer: a blocking open would hang here
        assert code_of(read, svc, "sock") == "not_a_file"
        assert code_of(listing, svc, "pipe") == "not_a_directory"
        assert {e.name: e.type for e in listing(svc, ".").entries}["pipe"] == "other"
        devfd = os.open("/dev", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for name in ("null", "zero", "urandom"):
                assert pytest.raises(FsError, projectfs.open_file, devfd, name).value.code == "not_a_file"
        finally:
            os.close(devfd)
    finally:
        s.close()
        os.close(dfd)


# --- deny table ------------------------------------------------------------------------

SAMPLES = {  # one concrete name per pattern of the table; a new pattern without a sample fails test_every_pattern_has_a_sample
    ".git": ".git", ".env": ".env", ".env.*": ".env.production", "*.pem": "server.pem", "*.key": "tls.key",
    "*.p12": "a.p12", "*.pfx": "a.pfx", "*.jks": "a.jks", "id_rsa*": "id_rsa", "id_dsa*": "id_dsa.pub",
    "id_ecdsa*": "id_ecdsa", "id_ed25519*": "id_ed25519.pub", ".ssh": ".ssh", ".gnupg": ".gnupg", ".aws": ".aws",
    ".azure": ".azure", ".kube": ".kube", ".docker": ".docker", ".npmrc": ".npmrc", ".pypirc": ".pypirc",
    ".netrc": ".netrc", ".git-credentials": ".git-credentials", "secrets": "secrets", "secret": "secret",
    "*secret*": "my_secret_notes.txt", "*credential*": "gcp-credentials.json", "*token*": "api_tokens.txt",
    "*.keystore": "release.keystore", ".vault*": ".vault-token", "*.tfstate": "terraform.tfstate",
    "*.tfvars": "prod.tfvars", ".terraform": ".terraform", "*.sqlite": "app.sqlite", "*.sqlite3": "app.sqlite3",
    "*.db": "data.db", "*.db-wal": "data.db-wal", "*.db-shm": "data.db-shm", "node_modules": "node_modules",
    ".venv": ".venv", "venv": "venv", "__pycache__": "__pycache__", ".codex": ".codex",
}
ALLOWED_NAMES = ["README.md", ".gitignore", ".gitattributes", ".envrc", "environment.py", "keyboard.py", "monkey.py",
                 "dbm.py", "tok.py", "sec.txt", "venv2", "notes.txt", "pem.txt", "id_rs", "src", "docker-compose.yml",
                 ".github", ".editorconfig", "Makefile"]


def test_every_pattern_has_a_sample_and_the_sample_matches_it():
    assert set(SAMPLES) == set(DENY_PATTERNS) and len(DENY_PATTERNS) == len(set(DENY_PATTERNS))
    deny = DenyList()
    for pattern, name in SAMPLES.items():
        assert deny.denies(name), (pattern, name)


@pytest.mark.parametrize("pattern,name", sorted(SAMPLES.items()))
def test_denied_pattern_in_every_position_and_case(world, pattern, name):
    root, _ = world
    svc, _ = service(root)
    variants = [name, name.upper(), name.title(), name + ".", name + " "]
    for v in variants:
        # name exists as a file: denied; name does not exist: the SAME answer (no existence oracle)
        assert code_of(read, svc, v) == "denied", v
        assert code_of(read, svc, f"sub/{v}") == "denied"  # last component
        assert code_of(read, svc, f"{v}/x.txt") == "denied"  # a middle component
        assert code_of(read, svc, f"sub/{v}/deep/x.txt") == "denied"
        assert code_of(listing, svc, v) == "denied"
        assert code_of(listing, svc, f"sub/{v}") == "denied"
    (root / name).write_text("PLANTED-CONTENT")
    (root / "sub" / name).write_text("PLANTED-CONTENT")
    assert code_of(read, svc, name) == "denied"
    listed = listing(svc, ".")
    assert name not in [e.name for e in listed.entries] and listed.hidden >= 1
    assert name not in wire(listed) and "PLANTED-CONTENT" not in wire(listed)
    assert listing(svc, "sub").hidden == 1 and [e.name for e in listing(svc, "sub").entries] == ["a.txt"]


def test_denied_directory_contents_are_unreachable_and_denied_looks_the_same_whether_or_not_it_exists(world):
    root, _ = world
    svc, _ = service(root)
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[remote] url=https://u:p@h")
    answers = set()
    for p in (".git/config", ".git/HEAD", ".git/nothing/at/all", "nodir/.git/config"):
        with pytest.raises(ToolError) as e:
            read(svc, p)
        answers.add((e.value.code, e.value.message, tuple(e.value.extra)))
    assert answers == {("denied", "access to this path is denied", ())}  # existing or not, the same answer
    a = pytest.raises(ToolError, read, svc, ".env").value
    b = pytest.raises(ToolError, read, svc, "sub/deeper/.env").value
    assert (a.code, a.message, a.extra) == (b.code, b.message, b.extra) == ("denied", "access to this path is denied", {})


@pytest.mark.parametrize("name", ALLOWED_NAMES)
def test_the_table_is_not_wider_than_specified(world, name):
    root, _ = world
    (root / name).write_text("ok\n") if name not in ("src", ".github") else (root / name).mkdir()
    if name in ("src", ".github"):
        assert listing(service(root)[0], name).hidden == 0
    else:
        assert read(service(root)[0], name).text == "1: ok"


def test_unicode_aliases_of_denied_names_are_denied_or_invalid(world):
    root, _ = world
    svc, _ = service(root)
    for name in ("ＳＥＣＲＥＴ.txt", "．env", ".ＥＮＶ", "ﬁle.key", "Ｔｏｋｅｎ.json", ".g\u200bit", ".git\u200b", "TOKEN"):
        assert code_of(read, svc, name) in {"denied", "invalid_path"}, name


def test_extra_patterns_add_to_the_table_and_never_remove_from_it(world):
    root, _ = world
    (root / "plan.private").write_text("x")
    (root / "Notes").write_text("x")
    svc, _ = service(root, project_file_deny_extra=("*.PRIVATE", "notes"))
    assert code_of(read, svc, "plan.private") == "denied" and code_of(read, svc, "NOTES") == "denied"
    assert code_of(read, svc, ".env") == "denied" and code_of(read, svc, "x/.git/y") == "denied"
    assert listing(svc, ".").hidden == 2
    assert read(service(root)[0], "plan.private").text == "1: x"  # without the extra it is readable


def test_root_path_with_a_sensitive_component_is_refused(world, tmp_path):
    root, _ = world
    bad = tmp_path / ".ssh" / "proj"
    bad.mkdir(parents=True)
    assert code_of(read, service(bad)[0], "x") == "cwd_denied"


# --- binary and huge files -------------------------------------------------------------

@pytest.mark.parametrize("name,data", [
    ("nul", b"text\0text"), ("late_nul_in_window", b"a" * 8000 + b"\0"),
    ("controls31", b"a" * 690 + b"\x01" * 310), ("latin1", bytes(range(128, 256)) * 4), ("random", os.urandom(4096) + b"\0"),
    ("png", b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR" + b"\0" * 100),
])
def test_binary_files_are_refused(world, name, data):
    root, _ = world
    (root / name).write_bytes(data)
    assert code_of(read, service(root)[0], name) == "binary_or_too_large"


def test_text_that_only_looks_odd_is_readable(world):
    root, _ = world
    svc, _ = service(root)
    (root / "ru.txt").write_text("Привет, мир\n" * 2000)
    (root / "cut.txt").write_text("a" + "я" * 5000)  # the 8 KiB sniffing window cuts a multi-byte character
    (root / "ctl29.txt").write_bytes(b"a" * 710 + b"\x01" * 290)
    (root / "esc.txt").write_bytes(b"\x1b[31mred\x1b[0m\n\ttab\n")
    assert read(svc, "ru.txt").text.startswith("1: Привет, мир")
    assert read(svc, "cut.txt").returned_lines == 1
    assert "\x01" not in read(svc, "ctl29.txt").text  # control bytes are stripped by redaction
    assert read(svc, "esc.txt").returned_lines == 2


def test_nul_after_the_sniffing_window_never_reaches_the_output(world):
    root, _ = world
    (root / "late").write_bytes(b"a\n" * 6000 + b"x\0y\n" + b"b\n" * 10)
    c = read(service(root)[0], "late", offset_lines=5995, max_lines=20)
    assert "\0" not in c.text and "x" in c.text


def test_size_limit_is_two_mib_exactly(world):
    root, _ = world
    svc, _ = service(root)
    (root / "ok.txt").write_bytes((b"x" * 99 + b"\n") * 20971 + b"y" * 52)  # 2 MiB exactly
    assert (root / "ok.txt").stat().st_size == 2 * 1024 * 1024
    c = read(svc, "ok.txt")
    assert c.size == 2 * 1024 * 1024 and c.truncated and c.returned_lines > 100
    (root / "big.txt").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    assert code_of(read, svc, "big.txt") == "binary_or_too_large"


def test_a_file_that_lies_about_its_size_is_stopped_by_the_scan(world, monkeypatch):
    root, _ = world
    (root / "grows.txt").write_bytes(b"x\n" * (1024 * 1024 + 10))  # 2 MiB + 20 bytes on disk
    real_fstat = os.fstat

    class Lie:
        def __init__(self, st):
            self.st_mode, self.st_size = st.st_mode, 100

    monkeypatch.setattr(projectfs.os, "fstat", lambda fd: Lie(real_fstat(fd)))
    fd = projectfs.open_root(str(root))
    try:
        with pytest.raises(FsError) as e:
            projectfs.read_file(fd, ["grows.txt"], DenyList(), root_id="r1", offset=10 ** 6, max_lines=5, budget=4096)
        assert e.value.code == "binary_or_too_large"
    finally:
        os.close(fd)


# --- paging and caps -------------------------------------------------------------------

def test_paging_reassembles_the_file_exactly(world):
    root, _ = world
    svc, _ = service(root)
    original = [f"line number {i}" for i in range(1, 1001)]
    (root / "long.txt").write_text("\n".join(original) + "\n")
    got, offset, pages = [], 0, 0
    while offset is not None:
        c = read(svc, "long.txt", offset_lines=offset)
        pages += 1
        assert c.offset_lines == offset and c.total_lines == 1000
        got += [ln.split(": ", 1) for ln in lines_of(c)]
        assert [int(n) for n, _ in got[-c.returned_lines:]] == list(range(offset + 1, offset + 1 + c.returned_lines))
        assert (c.next_offset_lines is None) == (not c.truncated)
        offset = c.next_offset_lines
        assert pages < 20
    assert [t for _, t in got] == original and pages == 4  # 300 + 300 + 300 + 100


def test_max_lines_offset_validation_and_end_of_file(world):
    root, _ = world
    svc, _ = service(root)
    (root / "t.txt").write_text("a\nb\nc\n")
    c = read(svc, "t.txt", max_lines=2)
    assert (c.text, c.truncated, c.next_offset_lines, c.total_lines) == ("1: a\n2: b", True, 2, 3)
    c = read(svc, "t.txt", offset_lines=2, max_lines=1)  # the last line exactly fills the page: not truncated
    assert (c.text, c.truncated, c.next_offset_lines) == ("3: c", False, None)
    c = read(svc, "t.txt", offset_lines=3)
    assert (c.text, c.returned_lines, c.truncated, c.total_lines) == ("", 0, False, 3)
    assert read(svc, "t.txt", offset_lines=10 ** 9).returned_lines == 0
    for bad in ({"offset_lines": -1}, {"offset_lines": "1"}, {"offset_lines": True}, {"offset_lines": 1.5}, {"max_lines": 0},
                {"max_lines": -5}, {"max_lines": "9"}, {"max_lines": False}):
        assert code_of(read, svc, "t.txt", **bad) == "invalid_arguments", bad
    assert code_of(read, svc, "t.txt", max_lines=1001) == "limit_exceeded"
    assert read(svc, "t.txt", max_lines=1000, offset_lines=None).returned_lines == 3


@pytest.mark.parametrize("budget", [1024, 4096, 16384, 65536])
def test_output_never_exceeds_the_byte_budget(world, budget):
    root, _ = world
    svc, _ = service(root, project_file_max_bytes=budget)
    (root / "w.txt").write_text(("word " * 20 + "\n") * 2000)
    (root / "q.txt").write_text('"' * 100 + "\n" + ("\\\" \n" * 5000))  # escape-heavy: JSON doubles it
    for name in ("w.txt", "q.txt"):
        c = read(svc, name, max_lines=1000)
        assert len(json.dumps(c.text, ensure_ascii=False).encode()) - 2 <= budget and c.truncated
        assert c.next_offset_lines == c.returned_lines >= 1


def test_default_cap_is_16_kib_and_300_lines(world):
    root, _ = world
    svc, _ = service(root)
    (root / "w.txt").write_text(("x" * 98 + "\n") * 500)
    c = read(svc, "w.txt")
    assert 150 < c.returned_lines < 300 and len(json.dumps(c.text).encode()) - 2 <= 16384 and c.truncated  # bytes bind first
    (root / "s.txt").write_text("y\n" * 500)
    assert read(svc, "s.txt").returned_lines == 300  # the line cap binds


def test_a_huge_single_line_is_cut_and_paging_still_progresses(world):
    root, _ = world
    svc, _ = service(root, project_file_max_bytes=1024)
    (root / "min.js").write_bytes(b"ab " * 500_000 + b"\nsecond\n")
    c = read(svc, "min.js")
    assert c.lines_cut and c.returned_lines >= 1 and c.text.startswith("1: ab ab") and len(c.text) <= 1024
    assert read(svc, "min.js", offset_lines=1).text == "2: second"
    (root / "red.js").write_text("token=a," * 10000 + "\nz\n")  # redaction expands tiny matches: the budget still holds
    c = read(svc, "red.js")
    assert len(json.dumps(c.text).encode()) - 2 <= 1024 and c.redacted and c.lines_cut


def test_total_lines_is_only_counted_when_cheap(world):
    root, _ = world
    svc, _ = service(root)
    (root / "mid.txt").write_text("l\n" * 1000)  # 2 KB: counted
    (root / "big.txt").write_text("l\n" * 400_000)  # 800 KB: the scan stops when the page is full
    assert read(svc, "mid.txt").total_lines == 1000
    assert read(svc, "big.txt").total_lines is None
    assert read(svc, "big.txt", offset_lines=399_990, max_lines=100).total_lines == 400_000  # EOF seen anyway


# --- memory ----------------------------------------------------------------------------

def test_reads_are_small_and_stop_once_the_page_is_full(world, monkeypatch):
    root, _ = world
    svc, _ = service(root)
    (root / "big.txt").write_text(("x" * 78 + "\n") * 25_000)  # ~2 MB
    sizes = []
    real_read = os.read

    def spy(fd, n):
        data = real_read(fd, n)
        sizes.append(len(data))
        return data

    monkeypatch.setattr(projectfs.os, "read", spy)
    c = read(svc, "big.txt")
    assert c.truncated and max(sizes) <= projectfs.CHUNK
    assert sum(sizes) <= 16384 + 3 * projectfs.CHUNK  # the cap plus a small slack, not the 2 MB file


def test_peak_python_memory_stays_small_even_for_a_two_mib_single_line(world):
    root, _ = world
    svc, _ = service(root)
    (root / "one.txt").write_bytes(b"a" * (2 * 1024 * 1024 - 1))
    (root / "many.txt").write_bytes(b"ab\n" * (600 * 1024))
    tracemalloc.start()
    try:
        for name in ("one.txt", "many.txt"):
            read(svc, name)
            read(svc, name, offset_lines=600_000 if name == "many.txt" else 0)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 512 * 1024, peak


def test_no_file_descriptors_leak_on_success_or_failure(world):
    root, outside = world
    svc, _ = service(root)
    os.symlink(outside, root / "lnk")
    os.mkfifo(root / "pipe")
    (root / "bin").write_bytes(b"\0")
    before = sorted(os.listdir("/proc/self/fd"))
    for _ in range(3):
        for p in ("README.md", "sub/a.txt", "lnk/o.txt", "pipe", "bin", "nope", "sub/nope/x", ".env", "../x", "sub"):
            with contextlib.suppress(ToolError):
                read(svc, p)
            with contextlib.suppress(ToolError):
                listing(svc, p)
    assert sorted(os.listdir("/proc/self/fd")) == before


# --- listing ---------------------------------------------------------------------------

def test_listing_is_one_level_sorted_dirs_first_with_sizes_and_hidden_counted(world):
    root, outside = world
    svc, _ = service(root)
    for n in ("b.txt", "A.txt", "c.py"):
        (root / n).write_text("12345")
    for n in (".git", ".env", "secret.txt", "id_rsa", "node_modules", "token.json", "app.db"):
        (root / n).write_text("x") if n not in (".git", "node_modules") else (root / n).mkdir()
    (root / "zdir").mkdir()
    (root / "Adir").mkdir()
    (root / "sub" / "nested").mkdir()
    os.symlink(outside, root / "lnk")
    out = listing(svc)
    assert [(e.name, e.type, e.size) for e in out.entries] == [
        ("Adir", "dir", None), ("sub", "dir", None), ("zdir", "dir", None),
        ("A.txt", "file", 5), ("b.txt", "file", 5), ("c.py", "file", 5), ("lnk", "symlink", None), ("README.md", "file", 20),
    ]
    assert out.hidden == 7 and not out.truncated and out.total == 8 and out.path == "." and out.root_id == "r1"
    assert "nested" not in wire(out) and "o.txt" not in wire(out)  # no recursion, nothing from outside
    assert [e.name for e in listing(svc, "sub").entries] == ["nested", "a.txt"]


def test_listing_caps_entries_and_says_so(world):
    root, _ = world
    (root / "many").mkdir()
    for i in range(250):
        (root / "many" / f"f{i:03d}.txt").write_text("x")
    out = listing(service(root)[0], "many")
    assert len(out.entries) == 200 and out.truncated and out.total == 250 and out.entries[0].name == "f000.txt"
    out = listing(service(root, project_dir_max_entries=10)[0], "many")
    assert len(out.entries) == 10 and out.truncated
    out = listing(service(root, project_file_max_bytes=1024)[0], "many")  # the byte budget binds first
    assert 0 < len(out.entries) < 40 and out.truncated and len(wire(out)) < 2048


def test_listing_scan_is_bounded_and_hidden_names_count_toward_it(world, monkeypatch):
    root, _ = world
    monkeypatch.setattr(projectfs, "SCAN_LIMIT", 30)
    (root / "d").mkdir()
    for i in range(40):
        (root / "d" / (f"f{i}" if i % 2 else f"token{i}")).write_text("x")
    out = listing(service(root)[0], "d")
    assert out.truncated and out.total + out.hidden == 30


def test_hostile_file_names_are_listed_safely(world):
    root, _ = world
    os.mkdir(root / "weird")
    raw = os.fsencode(root / "weird")
    for name in (b"bad\xff\xfename.txt", b"new\nline.txt", b"esc\x1b[31m.txt", "rtl\u202eexe.txt".encode(), b"nul_free\x01.txt"):
        with open(raw + b"/" + name, "w") as f:
            f.write("x")
    out = listing(service(root)[0], "weird")
    text = wire(out)  # must be serializable: no lone surrogates
    text.encode()
    assert len(out.entries) == 5
    for e in out.entries:
        assert all(ord(c) >= 32 and c != "\x7f" and c not in "\u202e" for c in e.name), repr(e.name)


def test_listing_outside_and_denied_paths(world):
    root, _ = world
    svc, _ = service(root)
    assert code_of(listing, svc, "..") == "invalid_path" and code_of(listing, svc, "/etc") == "invalid_path"
    assert code_of(listing, svc, "sub/.git") == "denied" and code_of(listing, svc, "nope") == "not_found"


# --- the root: the thread's cwd --------------------------------------------------------

@pytest.mark.parametrize("cwd", ["/", "/root", "/home", "/etc", "/var", "/usr", "/opt", "/srv", "/etc/ssh", "/proc", "/sys",
                                 "/dev", "/boot", "/tmp", "/var/tmp", "/proc/self", "/usr/", "/usr/.", "//", "/etc/../etc"])
def test_broad_or_system_roots_are_refused(cwd):
    svc = ProjectFiles(McpConfig(project_files=True), lambda r, s: None, Cwds(r1=cwd))
    assert code_of(read, svc, "x") in {"cwd_too_broad", "cwd_unavailable"}
    assert code_of(listing, svc) in {"cwd_too_broad", "cwd_unavailable"}
    for c in ("/", "/root", "/home", "/etc", "/var", "/usr", "/opt", "/srv", "/srv/projects"):
        if cwd == c:
            assert code_of(read, svc, "x") == "cwd_too_broad"


def test_root_guard_rules_on_strings():
    for ok in ("/srv/projects/app", "/root/proj", "/home/u/proj", "/opt/app", "/var/lib/app", "/tmp/x"):
        projectfs._check_root(ok)
    for bad in ("/", "/srv", "/home/claude", "/root", "/etc/nginx", "/proc/1", "/run/user/0", "/x"):
        with pytest.raises(FsError) as e:
            projectfs._check_root(bad)
        assert e.value.code == "cwd_too_broad", bad
    with pytest.raises(FsError) as e:
        projectfs._check_root("/root/.aws/proj")
    assert e.value.code == "cwd_denied"


def test_the_home_directory_is_too_broad(world, monkeypatch):
    root, _ = world
    monkeypatch.setenv("HOME", str(root))
    assert code_of(read, service(root)[0], "README.md") == "cwd_too_broad"


def test_missing_or_unusable_cwd_is_a_structured_error(world, tmp_path):
    root, _ = world
    for cwd in (None, "", "relative/dir", str(tmp_path / "gone"), str(root / "README.md"), "x\0y", 7, ["/a/b"]):
        svc = ProjectFiles(McpConfig(project_files=True), lambda r, s: None, Cwds(r1=cwd))
        for fn in (read, listing):
            with pytest.raises(ToolError) as e:
                fn(svc, "README.md") if fn is read else fn(svc)
            assert e.value.code == "cwd_unavailable" and "README" not in e.value.message and str(tmp_path) not in e.value.message


def test_cwd_is_cached_briefly_and_the_cache_is_bounded(world):
    root, _ = world
    now = [1000.0]
    src = Cwds(r1=str(root), **{f"x{i}": str(root) for i in range(200)})
    svc = ProjectFiles(McpConfig(project_files=True), lambda r, s: None, src, clock=lambda: now[0])
    for _ in range(3):
        read(svc, "README.md")
    assert src.calls == ["r1"]
    now[0] += CWD_TTL + 1
    read(svc, "README.md")
    assert src.calls == ["r1", "r1"]
    for i in range(200):
        arun(svc.read({"root_id": f"x{i}", "path": "README.md"}))
    assert len(svc._cwds) == CWD_CACHE_MAX and "r1" not in svc._cwds


# --- redaction -------------------------------------------------------------------------

PEM = ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun\n"
       "VTLw7onLRnrq0/IzW7yWR7QkrmBL7jTKEn5u+qKhbwKfBstIs+bMY2Zkp18gnTxK\n-----END RSA PRIVATE KEY-----")


def test_planted_secrets_in_a_normal_file_are_redacted(world):
    root, _ = world
    svc, _ = service(root)
    secrets = ["ghp_abcdefghijklmnopqrstuvwxyz0123456789", "hunter2hunter2", "sk-abcdefghijklmnopqrstuvwx",
               "abcdef123456789xyz", "AKIAABCDEFGHIJKLMNOP", "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun",
               "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0NTY3.SflKxwRJSMeKKF2QT4fw", "s3cr3tvalue"]
    (root / "config.py").write_text(
        'GITHUB_TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"\npassword=hunter2hunter2\n'
        "use sk-abcdefghijklmnopqrstuvwx here\nAuthorization: Bearer abcdef123456789xyz\naws AKIAABCDEFGHIJKLMNOP\n"
        + PEM + "\nnote: eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0NTY3.SflKxwRJSMeKKF2QT4fw\napi_key: 's3cr3tvalue'\nmax_tokens=4096\nend\n")
    c = read(svc, "config.py")
    assert c.redacted and "max_tokens=4096" in c.text and c.text.endswith("end")
    for s in secrets:
        assert s not in c.text, s
    assert wire(c).count("REDACTED") >= 7


def test_a_page_that_starts_inside_a_private_key_hides_the_key_body(world):
    root, _ = world
    svc, _ = service(root)
    (root / "deploy.txt").write_text("intro\n" + PEM + "\nafter\n")
    for offset in (0, 1, 2, 3):
        c = read(svc, "deploy.txt", offset_lines=offset)
        assert "MIIEowIBAAKC" not in c.text and "VTLw7onLR" not in c.text, offset
    c = read(svc, "deploy.txt", offset_lines=4)
    assert c.text == "5: [REDACTED PRIVATE KEY]\n6: after"
    assert read(svc, "deploy.txt", offset_lines=5).text == "6: after"
    (root / "open.txt").write_text("-----BEGIN PRIVATE KEY-----\nAAAA\nBBBB\n")  # never terminated: hidden to the end
    assert "AAAA" not in read(svc, "open.txt", offset_lines=1).text


def test_private_key_line_tracker():
    t = PrivateKeyLines()
    got = [t.hides(x) for x in ("a", "-----BEGIN EC PRIVATE KEY-----", "body", "-----END EC PRIVATE KEY-----", "b",
                                "-----BEGIN PRIVATE KEY----- x -----END PRIVATE KEY-----", "c")]
    assert got == [False, True, True, True, False, True, False]


# --- the MCP surface: flag, listing, scope, results, logs ------------------------------

@contextlib.asynccontextmanager
async def mcp_world(conn, root, enabled=True, **cfg):
    w = World(conn)
    src = Cwds(r1=str(root), r2=str(root), c1=str(root))
    w.files = ProjectFiles(McpConfig(project_files=enabled, **cfg), w.ctx.check_scope, src)
    w.src = src
    w.tools = build_tools(w.ctx, w.files)
    w.scopes = {"cqb-r1": ("s1", "r1")}
    srv = McpServer(TOKEN, w.tools, w.scopes.get, lambda: None, port=0)
    await srv.start()
    async with aiohttp.ClientSession() as http:
        w.http, w.srv, w.url = http, srv, f"http://127.0.0.1:{srv.port}"
        try:
            yield w
        finally:
            await srv.stop()


async def names(w):
    _, body = await rpc(w, "tools/list")
    return [t["name"] for t in body["result"]["tools"]]


OLD_TOOLS = ["get_thread_overview", "list_agents", "get_recent_events", "read_event", "get_questions"]


def test_tools_are_listed_only_when_the_flag_is_on(conn, world):
    root, _ = world

    async def main():
        async with mcp_world(conn, root, enabled=False) as w:
            assert await names(w) == OLD_TOOLS
            _, body = await rpc(w, "tools/list")
            assert "project" not in json.dumps(body)
        async with mcp_world(conn, root, enabled=True) as w:
            assert await names(w) == OLD_TOOLS + ["read_project_file", "list_project_dir"]
            _, body = await rpc(w, "tools/list")
            tools = {t["name"]: t for t in body["result"]["tools"]}
            for t in ("read_project_file", "list_project_dir"):
                assert tools[t]["annotations"]["readOnlyHint"] is True and "root_id" in tools[t]["inputSchema"]["properties"]
            assert set(tools["read_project_file"]["inputSchema"]["properties"]) == {
                "root_id", "source_id", "path", "offset_lines", "max_lines"}
            assert set(tools["list_project_dir"]["inputSchema"]["properties"]) == {"root_id", "source_id", "path"}
    arun(main())


def test_calls_are_refused_with_disabled_when_the_flag_is_off_and_nothing_is_read(conn, world):
    root, _ = world

    async def main():
        async with mcp_world(conn, root, enabled=False) as w:
            for tool in ("read_project_file", "list_project_dir"):
                for args in ({"root_id": "r1", "path": "README.md"}, {"root_id": "zzz"}, {}, {"root_id": "r1", "evil": 1}):
                    body, err = await call(w, tool, **args)
                    assert err and body["error"] == "disabled", (tool, args, body)
            assert w.src.calls == [] and w.history.calls == []
    arun(main())


def test_default_config_is_off_and_tools_without_a_service_are_disabled(conn):
    assert McpConfig().project_files is False
    tools = build_tools(None)  # test_deploy builds the names this way
    assert {"read_project_file", "list_project_dir"} <= set(tools) and not tools["read_project_file"].listed

    async def main():
        with pytest.raises(ToolError) as e:
            await tools["read_project_file"].handler({"root_id": "r1", "path": "x"})
        assert e.value.code == "disabled"
    arun(main())


def test_root_id_is_validated_by_the_server_too(conn, world):
    root, _ = world

    async def main():
        async with mcp_world(conn, root) as w:
            for tool, extra in (("read_project_file", {"path": "README.md"}), ("list_project_dir", {})):
                for args in ({"root_id": "r3"}, {"root_id": "c1"}, {"root_id": "zzz"}, {"root_id": "r1", "source_id": "other"},
                             {"root_id": "../r1"}, {"root_id": 5}):
                    body, err = await call(w, tool, **extra, **args)
                    assert err and body["error"] in {"scope_denied", "invalid_arguments"}, (tool, args, body)
                body, err = await call(w, tool, **extra)
                assert err and body["error"] == "invalid_arguments"  # no root_id: the hook did not run
                body, err = await call(w, tool, **extra, root_id="r1", source_id="s1")
                assert not err and body["root_id"] == "r1"
            assert w.src.calls.count("r2") == 0  # a denied scope never reaches the daemon for its cwd
    arun(main())


def test_unknown_argument_names_are_refused_by_the_server(conn, world):
    root, _ = world

    async def main():
        async with mcp_world(conn, root) as w:
            for tool, bad in (("read_project_file", {"path": "README.md", "max_bytes": 99999}),
                              ("read_project_file", {"path": "README.md", "cwd": "/"}),
                              ("list_project_dir", {"offset_lines": 1}), ("list_project_dir", {"recursive": True})):
                body, err = await call(w, tool, root_id="r1", **bad)
                assert err and body["error"] == "invalid_arguments", (tool, bad, body)
    arun(main())


def test_results_over_mcp_round_trip_and_stay_below_the_result_guard(conn, world):
    root, _ = world
    (root / "q.txt").write_text('"' * 5000 + "\n")  # worst case for JSON escaping, at the largest allowed budget
    (root / "q2.txt").write_text(('"\\' * 100 + "\n") * 2000)

    async def main():
        async with mcp_world(conn, root, project_file_max_bytes=65536) as w:
            for name in ("q.txt", "q2.txt"):
                body, err = await call(w, "read_project_file", root_id="r1", path=name, max_lines=1000)
                assert not err and body["returned_lines"] >= 1, body
            _, raw = await rpc(w, "tools/call", {"name": "read_project_file",
                                                 "arguments": {"root_id": "r1", "path": "q2.txt", "max_lines": 1000}})
            assert len(raw["result"]["content"][0]["text"].encode()) < RESULT_MAX_BYTES
            body, err = await call(w, "read_project_file", root_id="r1", path="sub/a.txt")
            assert body["text"] == "1: inside a" and body["total_lines"] == 1 and body["truncated"] is False
            body, err = await call(w, "list_project_dir", root_id="r1")
            assert {"sub", "README.md"} <= {e["name"] for e in body["entries"]} and body["hidden"] == 0
            body, err = await call(w, "read_project_file", root_id="r1", path=".env")
            assert err and body == {"error": "denied", "message": "access to this path is denied"}
    arun(main())


def test_secrets_never_pass_the_mcp_result_even_in_names_and_errors(conn, world):
    root, _ = world
    (root / "x.txt").write_text("password=hunter2hunter2\n")
    (root / "api_key=ghp_abcdefghijklmnopqrstuvwxyz0123456789.txt").write_text("x")

    async def main():
        async with mcp_world(conn, root) as w:
            body, _ = await call(w, "read_project_file", root_id="r1", path="x.txt")
            assert "hunter2hunter2" not in json.dumps(body) and body["redacted"] is True
            body, _ = await call(w, "list_project_dir", root_id="r1")
            assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in json.dumps(body)
    arun(main())


def test_logs_carry_counts_and_codes_only(conn, world, caplog):
    root, outside = world
    (root / "private-notes.txt").write_text("CONTENT-MARKER-12345\n")
    (root / ".env").write_text("CONTENT-MARKER-DENIED\n")
    os.symlink(outside, root / "linkname")

    async def main():
        async with mcp_world(conn, root) as w:
            for path in ("private-notes.txt", ".env", "linkname/o.txt", "missing-file-name.txt", "../escape-name", "sub"):
                await call(w, "read_project_file", root_id="r1", path=path)
                await call(w, "list_project_dir", root_id="r1", path=path)
            await call(w, "list_project_dir", root_id="r1")
    with caplog.at_level(logging.DEBUG):
        arun(main())
    text = caplog.text
    assert "project file" in text and "code=" in text and "lines=" in text  # something was logged ...
    for forbidden in ("CONTENT-MARKER", "private-notes", ".env", "linkname", "missing-file-name", "escape-name", "o.txt",
                      str(root), "hunter2"):
        assert forbidden not in text, forbidden  # ... but no content and no path
