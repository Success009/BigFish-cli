"""Tests for subagents/manager.py (plan items R12, R13, M1-M4, M6, part of R14). Offline."""
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from subagents import contracts as C
from subagents.fake import FakeChannel, summary_reply
from subagents.manager import Manager, ManagerError

READ_TOML = 'provider = "gemini"\ntools = ["read_file", "list_dir"]\nprompt = "Read only."\n'


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self.runs = os.path.join(self.root, "runs")
        self.roles = os.path.join(self.root, "roles")
        os.makedirs(self.roles)
        with open(os.path.join(self.roles, "explore.toml"), "w") as f:
            f.write(READ_TOML)
        self.gate = threading.Event()
        self.finish_lines = []
        self.managers = []

    def tearDown(self):
        self.gate.set()
        for m in self.managers:
            m.wait_all(10)
        self._tmp.cleanup()

    def quick(self, task, state):
        return FakeChannel([summary_reply("done: " + task.goal)])

    def blocking(self, task, state):
        return FakeChannel([lambda p: (self.gate.wait(10), summary_reply("late"))[1]])

    def mgr(self, factory=None, **kw):
        m = Manager(factory or self.quick, runs_dir=self.runs, roles_dir=self.roles,
                    on_finish=self.finish_lines.append, **kw)
        self.managers.append(m)
        return m


class TestSpawn(Base):
    def test_sequential_returns_summary_inline_and_no_notice(self):
        m = self.mgr()
        sid, text = m.spawn("explore", "find X", wait=True)
        self.assertEqual(text, "done: find X")
        self.assertEqual(m.drain_notices(), [])
        self.assertEqual(self.finish_lines, [])
        self.assertTrue(os.path.exists(C.result_path(sid, self.runs)))

    def test_sequential_long_summary_truncated(self):
        m = self.mgr(factory=lambda t, s: FakeChannel([summary_reply("y" * 9000)]))
        _, text = m.spawn("explore", "x", wait=True)
        self.assertIn("summary truncated", text)
        self.assertLess(len(text), C.SUMMARY_CAP_CHARS + 300)

    def test_sequential_failure_text(self):
        m = self.mgr(factory=lambda t, s: FakeChannel(["junk"] * 3))
        _, text = m.spawn("explore", "x", wait=True)
        self.assertIn("failed [bad_format]", text)

    def test_parallel_returns_at_once_then_one_notice(self):
        m = self.mgr(factory=self.blocking)
        t0 = time.time()
        sid, text = m.spawn("explore", "slow job")
        self.assertIsNone(text)
        self.assertLess(time.time() - t0, 2)
        self.assertEqual(m.drain_notices(), [])
        self.gate.set()
        self.assertTrue(m.wait_all())
        lines = m.drain_notices()
        self.assertEqual(len(lines), 1)
        self.assertIn(sid, lines[0])
        self.assertIn("done", lines[0])
        self.assertIn(C.result_path(sid, self.runs), lines[0])
        self.assertEqual(m.drain_notices(), [])
        self.assertEqual(self.finish_lines, lines)

    def test_two_finishing_together_give_two_lines(self):
        m = self.mgr(factory=self.blocking)
        a, _ = m.spawn("explore", "a")
        b, _ = m.spawn("explore", "b")
        self.gate.set()
        m.wait_all()
        lines = m.drain_notices()
        self.assertEqual(len(lines), 2)
        self.assertEqual({a, b}, {ln.split()[1] for ln in lines})

    def test_notice_waits_until_drained_after_main_turn_ended(self):
        m = self.mgr()
        sid, _ = m.spawn("explore", "x")
        m.wait_all()
        time.sleep(0.1)
        self.assertEqual(len(m.drain_notices()), 1)

    def test_bad_role_and_empty_brief(self):
        m = self.mgr()
        with self.assertRaises(ManagerError):
            m.spawn("nope", "x")
        with self.assertRaises(ManagerError):
            m.spawn("explore", "  ")

    def test_factory_failure_gives_failed_result_and_notice(self):
        def boom(task, state):
            raise RuntimeError("no browser")
        m = self.mgr(factory=boom)
        sid, _ = m.spawn("explore", "x")
        m.wait_all()
        line = m.drain_notices()[0]
        self.assertIn("failed [error]", line)
        self.assertIn("no browser", line)


class TestCapAndKillSwitch(Base):
    def test_cap_of_three(self):
        m = self.mgr(factory=self.blocking)
        for i in range(3):
            m.spawn("explore", "job %d" % i)
        with self.assertRaises(ManagerError) as cm:
            m.spawn("explore", "fourth")
        self.assertIn("cap reached", str(cm.exception))
        self.gate.set()
        m.wait_all()
        m.spawn("explore", "after")  # a slot is free again
        m.wait_all()

    def test_cap_is_configurable(self):
        m = self.mgr(factory=self.blocking, max_concurrent=1)
        m.spawn("explore", "a")
        with self.assertRaises(ManagerError):
            m.spawn("explore", "b")

    def test_sub_agent_cannot_get_spawn_tool(self):
        with self.assertRaises(C.ContractError):
            C.parse_role("x", READ_TOML.replace('"list_dir"', '"spawn"'))

    def test_cancel_one(self):
        m = self.mgr(factory=self.blocking)
        sid, _ = m.spawn("explore", "slow")
        self.assertIn("Cancel requested", m.cancel(sid))
        self.gate.set()
        m.wait_all()
        self.assertIn("cancelled", m.drain_notices()[0])
        with self.assertRaises(ManagerError):
            m.cancel(sid)

    def test_cancel_unknown_or_bad_id(self):
        m = self.mgr()
        for bad in ("sub_abcdef", "../x", ""):
            with self.assertRaises(ManagerError):
                m.cancel(bad)

    def test_kill_switch(self):
        m = self.mgr(factory=self.blocking)
        m.spawn("explore", "a")
        m.spawn("explore", "b")
        self.assertEqual(m.disable_all(), 2)
        with self.assertRaises(ManagerError):
            m.spawn("explore", "c")
        self.gate.set()
        m.wait_all()
        self.assertEqual(len(m.drain_notices()), 2)
        m.enable()
        m.spawn("explore", "d", wait=True)


