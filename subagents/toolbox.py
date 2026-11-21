"""subagents.toolbox - the tool layer a sub-agent runs against (plan items R3, R4).

Rules, enforced here in code (the prompt only describes them):
  1. A tool not in the role's allowed list is refused, never run.
  2. Read tools (read, list, search) work only inside the READ ROOTS (repo root plus the
     workdir). Readability is checked first, so agent/tools.py never reaches its sudo fallback.
  3. Write tools (write, edit, create dir, delete, rename) work only inside the WORKDIR.
     Paths are resolved with realpath, so "..", absolute paths and symlinks that point out
     are all refused.
  4. run_command uses a plain subprocess (no Rich panel, no sudo) with cwd = workdir, a timeout
     and an output cap. LIMIT: a shell command cannot be truly sandboxed (it can use absolute
     paths). The real protection for a worker is its isolated copy or worktree (plan W1) plus
     the main agent reviewing the diff. This module only blocks sudo, su and doas.

Never imports interface/ui, interface/audio, agent/elevation or agent/executor.
The file tools in agent/tools.py are loaded lazily by path (they need agent/ on sys.path).
"""
from __future__ import annotations

import importlib.util
import os
import re
import signal
import subprocess
import sys
from typing import Dict, List, Optional

from subagents.contracts import KNOWN_TOOLS, WRITE_TOOLS

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CMD_TIMEOUT = 120
OUTPUT_CAP = 15000

_PATH_KEYS = ("path", "file_path", "directory", "directory_path", "destination")
_SUDO_RE = re.compile(r"(^|[\s;&|(`])(sudo|su|doas)(\s|$)")

_tools_mod = None


