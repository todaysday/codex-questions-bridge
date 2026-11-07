"""Compaction counter: `rg -c` / `grep -c` in a child, anchored to the start of the line. Real tools on tmp fixtures."""

import asyncio
import json
import os
import shutil
import sys
import time

import pytest

from cqb.codex import rollout_count
from cqb.codex.rollout_count import CompactionCounter

TOOLS = [pytest.param(t, marks=pytest.mark.skipif(shutil.which(t) is None, reason=f"{t} is not installed"))
         for t in ("rg", "grep")]


def run(coro):
    return asyncio.run(coro)


def record(kind="response_item", ordinal=1, **payload):
    head = {"timestamp": "2026-11-07T12:48:30.464Z"}
    if ordinal is not None:
        head["ordinal"] = ordinal
    return json.dumps({**head, "type": kind, "payload": payload or {"x": 1}}, separators=(",", ":"), ensure_ascii=False)


def compacted(ordinal=1):
    return record("compacted", ordinal, message="", replacement_history=[{"type": "message", "text": "kept"}])


@pytest.fixture
def home(tmp_path):
    (tmp_path / "sessions" / "2026" / "11").mkdir(parents=True)
    (tmp_path / "archived_sessions").mkdir()
    return tmp_path


def write(home, lines, name="r.jsonl", sub="sessions/2026/11", end="\n"):
    p = home / sub / name
    p.write_bytes(("\n".join(lines) + end).encode())
    return str(p)


def counter(home, tool, **kw):
    return CompactionCounter(str(home), tools=(tool,), **kw)


@pytest.mark.parametrize("tool", TOOLS)
def test_counts_top_level_compacted_records_with_or_without_ordinal_and_none_gives_zero(home, tool):
    c = counter(home, tool)
    p = write(home, [record(), compacted(1), record(ordinal=2), compacted(3), compacted(None), record()])
    assert run(c(p)) == 3
    assert run(c(write(home, [record(), record(), "{}"], "z.jsonl"))) == 0  # no match: rg prints nothing, the exit code says 0
    assert run(c(write(home, [], "e.jsonl"))) == 0


@pytest.mark.parametrize("tool", TOOLS)
def test_nested_mentions_in_messages_are_not_counted(home, tool):
    quoted = record("response_item", 2, text='{"timestamp":"t","type":"compacted"} and "type":"compacted"')
    nested = json.dumps({"timestamp": "t", "ordinal": 3, "type": "response_item",
                         "payload": {"timestamp": "t", "type": "compacted", "items": [{"type": "compacted"}]}},
                        separators=(",", ":"))
    indented = ' ' + compacted(4)  # not a record start
    event = record("event_msg", 5, type="item_completed", item={"type": "contextCompaction"})  # the companion event
    p = write(home, [quoted, nested, indented, event, compacted(6)])
    assert run(counter(home, tool)(p)) == 1  # only the one real record


@pytest.mark.parametrize("tool", TOOLS)
def test_a_stray_nul_byte_and_a_huge_line_do_not_hide_matches(home, tool):
    huge = record("response_item", 2, blob="я" * 3_000_000)  # one line of 6 MB: streamed, never loaded by us
    p = write(home, [compacted(1), "junk\x00junk", huge, compacted(2), "\x00" + compacted(3)])
    assert run(counter(home, tool)(p)) == 2  # the line that starts with NUL is not a record start; the others are
    q = write(home, [compacted(1) + "\x00trailing", compacted(2)], "n.jsonl")
    assert run(counter(home, tool)(q)) == 2  # NUL after the match start: -a still sees the line


@pytest.mark.parametrize("tool", TOOLS)
def test_an_unchanged_file_is_not_scanned_again_and_a_changed_one_is(home, tool, monkeypatch):
    c = counter(home, tool)
    spawned = []
    real = c._count

    async def spy(path):
        spawned.append(path)
        return await real(path)
    monkeypatch.setattr(c, "_count", spy)
    p = write(home, [compacted(1)])
    assert run(c(p)) == 1 and run(c(p)) == 1 and len(spawned) == 1  # the second one came from the cache
    with open(p, "a") as f:
        f.write(compacted(2) + "\n")  # size and mtime change
    assert run(c(p)) == 2 and len(spawned) == 2
    st = os.stat(p)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))  # same size, newer mtime: scanned again
    assert run(c(p)) == 2 and len(spawned) == 3


