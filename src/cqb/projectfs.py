"""Confined, read-only access to the files of ONE project directory (design sections 4.2 and 10).

The Bridge runs as root and the reader is an LLM fed with untrusted data, so this module is
deliberately paranoid and does only blocking stdlib I/O (callers run it in a worker thread):

* The root is realpath-ed once and opened as a directory fd; the requested relative path is then
  walked component by component with O_NOFOLLOW (`dir_fd=`), so nothing is ever resolved by path
  again and a symlink swapped in after a check is simply refused. ANY symlink on the way is refused,
  even one that stays inside the root.
* Absolute paths, `..`, empty paths, backslashes, control/format characters and lookalikes are
  refused before the filesystem is touched.
* DENY_PATTERNS is checked on every component, on the string alone, so `denied` never reveals
  whether a path exists. Denied names are also hidden from listings (counted only).
* Only regular files are opened (lstat first, fstat after open, O_NONBLOCK so a FIFO cannot block).
* Memory: reads go 8 KiB at a time, a line is kept at most LINE_CAP bytes, a file is refused above
  MAX_FILE_BYTES, a listing scans at most SCAN_LIMIT entries. File contents are never logged here.
Redaction (security.py) is applied to every line before it is counted against the byte budget.
"""

import errno
import fnmatch
import json
import os
import stat
import unicodedata

from cqb.domain.context import DirEntryInfo, DirListing, FileChunk
from cqb.security import KEY_LINE, PrivateKeyLines, redact

# One table, case-insensitive fnmatch patterns applied to every path component (names, not paths).
DENY_PATTERNS: tuple[str, ...] = (
    ".git", ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker",
    ".npmrc", ".pypirc", ".netrc", ".git-credentials",
    "secrets", "secret", "*secret*", "*credential*", "*token*",
    "*.keystore", ".vault*", "*.tfstate", "*.tfvars", ".terraform",
    "*.sqlite", "*.sqlite3", "*.db", "*.db-wal", "*.db-shm",
    "node_modules", ".venv", "venv", "__pycache__", ".codex",
)
# Exact (wildcard-free) deny names must not appear in the ROOT path either: no project lives in ~/.ssh.
SENSITIVE_ROOT_PARTS = frozenset(p for p in DENY_PATTERNS if not any(c in p for c in "*?["))
BROAD_ROOTS = frozenset({
    "/", "/root", "/home", "/etc", "/var", "/usr", "/opt", "/srv", "/bin", "/sbin", "/lib", "/lib64",
    "/boot", "/dev", "/proc", "/sys", "/run", "/tmp", "/var/tmp", "/mnt", "/media", "/snap", "/srv/projects",
})
SYSTEM_PREFIXES = ("/etc", "/proc", "/sys", "/dev", "/boot", "/run")  # nothing under these is a project

MAX_FILE_BYTES = 2 * 1024 * 1024
SAMPLE_BYTES = 8192  # binary sniffing window
CHUNK = 8192
LINE_CAP = 2000  # bytes kept of one line
COUNT_MAX_BYTES = 256 * 1024  # total_lines is counted only for files up to this size ("cheap")
SCAN_LIMIT = 5000  # directory entries looked at
MAX_PATH_CHARS = 1024
MAX_DEPTH = 40
MAX_NAME_BYTES = 255
BINARY_RATIO = 0.30
_TEXT_ASCII = bytes({7, 8, 9, 10, 12, 13, 27} | set(range(0x20, 0x7F)))
_UNSAFE = frozenset({"Cc", "Cf", "Cs"})  # control, format (bidi, zero width), lone surrogates
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC


