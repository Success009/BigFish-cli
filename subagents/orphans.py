"""Orphan cleanup for sub-agent browsers (plan item A7).

After a hard kill of the CLI, a slot's Chrome keeps running (it has its own session) and its
profile copy stays on disk. This module finds and stops only those Chromes, by the exact
profile-copy path <work_root>/chrome_<name>, then deletes the stale copies.

Never touches: Chromes on debug ports 9222-9229, any other user-data-dir, runs listed as live.
Pure standard library. No project imports, no ui/audio.
"""
from __future__ import annotations

import os
import shutil
import signal
import time
from typing import Callable, Dict, Iterable, List, Optional, Tuple

DIR_PREFIX = "chrome_"
RESERVED_PORTS = frozenset(range(9222, 9230))
_UDD = "--user-data-dir="
_PORT = "--remote-debugging-port="


def _read_args(proc_root: str, pid: str) -> List[str]:
    try:
        with open(os.path.join(proc_root, pid, "cmdline"), "rb") as f:
            raw = f.read()
    except OSError:
        return []
    args = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
    if len(args) == 1 and " --" in args[0]:
        # Chrome rewrites its own argv into ONE string; split it back at each " --".
        parts = args[0].split(" --")
        args = [parts[0]] + ["--" + x for x in parts[1:]]
    return args


def _reserved_port(args: List[str]) -> bool:
    for a in args:
        if a.startswith(_PORT):
            try:
                return int(a[len(_PORT):]) in RESERVED_PORTS
            except ValueError:
                return True  # unreadable port: treat as protected
    return False


def find_orphans(work_root: str, live: Iterable[str] = (), proc_root: str = "/proc") -> Dict[str, List[int]]:
    """Map slot name -> pids of Chrome processes whose user-data-dir is <work_root>/chrome_<name>."""
    base = os.path.join(os.path.abspath(work_root), DIR_PREFIX)
    live = set(live)
    found: Dict[str, List[int]] = {}
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return found
    for pid in entries:
        if not pid.isdigit():
            continue
        args = _read_args(proc_root, pid)
        if not args or _reserved_port(args):
            continue
        for a in args:
            if not a.startswith(_UDD):
                continue
            path = os.path.normpath(a[len(_UDD):])
            if not path.startswith(base):
                continue
            name = path[len(base):].split(os.sep)[0]
            if name and name not in live:
                found.setdefault(name, []).append(int(pid))
            break
    return found


def _alive(pid: int, proc_root: str) -> bool:
    return os.path.exists(os.path.join(proc_root, str(pid)))


def sweep(work_root: str, live: Iterable[str] = (), *, grace: float = 3.0, proc_root: str = "/proc",
          kill: Optional[Callable[[int, int], None]] = None) -> Tuple[List[int], List[str]]:
    """Stop orphan slot Chromes, then delete stale chrome_* folders of runs that are not live.
    Returns (pids signalled, folder names removed)."""
    kill = kill or os.kill
    live = set(live)
    orphans = find_orphans(work_root, live, proc_root)
    pids = sorted({p for plist in orphans.values() for p in plist})
    for p in pids:
        try:
            kill(p, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + grace
    while time.time() < deadline and any(_alive(p, proc_root) for p in pids):
        time.sleep(0.1)
    for p in pids:
        if _alive(p, proc_root):
            try:
                kill(p, signal.SIGKILL)
            except OSError:
                pass
    removed: List[str] = []
    try:
        names = sorted(os.listdir(work_root))
    except OSError:
        return pids, removed
    for entry in names:
        if not entry.startswith(DIR_PREFIX):
            continue
        full = os.path.join(work_root, entry)
        if entry[len(DIR_PREFIX):] in live or not os.path.isdir(full):
            continue
        shutil.rmtree(full, ignore_errors=True)
        removed.append(entry)
    return pids, removed
