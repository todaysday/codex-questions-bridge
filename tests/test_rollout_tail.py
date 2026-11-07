"""Rollout tail reader: bounded, path-checked, fail soft. Fixtures hold numbers only."""

import json
import os

import pytest

from cqb.codex.rollout_tail import TAIL_STEPS, read_usage


def token_line(total=29_500, window=475_000, inp=29_000, cached=23_200, info=True, session=(290_000, 232_000)):
    payload = {"type": "token_count", "rate_limits": {"limit_id": "codex"}}
    payload["info"] = {
        "total_token_usage": {"total_tokens": total * 9, "input_tokens": session[0], "cached_input_tokens": session[1]}
        if session else {"total_tokens": total * 9},
        "last_token_usage": {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": 500,
                             "total_tokens": total},
        "model_context_window": window,
    } if info else None
    return json.dumps({"timestamp": "t", "type": "event_msg", "payload": payload})


def noise(n=1):
    return "\n".join(json.dumps({"type": "response_item", "payload": {"type": "message", "x": "y" * 80}})
                     for _ in range(n))


@pytest.fixture
def home(tmp_path):
    (tmp_path / "sessions" / "2026" / "11").mkdir(parents=True)
    (tmp_path / "archived_sessions").mkdir()
    return tmp_path


def write(home, lines, name="r.jsonl", sub="sessions/2026/11"):
    p = home / sub / name
    p.write_text("\n".join(lines) + "\n")
    return str(p)


def test_newest_token_count_wins_and_fields_are_mapped(home):
    p = write(home, [token_line(total=1), noise(3), token_line(total=29_500), noise(2)])
    u = read_usage(p, str(home))
    assert (u.context_tokens, u.context_window, u.input_tokens, u.cached_input_tokens) == (29_500, 475_000, 29_000, 23_200)
    assert u.fill_percent == 6 and (u.total_input_tokens, u.total_cached_input_tokens) == (290_000, 232_000)
    assert u.cache_permille == 800  # the whole session, not the latest call (23_200 / 29_000 is 80% here by chance)
    last_only = read_usage(write(home, [token_line(session=None)], "l.jsonl"), str(home))
    assert last_only.cache_permille is None  # no session totals: no figure rather than the last call's
    odd = read_usage(write(home, [token_line(session=(100, 200)), ], "o.jsonl"), str(home))
    assert odd.cache_permille is None  # cached above input is nonsense: unknown


def test_info_null_updates_are_skipped_in_favor_of_older_usage(home):
    p = write(home, [token_line(total=7), noise(2), token_line(info=False)])
    assert read_usage(p, str(home)).context_tokens == 7


def test_archived_sessions_dir_is_allowed(home):
    p = write(home, [token_line()], sub="archived_sessions")
    assert read_usage(p, str(home)).context_tokens == 29_500


def test_no_token_count_and_garbage_yield_none(home):
    assert read_usage(write(home, [noise(5)]), str(home)) is None
    assert read_usage(write(home, ["{not json token_count", '{"type":"event_msg","payload":{"type":"token_count",'
                                  '"info":{"last_token_usage":{"input_tokens":"x"}}}}'], "g.jsonl"), str(home)) is None
    assert read_usage(write(home, [], "e.jsonl"), str(home)) is None
    assert read_usage(str(home / "sessions" / "missing.jsonl"), str(home)) is None


def test_partial_first_line_is_ignored(home):
    # The tail cut lands exactly after a junk prefix, leaving a valid token_count JSON as the first
    # (partial) line of the tail. It belongs to a longer line, so it must not be trusted.
    path = home / "sessions" / "p.jsonl"
    junk = "Z" * 50
    head = junk + token_line(total=333) + "\n"
    pad = "y" * (TAIL_STEPS[0] + len(junk) - len(head) - 1) + "\n"
    path.write_text(head + pad)
    assert path.stat().st_size == TAIL_STEPS[0] + len(junk)
    assert read_usage(str(path), str(home)) is None
    path.write_text(token_line(total=444) + "\n" + head + pad)  # a complete older line farther up: widening finds it
    assert read_usage(str(path), str(home)).context_tokens == 444


def test_huge_file_reads_only_the_tail(home, monkeypatch):
    path = home / "sessions" / "huge.jsonl"
    with open(path, "w") as f:
        f.write(token_line(total=1) + "\n")  # at the head: must never be reached
        f.write(noise(1) + "\n")
        f.seek(8 * 1024 * 1024)  # sparse hole: 8 MiB of NULs, free on disk
        f.write("\n" + noise(2) + "\n" + token_line(total=42) + "\n")
    read = []
    real_read = os.read
    monkeypatch.setattr(os, "read", lambda fd, n: (lambda b: (read.append(len(b)), b)[1])(real_read(fd, n)))
    assert read_usage(str(path), str(home)).context_tokens == 42
    assert sum(read) <= TAIL_STEPS[0] + 65536  # first step only, one extra chunk of slack
    # a file whose only token_count is at the head is not found: the head is out of reach
    with open(path, "r+") as f:
        f.seek(8 * 1024 * 1024)
        f.write("\n" + noise(2) + "\n")
        f.truncate()
    read.clear()
    assert read_usage(str(path), str(home)) is None
    assert sum(read) <= sum(TAIL_STEPS)


def test_path_outside_codex_home_is_refused(home, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    p = elsewhere / "r.jsonl"
    p.write_text(token_line() + "\n")
    assert read_usage(str(p), str(home)) is None
    inside_root = home / "auth.jsonl"  # in codex_home but outside sessions dirs
    inside_root.write_text(token_line() + "\n")
    assert read_usage(str(inside_root), str(home)) is None
    assert read_usage(str(home / "sessions" / ".." / "auth.jsonl"), str(home)) is None
    assert read_usage(str(home / "sessions"), str(home)) is None  # the directory itself
    assert read_usage("", str(home)) is None


def test_sibling_directory_with_same_prefix_is_refused(home):
    sib = home / "sessions_evil"
    sib.mkdir()
    (sib / "r.jsonl").write_text(token_line() + "\n")
    assert read_usage(str(sib / "r.jsonl"), str(home)) is None


def test_symlink_escape_is_refused(home, tmp_path_factory):
    secret = tmp_path_factory.mktemp("secret") / "outside.jsonl"
    secret.write_text(token_line() + "\n")
    link = home / "sessions" / "2026" / "link.jsonl"
    link.symlink_to(secret)
    assert read_usage(str(link), str(home)) is None
    dirlink = home / "sessions" / "dirlink"
    dirlink.symlink_to(secret.parent)
    assert read_usage(str(dirlink / "outside.jsonl"), str(home)) is None


def test_symlink_inside_the_sessions_tree_is_followed_to_a_checked_target(home):
    real = write(home, [token_line(total=5)], "real.jsonl")
    link = home / "sessions" / "2026" / "alias.jsonl"
    link.symlink_to(real)
    assert read_usage(str(link), str(home)).context_tokens == 5


def test_non_regular_file_is_refused(home):
    fifo = home / "sessions" / "fifo.jsonl"
    os.mkfifo(fifo)
    assert read_usage(str(fifo), str(home)) is None
