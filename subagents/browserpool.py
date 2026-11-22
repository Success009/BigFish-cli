"""Per-sub-agent Chrome profile and debug port (plan item A1).

Pure standard library. No provider names, no project imports, no ui/audio.
A slot owns one profile copy and one Chrome process, and stops only that process by pid.
"""
from __future__ import annotations

import os
import shutil
import socket
import signal
import subprocess
import time
import urllib.request

SKIP_DIRS = ("Cache", "Code Cache", "GPUCache", "Service Worker")
RESERVED_PORTS = frozenset(range(9222, 9230))
FIRST_PORT = 9230
LAST_PORT = 9330


class PoolError(RuntimeError):
    """Profile copy, port or Chrome launch failed."""


def _active_window():
    """X11 id of the focused window, or None when xdotool is missing or fails."""
    try:
        if os.name == "nt" or not shutil.which("xdotool"):
            return None
        out = subprocess.run(["xdotool", "getactivewindow"], capture_output=True, text=True, timeout=2).stdout.strip()
        return out if out.isdigit() else None
    except Exception:
        return None


def _restore_window(win_id) -> None:
    """Give focus back to win_id so a new background Chrome never keeps it."""
    try:
        if win_id and shutil.which("xdotool"):
            subprocess.run(["xdotool", "windowactivate", str(win_id)], capture_output=True, timeout=2)
    except Exception:
        pass


def _check_profile_name(profile: str) -> str:
    if (not profile or profile in (".", "..") or "/" in profile or "\\" in profile
            or os.sep in profile):
        raise PoolError("bad profile name: %r" % (profile,))
    return profile


def build_profile_copy(src_dir: str, profile: str, dest_dir: str) -> str:
    """Copy Local State plus ONE profile folder into dest_dir, skipping disposable caches."""
    _check_profile_name(profile)
    src_profile = os.path.join(src_dir, profile)
    if not os.path.isdir(src_profile):
        raise PoolError("profile not found: %s" % src_profile)
    if os.path.exists(dest_dir):
        raise PoolError("destination already exists: %s" % dest_dir)
    os.makedirs(dest_dir)
    try:
        local_state = os.path.join(src_dir, "Local State")
        if os.path.isfile(local_state):
            shutil.copy2(local_state, os.path.join(dest_dir, "Local State"))
        shutil.copytree(
            src_profile,
            os.path.join(dest_dir, profile),
            symlinks=True,
            ignore=shutil.ignore_patterns(*SKIP_DIRS),
            ignore_dangling_symlinks=True,
        )
    except Exception as exc:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise PoolError("profile copy failed: %s" % exc)
    return dest_dir


def _port_is_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pick_free_port(start: int = FIRST_PORT, last: int = LAST_PORT, taken=()) -> int:
    taken = set(taken)
    for port in range(start, last + 1):
        if port in RESERVED_PORTS or port in taken:
            continue
        if _port_is_free(port):
            return port
    raise PoolError("no free debug port in %d-%d" % (start, last))


def http_ready(port: int, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % port, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def default_binary() -> str:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    raise PoolError("no chrome binary found")


class BrowserSlot:
    """One sub-agent's private browser: profile copy + debug port + process."""

    def __init__(self, src_dir, profile, work_root, name, *, binary=None, ready=None,
                 extra_args=(), start_url=None, port_taken=(), ready_timeout=30.0):
        self.src_dir = src_dir
        self.profile = _check_profile_name(profile)
        self.dest_dir = os.path.join(work_root, "chrome_" + name)
        self.binary = binary
        self.ready = ready or http_ready
        self.extra_args = tuple(extra_args)
        self.start_url = start_url
        self.port_taken = tuple(port_taken)
        self.ready_timeout = ready_timeout
        self.port = None
        self.proc = None

    @property
    def cdp_url(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    def start(self) -> str:
        if self.proc is not None:
            raise PoolError("slot already started")
        binary = self.binary or default_binary()
        build_profile_copy(self.src_dir, self.profile, self.dest_dir)
        try:
            self.port = pick_free_port(taken=self.port_taken)
            _prev_window = _active_window()
            args = [binary,
                    "--user-data-dir=" + self.dest_dir,
                    "--profile-directory=" + self.profile,
                    "--remote-debugging-port=%d" % self.port,
                    "--window-size=500,1040",
                    "--no-first-run"]
            args.extend(self.extra_args)
            if self.start_url:
                args.append(self.start_url)
            self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         start_new_session=True)
            deadline = time.time() + self.ready_timeout
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    raise PoolError("chrome exited early (code %s)" % self.proc.returncode)
                if self.ready(self.port):
                    _restore_window(_prev_window)
                    return self.cdp_url
                time.sleep(0.2)
            raise PoolError("chrome debug port %d not ready in %.0fs" % (self.port, self.ready_timeout))
        except Exception:
            self.close()
            raise

    def _group_alive(self, pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def _signal_group(self, pgid: int, sig) -> None:
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def stop(self, grace: float = 5.0) -> None:
        """Stop only the process group this slot started (its own session, so never another Chrome)."""
        p = self.proc
        if p is None:
            return
        pgid = p.pid
        self._signal_group(pgid, signal.SIGTERM)
        try:
            p.wait(timeout=grace)
        except Exception:
            pass
        deadline = time.time() + grace
        while self._group_alive(pgid) and time.time() < deadline:
            time.sleep(0.1)
        if self._group_alive(pgid):
            self._signal_group(pgid, signal.SIGKILL)
            deadline = time.time() + grace
            while self._group_alive(pgid) and time.time() < deadline:
                time.sleep(0.1)
        try:
            p.wait(timeout=1)
        except Exception:
            pass
        self.proc = None

    def close(self) -> None:
        self.stop()
        shutil.rmtree(self.dest_dir, ignore_errors=True)
        if os.path.exists(self.dest_dir):
            time.sleep(0.5)
            shutil.rmtree(self.dest_dir, ignore_errors=True)