class TestList(Base):
    def test_list_running_and_finished(self):
        m = self.mgr(factory=self.blocking)
        a, _ = m.spawn("explore", "a")
        self.assertIn("running", m.list_text())
        self.gate.set()
        m.wait_all()
        self.assertIn("done", m.list_text())
        self.assertIn(a, m.list_text())

    def test_empty_list(self):
        self.assertEqual(self.mgr().list_text(), "No sub-agents.")

    def test_corrupt_run_is_listed_not_crashing(self):
        os.makedirs(os.path.join(self.runs, "sub_abcdef"))
        with open(os.path.join(self.runs, "sub_abcdef", "task.json"), "w") as f:
            f.write("{")
        self.assertIn("corrupt", self.mgr().list_text())


class TestRecovery(Base):
    def make_crashed(self, with_state=True, with_result=False):
        role = C.load_role("explore", self.roles)
        task = C.make_task(role, "resume me")
        C.write_atomic(C.task_path(task.id, self.runs), task.to_json())
        if with_state:
            st = C.SavedState(id=task.id, status="running", provider="gemini", tokens_used=300, turns=2,
                              chat_url="fake://chat/9")
            C.write_atomic(C.state_path(task.id, self.runs), st.to_json())
        if with_result:
            res = C.Result(id=task.id, role="explore", status="done", summary="ok")
            C.write_atomic(C.result_path(task.id, self.runs), res.render())
        return task

    def test_unfinished_run_found_and_resumed(self):
        task = self.make_crashed()
        seen = []
        m = self.mgr(factory=lambda t, s: (seen.append(s), FakeChannel([summary_reply("resumed")]))[1])
        self.assertEqual(m.find_unfinished(), [task.id])
        self.assertIn("needs_resume", m.list_text())
        m.resume(task.id)
        m.wait_all()
        self.assertEqual(seen[0].chat_url, "fake://chat/9")
        self.assertEqual(seen[0].turns, 2)
        self.assertEqual(m.find_unfinished(), [])
        self.assertFalse(os.path.exists(C.state_path(task.id, self.runs)))
        self.assertIn("done", m.drain_notices()[0])

    def test_crash_before_first_turn_has_no_state(self):
        task = self.make_crashed(with_state=False)
        m = self.mgr()
        self.assertEqual(m.find_unfinished(), [task.id])
        m.resume(task.id)
        m.wait_all()
        self.assertIn("done", m.list_text())

    def test_result_and_state_both_present_is_finished(self):
        task = self.make_crashed(with_state=True, with_result=True)
        m = self.mgr()
        self.assertEqual(m.find_unfinished(), [])
        self.assertFalse(os.path.exists(C.state_path(task.id, self.runs)))

    def test_resume_refused_when_not_waiting_or_disabled(self):
        m = self.mgr()
        with self.assertRaises(ManagerError):
            m.resume("sub_abcdef")
        task = self.make_crashed()
        m.disable_all()
        with self.assertRaises(ManagerError):
            m.resume(task.id)

    def test_resume_with_missing_role_refused(self):
        task = self.make_crashed()
        os.remove(os.path.join(self.roles, "explore.toml"))
        with self.assertRaises(ManagerError):
            self.mgr().resume(task.id)


class TestCleanup(Base):
    def test_keeps_newest_n_and_never_touches_unfinished(self):
        m = self.mgr(keep_finished=2)
        ids = []
        for i in range(4):
            sid, _ = m.spawn("explore", "job %d" % i, wait=True)
            os.utime(C.result_path(sid, self.runs), (1000 + i, 1000 + i))
            ids.append(sid)
        role = C.load_role("explore", self.roles)
        crashed = C.make_task(role, "unfinished")
        C.write_atomic(C.task_path(crashed.id, self.runs), crashed.to_json())
        removed = m.clean()
        self.assertEqual(set(removed), {ids[0], ids[1]})
        self.assertFalse(os.path.exists(C.run_dir(ids[0], self.runs)))
        self.assertTrue(os.path.exists(C.run_dir(ids[3], self.runs)))
        self.assertTrue(os.path.exists(C.run_dir(crashed.id, self.runs)))

    def test_active_run_never_cleaned(self):
        m = self.mgr(factory=self.blocking, keep_finished=0)
        sid, _ = m.spawn("explore", "slow")
        self.assertEqual(m.clean(), [])
        self.assertTrue(os.path.exists(C.run_dir(sid, self.runs)))

    def test_nothing_to_clean(self):
        self.assertEqual(self.mgr().clean(), [])

    def test_clean_zero_removes_all_finished(self):
        m = self.mgr()
        ids = [m.spawn("explore", "job %d" % i, wait=True)[0] for i in range(3)]
        role = C.load_role("explore", self.roles)
        crashed = C.make_task(role, "unfinished")
        C.write_atomic(C.task_path(crashed.id, self.runs), crashed.to_json())
        self.assertEqual(m.clean(), [])
        self.assertEqual(set(m.clean(0)), set(ids))
        for sid in ids:
            self.assertFalse(os.path.exists(C.run_dir(sid, self.runs)))
        self.assertTrue(os.path.exists(C.run_dir(crashed.id, self.runs)))


if __name__ == "__main__":
    unittest.main()
