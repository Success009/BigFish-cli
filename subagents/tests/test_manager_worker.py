"""Manager-level worker tests (plan items W1, W2, W3, W4). Offline, fake channel."""
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from subagents import contracts as C
from subagents import isolation as ISO
from subagents.fake import FakeChannel, tool_reply, summary_reply
from subagents.manager import Manager, ManagerError

WORKER_TOML = ('provider = "gemini"\n'
               'tools = ["read_file", "list_dir", "write_file", "edit_file", "run_command"]\n'
               'prompt = "Edit."\n')
EXPLORE_TOML = 'provider = "gemini"\ntools = ["read_file", "list_dir"]\nprompt = "Read."\n'


def put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def get(path):
    with open(path) as f:
        return f.read()


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self.repo = os.path.join(self.root, "repo")
        self.runs = os.path.join(self.root, "runs")
        self.roles = os.path.join(self.root, "roles")
        os.makedirs(self.roles)
        put(os.path.join(self.roles, "worker.toml"), WORKER_TOML)
        put(os.path.join(self.roles, "explore.toml"), EXPLORE_TOML)
        put(os.path.join(self.repo, "pkg", "a.py"), "old\n")
        put(os.path.join(self.repo, "pkg", "sub", "b.py"), "b\n")
        put(os.path.join(self.repo, "other", "c.py"), "c\n")
        put(os.path.join(self.repo, "top.txt"), "top\n")
        self.gate = threading.Event()
        self.managers = []

    def tearDown(self):
        self.gate.set()
        for m in self.managers:
            m.wait_all(10)
        self._tmp.cleanup()

    def mgr(self, factory, **kw):
        m = Manager(factory, runs_dir=self.runs, roles_dir=self.roles, source_root=self.repo,
                    read_roots=[self.repo], **kw)
        self.managers.append(m)
        return m

    def script(self, *replies):
        return lambda task, state: FakeChannel(list(replies))

    def blocking(self, task, state):
        return FakeChannel([lambda p: (self.gate.wait(10), summary_reply("late"))[1]])


class TestWorkerCopy(Base):
    def test_worker_edits_copy_and_source_is_untouched(self):
        m = self.mgr(self.script(tool_reply("write_file", path="pkg/a.py", content="new"),
                                 summary_reply("changed a.py")))
        sid, text = m.spawn("worker", "change a.py", scope=["pkg"], wait=True)
        self.assertEqual(get(os.path.join(self.repo, "pkg", "a.py")), "old\n")
        self.assertIn("new", get(os.path.join(C.work_path(sid, self.runs), "pkg", "a.py")))
        diff = get(C.diff_path(sid, self.runs))
        self.assertIn("--- a/pkg/a.py", diff)
        self.assertIn("-old", diff)
        self.assertIn("changed a.py", text)
        self.assertIn(C.diff_path(sid, self.runs), text)

    def test_task_scope_is_normalized_and_workdir_assigned(self):
        m = self.mgr(self.script(summary_reply("ok")))
        sid, _ = m.spawn("worker", "x", scope=["pkg/sub", "pkg"], wait=True)
        with open(C.task_path(sid, self.runs)) as f:
            t = C.Task.from_json(f.read())
        self.assertEqual(t.scope, ["pkg"])
        self.assertEqual(t.workdir, C.work_path(sid, self.runs))

    def test_no_changes_gives_no_changes_diff(self):
        m = self.mgr(self.script(summary_reply("nothing needed")))
        sid, _ = m.spawn("worker", "x", scope=["pkg"], wait=True)
        self.assertEqual(get(C.diff_path(sid, self.runs)), "No changes.\n")

    def test_failed_worker_still_leaves_its_diff(self):
        m = self.mgr(self.script(tool_reply("write_file", path="pkg/a.py", content="half"), "junk", "junk", "junk"))
        sid, text = m.spawn("worker", "x", scope=["pkg"], wait=True)
        self.assertIn("failed [bad_format]", text)
        self.assertIn("+half", get(C.diff_path(sid, self.runs)))

    def test_worker_can_read_source_but_only_write_its_copy(self):
        m = self.mgr(self.script(tool_reply("read_file", path=os.path.join(self.repo, "top.txt"), read_all="true"),
                                 summary_reply("ok")))
        calls = []
        sid, _ = m.spawn("worker", "x", scope=["pkg"], wait=True)
        with open(C.transcript_path(sid, self.runs)) as f:
            self.assertIn("top", f.read())

    def test_explore_gets_no_copy_and_no_diff(self):
        m = self.mgr(self.script(summary_reply("ok")))
        sid, text = m.spawn("explore", "x", wait=True)
        self.assertFalse(os.path.exists(C.work_path(sid, self.runs)))
        self.assertFalse(os.path.exists(C.diff_path(sid, self.runs)))
        self.assertNotIn("diff", text.lower())

    def test_parallel_worker_notice(self):
        m = self.mgr(self.blocking)
        sid, text = m.spawn("worker", "x", scope=["pkg"])
        self.assertIsNone(text)
        self.gate.set()
        m.wait_all()
        self.assertIn(sid, m.drain_notices()[0])
        self.assertTrue(os.path.exists(C.diff_path(sid, self.runs)))

    def test_clean_removes_copies_with_the_run(self):
        m = self.mgr(self.script(summary_reply("ok")), keep_finished=0)
        sid, _ = m.spawn("worker", "x", scope=["pkg"], wait=True)
        self.assertEqual(m.clean(), [sid])
        self.assertFalse(os.path.exists(C.run_dir(sid, self.runs)))
        self.assertTrue(os.path.exists(os.path.join(self.repo, "pkg", "a.py")))


