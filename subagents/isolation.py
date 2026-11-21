"""subagents.isolation - private working copies for workers (plan items W1, W2 helpers, W3 diff).

A worker never edits the real repo. For a task with write tools the harness copies ONLY the
files in the task's scope into two folders inside the run folder:
    base/   pristine copy (never given to the worker)
    work/   the worker's working folder, same relative paths as the repo
When the run ends, make_diff(base, work) produces a unified diff that `patch -p1` accepts when run
from the repo root. The main agent reviews it, runs the tests, and applies it. Nothing here ever
writes into the source repo.

Limits (documented, not hidden): base/ is not made read-only (that would break cleanup), so a
shell command that deliberately edits base/ can hide a change. Symlinks are copied as symlinks and
ignored by the diff. Empty-file creation cannot be expressed as a patch and is reported as a note.
"""
from __future__ import annotations

import difflib
import os
import shutil
from typing import Dict, List

SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv", "venv"})
MAX_FILES = 5000
MAX_BYTES = 50 * 1024 * 1024


class IsolationError(ValueError):
    """The scope or copy request is not acceptable."""


def _real(p: str) -> str:
    return os.path.realpath(p)


def _inside(path: str, root: str) -> bool:
    root, path = _real(root), _real(path)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _is_under(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent + "/")


def normalize_scope(scope: List[str], source_root: str, runs_dir: str) -> List[str]:
    """Return sorted repo-relative posix paths, nested entries dropped. Raises IsolationError."""
    if not scope:
        raise IsolationError("a worker needs a scope: the files or folders it may change")
    root = _real(source_root)
    rels: List[str] = []
    for s in scope:
        if not isinstance(s, str) or not s.strip():
            raise IsolationError("empty scope entry")
        raw = os.path.expanduser(s.strip())
        absp = _real(raw if os.path.isabs(raw) else os.path.join(root, raw))
        if not _inside(absp, root):
            raise IsolationError("scope path is outside the repository: %s" % s)
        if absp == root:
            raise IsolationError("scope cannot be the whole repository; name the folders or files")
        if _inside(runs_dir, absp):
            raise IsolationError("scope contains the sub-agent run folder: %s" % s)
        if not os.path.exists(absp):
            raise IsolationError("scope path does not exist: %s" % s)
        rels.append(os.path.relpath(absp, root).replace(os.sep, "/"))
    out: List[str] = []
    for r in sorted(set(rels)):
        if not any(_is_under(r, k) for k in out):
            out.append(r)
    return out


def overlaps(a: List[str], b: List[str]) -> bool:
    return any(_is_under(x, y) or _is_under(y, x) for x in a for y in b)


def _walk_files(path: str):
    if os.path.islink(path) or os.path.isfile(path):
        yield path
        return
    for dp, dns, fns in os.walk(path):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            yield os.path.join(dp, fn)


def _copy(src: str, dst: str) -> None:
    if os.path.isdir(src) and not os.path.islink(src):
        shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns(*SKIP_DIRS))
    else:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst, follow_symlinks=False)


def prepare(run_dir: str, source_root: str, rels: List[str]) -> str:
    """Copy the scope into run_dir/base and run_dir/work. Returns the work folder."""
    root = _real(source_root)
    srcs = [os.path.join(root, r) for r in rels]
    count = size = 0
    for s in srcs:
        for f in _walk_files(s):
            count += 1
            try:
                size += os.lstat(f).st_size
            except OSError:
                pass
            if count > MAX_FILES or size > MAX_BYTES:
                raise IsolationError("scope is too large (over %d files or %d MB); narrow it"
                                     % (MAX_FILES, MAX_BYTES // (1024 * 1024)))
    base, work = os.path.join(run_dir, "base"), os.path.join(run_dir, "work")
    os.makedirs(base, exist_ok=True)
    os.makedirs(work, exist_ok=True)
    for r, s in zip(rels, srcs):
        for dest in (base, work):
            _copy(s, os.path.join(dest, r))
    return work


# ---- diff ---------------------------------------------------------------------------------
def _files(root: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            p = os.path.join(dp, fn)
            if os.path.islink(p):
                continue
            out[os.path.relpath(p, root).replace(os.sep, "/")] = p
    return out


def _read(p: str) -> bytes:
    with open(p, "rb") as f:
        return f.read()


def _binary(data) -> bool:
    return data is not None and b"\x00" in data[:8192]


def _lines(data) -> List[str]:
    if data is None:
        return []
    parts = data.decode("utf-8", "surrogateescape").split("\n")
    out = [x + "\n" for x in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


def make_diff(base: str, work: str, write_paths=None) -> str:
    a, b = _files(base), _files(work)
    chunks: List[str] = []
    wp = [w.strip().lstrip("./").rstrip("/") for w in (write_paths or []) if w and w.strip()]
    ignored: List[str] = []
    for rel in sorted(set(a) | set(b)):
        if wp and not any(rel == w or rel.startswith(w + "/") for w in wp):
            if a.get(rel) is None or b.get(rel) is None or _read(a[rel]) != _read(b[rel]):
                ignored.append(rel)
            continue
        da = _read(a[rel]) if rel in a else None
        db = _read(b[rel]) if rel in b else None
        if da == db:
            continue
        if _binary(da) or _binary(db):
            chunks.append("Binary files a/%s and b/%s differ\n" % (rel, rel))
            continue
        diff = list(difflib.unified_diff(
            _lines(da), _lines(db),
            fromfile="a/" + rel if da is not None else "/dev/null",
            tofile="b/" + rel if db is not None else "/dev/null"))
        if not diff:
            chunks.append("Note: %s changed in a way a patch cannot express (empty file created or emptied)\n" % rel)
            continue
        chunks.append("".join(x if x.endswith("\n") else x + "\n\\ No newline at end of file\n" for x in diff))
    if ignored:
        chunks.append("Note: ignored changes outside write_paths: %s\n" % ", ".join(ignored))
    return "".join(chunks) if chunks else "No changes.\n"


def write_diff(base: str, work: str, path: str, write_paths=None) -> None:
    text = make_diff(base, work, write_paths)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(text)
    os.replace(tmp, path)