def test_the_cache_is_bounded_and_drops_the_oldest(home, monkeypatch):
    monkeypatch.setattr(rollout_count, "CACHE_MAX", 3)
    c = CompactionCounter(str(home), argv=[sys.executable, "-c", "print(7)"])
    for i in range(5):
        assert run(c(write(home, [record()], f"f{i}.jsonl"))) == 7
    assert len(c._cache) == 3 and {k[0].rsplit("/", 1)[1] for k in c._cache} == {"f2.jsonl", "f3.jsonl", "f4.jsonl"}


def test_paths_outside_the_codex_directories_or_not_regular_files_are_refused_before_any_spawn(home, tmp_path):
    spawned = []

    class Spy(CompactionCounter):
        async def _count(self, real):
            spawned.append(real)
            return 1
    c = Spy(str(home))
    outside = tmp_path / "other.jsonl"
    outside.write_text(compacted() + "\n")
    inside = write(home, [compacted()])
    (home / "sessions" / "link.jsonl").symlink_to(outside)  # a link out of the directory
    (home / "sessions" / "dir.jsonl").mkdir()
    os.mkfifo(home / "sessions" / "fifo.jsonl")
    for bad in (str(outside), str(home / "sessions" / "link.jsonl"), str(home / "sessions" / ".." / "other.jsonl"),
                str(home / "sessions"), str(home / "sessions" / "dir.jsonl"), str(home / "sessions" / "fifo.jsonl"),
                str(home / "sessions" / "missing.jsonl"), "", "/etc/passwd", str(home / "sessions-evil" / "x")):
        assert run(c(bad)) is None, bad
    assert spawned == [] and run(c(inside)) == 1 and spawned == [os.path.realpath(inside)]  # the checked real path is used


def test_the_command_has_no_shell_the_path_after_double_dash_and_a_clean_environment(home):
    c = CompactionCounter(str(home))
    argv = c._command()
    assert argv is not None and argv[-1] == "--" and rollout_count.PATTERN in argv and "-a" in argv
    assert os.path.basename(argv[0]) in ("rg", "grep") and not any(" " in a for a in argv[:1])
    env = CompactionCounter(str(home), argv=[sys.executable, "-c", (
        "import os; print(1 if os.environ.get('LC_ALL') == 'C' and 'HOME' not in os.environ else 0)")])
    assert run(env(write(home, [record()]))) == 1  # LC_ALL=C, and nothing else from our environment leaks in


def test_the_executable_is_resolved_once_rg_first_and_grep_as_the_fallback(home, monkeypatch):
    if shutil.which("grep") is None:
        pytest.skip("grep is not installed")
    calls = []
    real = shutil.which
    monkeypatch.setattr(rollout_count.shutil, "which", lambda name: calls.append(name) or real(name))
    c = CompactionCounter(str(home), tools=("rg-surely-missing", "grep"))  # rg is missing: grep is the fallback
    p = write(home, [compacted(1), compacted(2)])
    assert run(c(p)) == 2 and os.path.basename(c._argv[0]) == "grep"
    run(c(write(home, [compacted(1)], "b.jsonl")))
    assert calls == ["rg-surely-missing", "grep"]  # resolved at the first use only
    both = CompactionCounter(str(home))
    both._command()
    assert os.path.basename(both._argv[0]) == ("rg" if real("rg") else "grep")  # rg is preferred when installed


def test_no_tool_at_all_a_missing_executable_and_junk_results_are_unknown(home):
    p = write(home, [compacted()])
    assert run(CompactionCounter(str(home), tools=("surely-not-installed-1", "surely-not-installed-2"))(p)) is None
    assert run(CompactionCounter(str(home), argv=["/nonexistent/bin/rg"])(p)) is None
    for code in ("import sys; sys.exit(2)", "import sys; print(5); sys.exit(3)", "print('abc')", "print('9' * 100000)", "print(-1 * 5 + 5, end='x')"):
        c = CompactionCounter(str(home), argv=[sys.executable, "-c", code])
        assert run(c(p)) is None, code
    zero = CompactionCounter(str(home), argv=[sys.executable, "-c", "import sys; sys.exit(1)"])  # exit 1: no match
    assert run(zero(p)) == 0
    assert run(CompactionCounter(str(home), argv=[sys.executable, "-c", "print(12)"])(p)) == 12


def test_a_slow_child_is_killed_at_the_timeout_and_the_count_is_simply_unknown(home):
    p = write(home, [compacted()])
    c = CompactionCounter(str(home), timeout=0.3, argv=[sys.executable, "-c", "import time; time.sleep(30)"])
    started = time.monotonic()
    assert run(c(p)) is None
    assert time.monotonic() - started < 5
    assert c._cache == {}  # an unknown result is never cached
    fast = CompactionCounter(str(home), argv=[sys.executable, "-c", "print(4)"])
    assert run(fast(p)) == 4