class FsError(Exception):
    """A refusal with a stable code; the caller owns the (generic) message."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- names and paths -------------------------------------------------------------------

def fold(name: str) -> str:
    """Comparison form of a name: NFKC, no format characters, case-folded, no trailing dots or spaces."""
    text = unicodedata.normalize("NFKC", name)
    text = "".join(c for c in text if unicodedata.category(c) != "Cf")
    return text.casefold().rstrip(" .")


class DenyList:
    def __init__(self, extra: tuple[str, ...] = ()) -> None:
        self._patterns = tuple(p.casefold() for p in (*DENY_PATTERNS, *extra))

    def denies(self, name: str) -> bool:
        key = fold(name)
        return any(fnmatch.fnmatchcase(key, p) for p in self._patterns)

    def check(self, parts: list[str]) -> None:
        if any(self.denies(p) for p in parts):
            raise FsError("denied")


def split_relpath(path: object) -> list[str]:
    """Components of a safe relative path (`.` and empty components dropped); FsError("invalid_path") otherwise."""
    if not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS or path.startswith("/"):
        raise FsError("invalid_path")
    if "\\" in path or any(unicodedata.category(c) in _UNSAFE for c in path):
        raise FsError("invalid_path")
    parts = [c for c in path.split("/") if c not in ("", ".")]
    if len(parts) > MAX_DEPTH:
        raise FsError("invalid_path")
    for c in parts:
        norm = unicodedata.normalize("NFKC", c)
        if c == ".." or norm == ".." or "/" in norm or "\\" in norm or len(c.encode()) > MAX_NAME_BYTES:
            raise FsError("invalid_path")
    return parts


def safe_name(name: str) -> str:
    """A directory entry name fit for JSON and for a prompt: valid UTF-8, no control or format characters."""
    text = os.fsencode(name).decode("utf-8", "replace")
    return "".join("?" if unicodedata.category(c) in _UNSAFE else c for c in text)


# --- the root --------------------------------------------------------------------------

def open_root(cwd: object) -> int:
    """Directory fd of the project root; FsError cwd_unavailable / cwd_too_broad / cwd_denied."""
    if not isinstance(cwd, str) or not cwd.startswith("/") or "\0" in cwd or len(cwd) > 4096:
        raise FsError("cwd_unavailable")
    try:
        real = os.path.realpath(cwd, strict=True)
    except (OSError, ValueError):
        raise FsError("cwd_unavailable") from None
    _check_root(real)
    try:
        fd = os.open(real, DIR_FLAGS)
    except OSError:
        raise FsError("cwd_unavailable") from None
    try:
        # what was really opened must be what was checked (a swap between realpath and open)
        if os.readlink(f"/proc/self/fd/{fd}") != real:
            raise FsError("cwd_unavailable")
    except OSError:
        os.close(fd)
        raise FsError("cwd_unavailable") from None
    except FsError:
        os.close(fd)
        raise
    return fd


def _check_root(real: str) -> None:
    parts = [p for p in real.split("/") if p]
    home = os.path.realpath(os.path.expanduser("~"))
    if (len(parts) < 2 or real in BROAD_ROOTS or real == home or (len(parts) == 2 and parts[0] == "home")
            or any(real == p or real.startswith(p + "/") for p in SYSTEM_PREFIXES)):
        raise FsError("cwd_too_broad")
    if any(fold(p) in SENSITIVE_ROOT_PARTS for p in parts):
        raise FsError("cwd_denied")


# --- the walk --------------------------------------------------------------------------

def _classify(name: str, dir_fd: int, err: OSError) -> FsError:
    """Why opening `name` failed, from lstat (a symlink is always named as such)."""
    try:
        mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return FsError("not_found")
    except OSError:
        mode = None
    if mode is not None and stat.S_ISLNK(mode):
        return FsError("symlink_refused")
    return {errno.ENOENT: FsError("not_found"), errno.ENOTDIR: FsError("not_a_directory"),
            errno.ELOOP: FsError("symlink_refused"), errno.ENAMETOOLONG: FsError("invalid_path"),
            errno.EACCES: FsError("denied"), errno.EPERM: FsError("denied")}.get(err.errno, FsError("unavailable"))


def walk_dirs(root_fd: int, parts: list[str]) -> int:
    """Open the directory `parts` below the root, one O_NOFOLLOW step at a time; the caller closes the fd."""
    cur = os.dup(root_fd)
    try:
        for name in parts:
            try:
                nxt = os.open(name, DIR_FLAGS, dir_fd=cur)
            except OSError as err:
                raise _classify(name, cur, err) from None
            os.close(cur)
            cur = nxt
    except BaseException:
        os.close(cur)
        raise
    return cur


def open_file(parent_fd: int, name: str) -> tuple[int, int]:
    """(fd, size) of a regular file; lstat before (never open a FIFO or device) and fstat after (races)."""
    try:
        mode = os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode
    except OSError as err:
        raise _classify(name, parent_fd, err) from None
    if stat.S_ISLNK(mode):
        raise FsError("symlink_refused")
    if not stat.S_ISREG(mode):
        raise FsError("not_a_file")
    try:
        fd = os.open(name, FILE_FLAGS, dir_fd=parent_fd)
    except OSError as err:
        raise (FsError("not_a_file") if err.errno == errno.ENXIO else _classify(name, parent_fd, err)) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise FsError("not_a_file")
        if st.st_size > MAX_FILE_BYTES:
            raise FsError("binary_or_too_large")
    except BaseException:
        os.close(fd)
        raise
    return fd, st.st_size


# --- reading ---------------------------------------------------------------------------

def looks_binary(sample: bytes) -> bool:
    """A NUL byte, or more than 30% bytes that are not text (valid UTF-8 high bytes count as text)."""
    if not sample:
        return False
    if b"\0" in sample:
        return True
    rest = sample.translate(None, _TEXT_ASCII)
    try:
        sample.decode("utf-8")
        bad = sum(1 for b in rest if b < 0x80)
    except UnicodeDecodeError as err:
        # a multi-byte character cut by the window is fine; anything else counts as binary data
        cut = len(sample) >= SAMPLE_BYTES and err.start >= len(sample) - 3 and err.reason == "unexpected end of data"
        bad = sum(1 for b in rest if b < 0x80) if cut else len(rest)
    return bad / len(sample) > BINARY_RATIO


def _cost(row: str) -> int:
    """Bytes the row takes inside the JSON result, escapes included (the two quotes stand for the joining `\\n`)."""
    return len(json.dumps(row, ensure_ascii=False).encode())


class _Page:
    """Collects the lines [offset, offset + max_lines) within a byte budget; sees every line for the key state."""

    def __init__(self, offset: int, max_lines: int, budget: int, count_all: bool) -> None:
        self.offset, self.max_lines, self.budget, self.count_all = offset, max_lines, budget, count_all
        self.n = 0
        self.rows: list[str] = []
        self.used = 0
        self.more = False
        self.lines_cut = False
        self.redacted = False
        self._keys = PrivateKeyLines()

    def feed(self, raw: bytes, cut: bool) -> bool:
        """One line in file order. False = nothing more is needed from the file."""
        idx = self.n
        self.n += 1
        text = raw.decode("utf-8", "replace")
        in_key = self._keys.hides(text)  # state must follow every line, also those before the page
        if idx < self.offset:
            return True
        if self.more:
            return self.count_all
        if len(self.rows) >= self.max_lines:
            self.more = True
            return self.count_all
        if in_key:
            shown, changed = KEY_LINE, True
        else:
            shown, changed = redact(text)
        row = f"{idx + 1}: {shown}"
        cost = _cost(row)
        if cost > self.budget - self.used:
            if self.rows:
                self.more = True
                return self.count_all
            while cost > self.budget and row:  # a single line larger than the whole budget: shorten it
                row = row[: len(row) // 2]
                cost = _cost(row)
            cut = True
        self.rows.append(row)
        self.used += cost
        self.lines_cut |= cut
        self.redacted |= changed
        return True


def _read_exact(fd: int, n: int) -> bytes:
    out = b""
    while len(out) < n:
        piece = os.read(fd, n - len(out))
        if not piece:
            break
        out += piece
    return out


def _scan(fd: int, sample: bytes, page: _Page) -> bool:
    """Feed the lines of the file to `page`; True when the end of the file was reached."""
    pending = bytearray()
    pending_cut = False
    scanned = 0
    chunk = sample
    while chunk:
        scanned += len(chunk)
        if scanned > MAX_FILE_BYTES:  # a file that grew, or a lying st_size
            raise FsError("binary_or_too_large")
        pieces = chunk.split(b"\n")
        for piece in pieces[:-1]:
            room = LINE_CAP - len(pending)
            pending += piece[:room]
            pending_cut |= len(piece) > room
            line = bytes(pending[:-1] if pending.endswith(b"\r") else pending)
            if not page.feed(line, pending_cut):
                return False
            pending.clear()
            pending_cut = False
        tail = pieces[-1]
        room = LINE_CAP - len(pending)
        pending += tail[:room]
        pending_cut |= len(tail) > room
        chunk = os.read(fd, CHUNK)
    if pending or pending_cut:
        page.feed(bytes(pending[:-1] if pending.endswith(b"\r") else pending), pending_cut)
    return True


def read_file(
    root_fd: int, parts: list[str], deny: DenyList, *, root_id: str, offset: int, max_lines: int, budget: int,
) -> FileChunk:
    deny.check(parts)  # on the strings alone, before the filesystem is touched
    if not parts:
        raise FsError("not_a_file")
    parent = walk_dirs(root_fd, parts[:-1])
    try:
        fd, size = open_file(parent, parts[-1])
    finally:
        os.close(parent)
    try:
        sample = _read_exact(fd, SAMPLE_BYTES)
        if looks_binary(sample):
            raise FsError("binary_or_too_large")
        page = _Page(offset, max_lines, budget, count_all=size <= COUNT_MAX_BYTES)
        eof = _scan(fd, sample, page)
    finally:
        os.close(fd)
    return FileChunk(
        root_id=root_id, path="/".join(parts), text="\n".join(page.rows), offset_lines=offset,
        returned_lines=len(page.rows), total_lines=page.n if eof else None, truncated=page.more,
        next_offset_lines=offset + len(page.rows) if page.more else None, lines_cut=page.lines_cut,
        redacted=page.redacted, size=size,
    )


# --- listing ---------------------------------------------------------------------------

def list_dir(
    root_fd: int, parts: list[str], deny: DenyList, *, root_id: str, max_entries: int, budget: int,
) -> DirListing:
    deny.check(parts)
    dfd = walk_dirs(root_fd, parts)
    try:
        hidden = 0
        seen = 0
        scan_cut = False
        visible: list[DirEntryInfo] = []
        with os.scandir(dfd) as it:
            for entry in it:
                if seen >= SCAN_LIMIT:
                    scan_cut = True
                    break
                seen += 1
                if deny.denies(entry.name):
                    hidden += 1
                    continue
                visible.append(_describe(entry))
    finally:
        os.close(dfd)
    visible.sort(key=lambda e: (e.type != "dir", e.name.casefold(), e.name))
    kept: list[DirEntryInfo] = []
    used = 0
    for e in visible[:max_entries]:
        cost = len(json.dumps({"name": e.name, "type": e.type, "size": e.size}, ensure_ascii=False).encode()) + 1
        if used + cost > budget:
            break
        kept.append(e)
        used += cost
    return DirListing(
        root_id=root_id, path="/".join(parts) or ".", entries=tuple(kept), hidden=hidden,
        truncated=scan_cut or len(kept) < len(visible), total=len(visible),
    )


def _describe(entry: os.DirEntry[str]) -> DirEntryInfo:
    name = safe_name(entry.name)
    try:
        if entry.is_symlink():
            return DirEntryInfo(name, "symlink", None)
        if entry.is_dir(follow_symlinks=False):
            return DirEntryInfo(name, "dir", None)
        if entry.is_file(follow_symlinks=False):
            return DirEntryInfo(name, "file", entry.stat(follow_symlinks=False).st_size)
    except OSError:
        pass
    return DirEntryInfo(name, "other", None)