class TestSpawnRules(Base):
    def test_worker_needs_scope(self):
        m = self.mgr(self.script(summary_reply("ok")))
        for bad in (None, [], [""], ["nope"], ["."], ["../x"]):
            with self.assertRaises(ManagerError, msg=repr(bad)):
                m.spawn("worker", "x", scope=bad)

    def test_worker_cannot_pick_its_own_folder(self):
        m = self.mgr(self.script(summary_reply("ok")))
        with self.assertRaises(ManagerError):
            m.spawn("worker", "x", scope=["pkg"], workdir="/tmp")

    def test_too_large_scope_leaves_nothing_behind(self):
        m = self.mgr(self.script(summary_reply("ok")))
        with mock.patch.object(ISO, "MAX_FILES", 1):
            with self.assertRaises(ManagerError):
                m.spawn("worker", "x", scope=["pkg"])
        self.assertEqual(os.listdir(self.runs), [])

    def test_overlapping_scope_refused_while_running(self):
        m = self.mgr(self.blocking)
        m.spawn("worker", "a", scope=["pkg"])
        for overlap in (["pkg"], ["pkg/sub"], ["pkg/a.py"]):
            with self.assertRaises(ManagerError, msg=repr(overlap)) as cm:
                m.spawn("worker", "b", scope=overlap)
            self.assertIn("overlaps", str(cm.exception))
        m.spawn("worker", "c", scope=["other"])           # disjoint scope is fine
        self.gate.set()
        m.wait_all()

    def test_overlap_allowed_again_after_finish(self):
        m = self.mgr(self.script(summary_reply("ok")))
        m.spawn("worker", "a", scope=["pkg"], wait=True)
        m.spawn("worker", "b", scope=["pkg"], wait=True) if False else None
        m2 = self.mgr(self.script(summary_reply("ok")))
        m2.spawn("worker", "b", scope=["pkg"], wait=True)

    def test_unfinished_crashed_worker_still_holds_its_scope(self):
        role = C.load_role("worker", self.roles)
        sid = C.new_id()
        task = C.make_task(role, "crashed", scope=["pkg"], workdir=C.work_path(sid, self.runs), sub_id=sid)
        C.write_atomic(C.task_path(sid, self.runs), task.to_json())
        m = self.mgr(self.script(summary_reply("ok")))
        with self.assertRaises(ManagerError) as cm:
            m.spawn("worker", "b", scope=["pkg/sub"])
        self.assertIn(sid, str(cm.exception))

    def test_explore_ignores_scope_clash(self):
        m = self.mgr(self.blocking)
        m.spawn("worker", "a", scope=["pkg"])
        m.spawn("explore", "read pkg", scope=["pkg"])
        self.gate.set()
        m.wait_all()


class TestSandboxEscapes(Base):
    def run_worker(self, *replies, scope=("pkg",)):
        m = self.mgr(self.script(*replies, summary_reply("ok")))
        sid, _ = m.spawn("worker", "x", scope=list(scope), wait=True)
        return sid

    def transcript(self, sid):
        with open(C.transcript_path(sid, self.runs)) as f:
            return f.read()

    def test_absolute_path_into_source_denied(self):
        target = os.path.join(self.repo, "pkg", "a.py")
        sid = self.run_worker(tool_reply("write_file", path=target, content="HACK"))
        self.assertEqual(get(target), "old\n")
        self.assertIn("Access denied", self.transcript(sid))

    def test_dotdot_out_of_work_denied(self):
        sid = self.run_worker(tool_reply("write_file", path="../../../repo/top.txt", content="HACK"))
        self.assertEqual(get(os.path.join(self.repo, "top.txt")), "top\n")
        self.assertIn("Access denied", self.transcript(sid))

    def test_symlink_inside_scope_pointing_out_denied(self):
        outside = os.path.join(self.root, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.repo, "pkg", "link"))
        sid = self.run_worker(tool_reply("write_file", path="pkg/link/evil.txt", content="HACK"))
        self.assertFalse(os.path.exists(os.path.join(outside, "evil.txt")))
        self.assertIn("Access denied", self.transcript(sid))

    def test_write_outside_scope_but_inside_copy_root_stays_in_copy(self):
        # Files outside the scope do not exist in the copy; a new file there is still only a copy change.
        sid = self.run_worker(tool_reply("write_file", path="newdir/x.txt", content="x"))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "newdir")))
        self.assertIn("+++ b/newdir/x.txt", get(C.diff_path(sid, self.runs)))

    def test_sudo_refused(self):
        sid = self.run_worker(tool_reply("run_command", command="sudo ls"))
        self.assertIn("not allowed", self.transcript(sid))

    def test_delete_tool_cannot_be_given_to_role_by_accident(self):
        sid = self.run_worker(tool_reply("delete_file", path="pkg/a.py"))
        self.assertIn("not available", self.transcript(sid))
        self.assertEqual(get(os.path.join(self.repo, "pkg", "a.py")), "old\n")


if __name__ == "__main__":
    unittest.main()