def _agent_tools():
    """Load agent/tools.py once, by path, under a private module name."""
    global _tools_mod
    if _tools_mod is None:
        for d in ("agent", "core"):
            p = os.path.join(REPO_ROOT, d)
            if p not in sys.path:
                sys.path.append(p)
        spec = importlib.util.spec_from_file_location(
            "bf_agent_tools", os.path.join(REPO_ROOT, "agent", "tools.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _tools_mod = mod
    return _tools_mod


def _real(p: str) -> str:
    return os.path.realpath(p)


def _inside(path: str, root: str) -> bool:
    root = _real(root)
    path = _real(path)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _int(v) -> Optional[int]:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


class Toolbox:
    def __init__(self, allowed_tools: List[str], workdir: str = "",
                 read_roots: Optional[List[str]] = None,
                 cmd_timeout: int = CMD_TIMEOUT, output_cap: int = OUTPUT_CAP,
                 write_paths: Optional[List[str]] = None):
        bad = [t for t in allowed_tools if t not in KNOWN_TOOLS]
        if bad:
            raise ValueError("tools not allowed for sub-agents: %s" % ", ".join(sorted(bad)))
        self.allowed = frozenset(allowed_tools)
        self.workdir = _real(workdir) if workdir else ""
        self.write_paths = [w.strip().lstrip("./") or "." for w in (write_paths or []) if w and w.strip()]
        roots = list(read_roots) if read_roots else [REPO_ROOT]
        if self.workdir:
            roots.append(self.workdir)
        self.read_roots = [_real(r) for r in roots]
        self.cmd_timeout = cmd_timeout
        self.output_cap = output_cap
        self.files_changed: List[str] = []
        self.base = self.workdir or self.read_roots[0]

    # ---- helpers ----
    def _resolve(self, params: Dict) -> str:
        raw = ""
        for k in _PATH_KEYS:
            if params.get(k):
                raw = str(params[k])
                break
        if not raw:
            return ""
        raw = os.path.expanduser(raw)
        return os.path.abspath(raw if os.path.isabs(raw) else os.path.join(self.base, raw))

    def _can_read(self, abs_path: str) -> Optional[str]:
        if not any(_inside(abs_path, r) for r in self.read_roots):
            return "ERROR: Access denied. %s is outside the folders this sub-agent may read." % abs_path
        if os.path.exists(abs_path) and not os.access(abs_path, os.R_OK):
            return "ERROR: Access denied. %s is not readable." % abs_path
        return None

    def _can_write(self, abs_path: str) -> Optional[str]:
        if not self.workdir:
            return "ERROR: Access denied. This sub-agent has no folder it may write to."
        if not _inside(abs_path, self.workdir):
            return "ERROR: Access denied. %s is outside the assigned folder (%s)." % (abs_path, self.workdir)
        if self.write_paths and not any(_inside(abs_path, os.path.join(self.workdir, w)) for w in self.write_paths):
            return "ERROR: Access denied. %s is outside the paths this worker may write: %s." % (abs_path, ", ".join(self.write_paths))
        return None

    def _note_change(self, abs_path: str, out: str) -> None:
        if not str(out).startswith("ERROR") and abs_path not in self.files_changed:
            self.files_changed.append(abs_path)

    # ---- entry point ----
    def execute(self, tool: str, params: Optional[Dict] = None) -> str:
        params = params or {}
        if tool not in self.allowed:
            return "ERROR: Tool '%s' is not available to this sub-agent." % tool
        try:
            return self._run(tool, params)
        except Exception as e:  # a tool bug must never kill the runner
            return "ERROR: %s failed: %s" % (tool, e)

    def _run(self, tool: str, params: Dict) -> str:
        t = _agent_tools()
        p = self._resolve(params)

        if tool == "run_command":
            return self._run_command(str(params.get("command", "")))

        if tool in WRITE_TOOLS:
            if not p:
                return "ERROR: %s requires a path." % tool
            err = self._can_write(p)
            if err:
                return err
            if tool == "write_file":
                if params.get("content") is None:
                    return "ERROR: write_file requires a content parameter."
                out = t.write_file(p, params["content"])
            elif tool == "edit_file":
                sv = params.get("search") if "search" in params else params.get("search_text")
                rv = params.get("replace") if "replace" in params else params.get("replace_text")
                out = t.edit_file(p, sv, rv, _int(params.get("start_line")), _int(params.get("end_line")),
                                  params.get("start_string"), params.get("end_string"))
            elif tool == "create_dir":
                out = t.create_directory(p)
            elif tool == "delete_file":
                if _real(p) == self.workdir:
                    return "ERROR: Refusing to delete the assigned folder itself."
                out = t.delete_item(p)
            else:  # rename_file
                raw_new = params.get("new_path") or ""
                if not raw_new:
                    return "ERROR: rename_file requires new_path."
                raw_new = os.path.expanduser(str(raw_new))
                new = os.path.abspath(raw_new if os.path.isabs(raw_new) else os.path.join(self.base, raw_new))
                err = self._can_write(new)
                if err:
                    return err
                out = t.rename_item(p, new)
            self._note_change(p, out)
            return out

        # read tools
        target = p or self.base
        err = self._can_read(target)
        if err:
            return err
        if tool == "read_file":
            return t.read_file(
                target, _int(params.get("start_line")), _int(params.get("end_line")),
                params.get("search_term") or params.get("search"),
                _int(params.get("surrounding_lines")) or 3,
                read_all=str(params.get("read_all", "")).strip().lower() == "true")
        if tool == "list_dir":
            return t.list_directory(target)
        return t.grep_search(target, params.get("search_text"))  # search_text

    # ---- run command ----
    def _run_command(self, cmd: str) -> str:
        if not cmd.strip():
            return "ERROR: run_command requires a command."
        if _SUDO_RE.search(cmd):
            return "ERROR: sudo, su and doas are not allowed for sub-agents."
        if not self.workdir:
            return "ERROR: Access denied. This sub-agent has no folder to run commands in."
        proc = subprocess.Popen(
            ["/bin/bash", "-c", cmd], cwd=self.workdir, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=self.cmd_timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            out, _ = proc.communicate()
            timed_out = True
        text = out.decode("utf-8", errors="replace")
        if len(text) > self.output_cap:
            text = text[: self.output_cap] + "\n[output truncated at %d characters]" % self.output_cap
        if timed_out:
            return text + "\nERROR: command timed out after %ds and was killed." % self.cmd_timeout
        return text + "\n[exit code: %d]" % proc.returncode
