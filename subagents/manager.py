"""subagents.manager - the main-agent side (plan items R12, R13, M1-M4, M5 hook, M6).

Plain Python API, NOT wired into main.py (link-in gate). Operations the main agent will get:
  spawn(role, brief, scope, workdir, wait)  list_text()  cancel(id)
User commands: disable_all() (/subs off), clean() (/subs clean).
Harness hooks: drain_notices() (one line per finished parallel sub), on_finish callback
(terminal finish line for the user), find_unfinished() / resume() for crash recovery.

Everything about a run lives in its folder (contracts.py), so the list is rebuilt from disk
and survives a restart. A run with a result file is finished; a run with task.json but no result
that is not active in this process is needs_resume.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from subagents import contracts as C
from subagents import isolation as ISO
from subagents.runner import Runner
from subagents.toolbox import Toolbox, REPO_ROOT


class ManagerError(Exception):
    """A request the manager refuses (cap reached, disabled, bad role, unknown id)."""


class Manager:
    def __init__(self, channel_factory: Callable, runs_dir: Optional[str] = None,
                 roles_dir: Optional[str] = None, max_concurrent: int = C.MAX_CONCURRENT,
                 keep_finished: int = C.KEEP_FINISHED,
                 on_finish: Optional[Callable[[str], None]] = None,
                 read_roots: Optional[List[str]] = None,
                 source_root: Optional[str] = None):
        """channel_factory(task, saved_state_or_None) -> a channel with send(prompt)."""
        self.factory = channel_factory
        self.runs_dir = runs_dir or C.RUNS_DIR
        self.roles_dir = roles_dir
        self.max_concurrent = max_concurrent
        self.keep_finished = keep_finished
        self.on_finish = on_finish
        self.read_roots = read_roots or [REPO_ROOT]
        self.source_root = source_root or REPO_ROOT   # repo that worker scopes are copied from
        self.enabled = True
        self._lock = threading.Lock()
        self._active: Dict[str, Tuple[threading.Thread, threading.Event]] = {}
        self._notices: List[str] = []
        os.makedirs(self.runs_dir, exist_ok=True)

    # ---- spawn ----
    def spawn(self, role: str, brief: str, scope: Optional[List[str]] = None,
              workdir: str = "", wait: bool = False,
              write_paths: Optional[List[str]] = None) -> Tuple[str, Optional[str]]:
        """Parallel: returns (id, None) at once. wait=True: blocks, returns (id, text for the model)."""
        if not self.enabled:
            raise ManagerError("sub-agents are switched off (/subs off)")
        rels = None
        try:
            r = C.load_role(role, self.roles_dir)
            if r.writes:
                # A worker's folder is a private copy of its scope, assigned by the harness.
                if workdir:
                    raise ManagerError("a worker's folder is assigned by the harness; do not pass workdir")
                sub_id = C.new_id()
                rels = ISO.normalize_scope(scope or [], self.source_root, self.runs_dir)
                task = C.make_task(r, brief, scope=rels, workdir=C.work_path(sub_id, self.runs_dir),
                                   wait=wait, sub_id=sub_id, write_paths=write_paths)
            else:
                task = C.make_task(r, brief, scope=scope, workdir=workdir, wait=wait, write_paths=write_paths)
        except (C.ContractError, ISO.IsolationError) as e:
            raise ManagerError(str(e))
        with self._lock:
            if len(self._active) >= self.max_concurrent:
                raise ManagerError("cap reached: %d sub-agents are already running" % self.max_concurrent)
            if rels is not None:
                clash = self._scope_clash(rels)
                if clash:
                    raise ManagerError("scope overlaps worker %s, which has not finished" % clash)
                try:
                    ISO.prepare(C.run_dir(task.id, self.runs_dir), self.source_root, rels)
                except (ISO.IsolationError, OSError) as e:
                    shutil.rmtree(C.run_dir(task.id, self.runs_dir), ignore_errors=True)
                    raise ManagerError("could not prepare the worker folder: %s" % e)
            C.write_atomic(C.task_path(task.id, self.runs_dir), task.to_json())
            th = self._start(task, r, None)
        if not wait:
            return task.id, None
        th.join()
        return task.id, self._inline_text(task)

    def _start(self, task: C.Task, role: C.Role, resume: Optional[C.SavedState]) -> threading.Thread:
        """Caller holds the lock. Registers the run BEFORE the thread starts (atomic cap)."""
        cancel = threading.Event()
        th = threading.Thread(target=self._worker, args=(task, role, cancel, resume),
                              name="subagent-" + task.id, daemon=True)
        self._active[task.id] = (th, cancel)
        th.start()
        return th

    def _worker(self, task, role, cancel, resume):
        channel = None
        try:
            channel = self.factory(task, resume)
            tb = Toolbox(task.allowed_tools, workdir=task.workdir, read_roots=self.read_roots,
                         write_paths=task.write_paths)
            res = Runner(task, role.prompt, channel, tb, runs_dir=self.runs_dir,
                         cancel=cancel, resume=resume).run()
        except Exception as e:  # e.g. the channel could not be created
            res = self._write_failed(task, "could not start: %s: %s" % (type(e).__name__, e))
        finally:
            closer = getattr(channel, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass
        if set(task.allowed_tools) & C.WRITE_TOOLS:
            self._write_diff(task)
        line = C.format_notice(task.id, task.role, res.status, res.reason, res.detail, self.runs_dir)
        with self._lock:
            self._active.pop(task.id, None)
            if not task.wait:
                self._notices.append(line)
        if not task.wait and self.on_finish:
            try:
                self.on_finish(line)
            except Exception:
                pass

    def _scope_clash(self, rels: List[str]) -> Optional[str]:
        """Id of an unfinished worker (running or needs_resume) whose scope overlaps rels."""
        for sid in self._ids():
            if os.path.exists(C.result_path(sid, self.runs_dir)):
                continue
            try:
                with open(C.task_path(sid, self.runs_dir), encoding="utf-8") as f:
                    t = C.Task.from_json(f.read())
            except (OSError, C.ContractError):
                continue
            if set(t.allowed_tools) & C.WRITE_TOOLS and ISO.overlaps(rels, t.scope):
                return sid
        return None

    def _write_diff(self, task: C.Task) -> None:
        path = C.diff_path(task.id, self.runs_dir)
        try:
            ISO.write_diff(C.base_path(task.id, self.runs_dir), C.work_path(task.id, self.runs_dir), path,
                           task.write_paths)
        except Exception as e:  # the diff must never break finishing; say what went wrong in the file
            try:
                C.write_atomic(path, "Could not build the diff: %s: %s\n" % (type(e).__name__, e))
            except OSError:
                pass

    def _write_failed(self, task: C.Task, detail: str) -> C.Result:
        now = time.time()
        res = C.Result(id=task.id, role=task.role, status=C.FAILED, reason=C.ERROR, detail=detail,
                       summary="Sub-agent failed before doing any work: " + detail, partial=True,
                       unfinished=["task not started"], started_at=now, finished_at=now)
        C.write_atomic(C.result_path(task.id, self.runs_dir), res.render())
        try:
            os.remove(C.state_path(task.id, self.runs_dir))
        except OSError:
            pass
        return res

    def _inline_text(self, task: C.Task) -> str:
        text = self._inline_base(task)
        if set(task.allowed_tools) & C.WRITE_TOOLS:
            text += ("\nChanges were made in a private copy. Review the diff and run the tests before applying: "
                     + C.diff_path(task.id, self.runs_dir))
        return text

    def _inline_base(self, task: C.Task) -> str:
        res = self._read_result(task.id)
        if res is None:
            return "Sub-agent %s ended without a readable result file." % task.id
        if res.status == C.DONE:
            return res.inline_summary(task.summary_cap)
        return "Sub-agent %s %s [%s]: %s\n%s" % (task.id, res.status, res.reason, res.detail,
                                                 res.inline_summary(task.summary_cap))

    def _read_result(self, sub_id: str) -> Optional[C.Result]:
        try:
            with open(C.result_path(sub_id, self.runs_dir), encoding="utf-8") as f:
                return C.Result.parse(f.read())
        except (OSError, C.ContractError):
            return None

    # ---- list / cancel ----
    def _ids(self) -> List[str]:
        try:
            names = os.listdir(self.runs_dir)
        except OSError:
            return []
        return sorted(n for n in names if os.path.isdir(os.path.join(self.runs_dir, n))
                      and C._ID_RE.match(n))

    def entries(self) -> List[dict]:
        out = []
        with self._lock:
            active = set(self._active)
        for sid in self._ids():
            e = {"id": sid, "role": "?", "status": "corrupt", "reason": "", "turns": 0, "tokens": 0}
            try:
                with open(C.task_path(sid, self.runs_dir), encoding="utf-8") as f:
                    e["role"] = C.Task.from_json(f.read()).role
            except (OSError, C.ContractError):
                out.append(e)
                continue
            res = self._read_result(sid) if os.path.exists(C.result_path(sid, self.runs_dir)) else None
            if os.path.exists(C.result_path(sid, self.runs_dir)):
                e["status"] = res.status if res else "corrupt"
                if res:
                    e.update(reason=res.reason, turns=res.turns, tokens=res.tokens_used)
            elif sid in active:
                e["status"] = C.RUNNING
                st = self._read_state(sid)
                if st:
                    e.update(turns=st.turns, tokens=st.tokens_used)
            else:
                e["status"] = C.NEEDS_RESUME
            out.append(e)
        return out

    def list_text(self) -> str:
        rows = self.entries()
        if not rows:
            return "No sub-agents."
        lines = []
        for e in rows:
            extra = " [%s]" % e["reason"] if e["reason"] else ""
            lines.append("%s  %-8s %-12s turns=%d tokens~%d%s" % (
                e["id"], e["role"], e["status"], e["turns"], e["tokens"], extra))
        return "\n".join(lines)

    def _read_state(self, sub_id: str) -> Optional[C.SavedState]:
        try:
            with open(C.state_path(sub_id, self.runs_dir), encoding="utf-8") as f:
                return C.SavedState.from_json(f.read())
        except (OSError, C.ContractError):
            return None

    def cancel(self, sub_id: str) -> str:
        try:
            C.validate_id(sub_id)
        except C.ContractError:
            raise ManagerError("bad sub-agent id: %r" % (sub_id,))
        with self._lock:
            item = self._active.get(sub_id)
        if item is None:
            raise ManagerError("%s is not running" % sub_id)
        item[1].set()
        return "Cancel requested for %s. It stops at the next safe point." % sub_id

    def disable_all(self) -> int:
        """/subs off: no new spawns, cancel everything running. Returns how many were cancelled."""
        self.enabled = False
        with self._lock:
            events = [ev for (_, ev) in self._active.values()]
        for ev in events:
            ev.set()
        return len(events)

    def enable(self) -> None:
        self.enabled = True

    def wait_all(self, timeout: float = 30.0) -> bool:
        """Block until no run is active (tests and shutdown). True if all finished in time."""
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                threads = [t for (t, _) in self._active.values()]
            if not threads:
                return True
            for t in threads:
                t.join(max(0.0, min(0.2, end - time.time())))
        with self._lock:
            return not self._active

    # ---- notices ----
    def drain_notices(self) -> List[str]:
        """One line per parallel sub that finished since the last drain. Empties the queue."""
        with self._lock:
            out, self._notices = self._notices, []
        return out

    # ---- crash recovery ----
    def find_unfinished(self) -> List[str]:
        """Start-up scan. A run that has a result file is finished: a leftover state file is removed.
        Runs with a task but no result, not active here, need resume."""
        found = []
        with self._lock:
            active = set(self._active)
        for sid in self._ids():
            if os.path.exists(C.result_path(sid, self.runs_dir)):
                try:
                    os.remove(C.state_path(sid, self.runs_dir))
                except OSError:
                    pass
                continue
            if sid in active or not os.path.exists(C.task_path(sid, self.runs_dir)):
                continue
            try:
                with open(C.task_path(sid, self.runs_dir), encoding="utf-8") as f:
                    C.Task.from_json(f.read())
            except (OSError, C.ContractError):
                continue
            found.append(sid)
        return found

    def resume(self, sub_id: str) -> str:
        if not self.enabled:
            raise ManagerError("sub-agents are switched off (/subs off)")
        if sub_id not in self.find_unfinished():
            raise ManagerError("%s is not waiting for resume" % sub_id)
        with open(C.task_path(sub_id, self.runs_dir), encoding="utf-8") as f:
            task = C.Task.from_json(f.read())
        try:
            role = C.load_role(task.role, self.roles_dir)
        except C.ContractError as e:
            raise ManagerError(str(e))
        st = self._read_state(sub_id)
        with self._lock:
            if len(self._active) >= self.max_concurrent:
                raise ManagerError("cap reached: %d sub-agents are already running" % self.max_concurrent)
            self._start(task, role, st)
        return "Resumed %s." % sub_id

    # ---- cleanup ----
    def clean(self, keep: Optional[int] = None) -> List[str]:
        """Keep the newest keep_finished finished runs; delete older ones. Never touches a run
        without a result file (unfinished or needs_resume) or one that is active. Worker folders
        are NOT deleted here (the manager does not own them; see plan W1).
        keep=None uses self.keep_finished; keep=0 removes every finished run."""
        with self._lock:
            active = set(self._active)
        finished = []
        for sid in self._ids():
            rp = C.result_path(sid, self.runs_dir)
            if sid in active or not os.path.exists(rp):
                continue
            try:
                finished.append((os.path.getmtime(rp), sid))
            except OSError:
                continue
        finished.sort(reverse=True)
        removed = []
        limit = self.keep_finished if keep is None else max(0, int(keep))
        for _, sid in finished[limit:]:
            shutil.rmtree(C.run_dir(sid, self.runs_dir), ignore_errors=True)
            removed.append(sid)
        return removed
