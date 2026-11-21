# Offline tests for the sub-agent link-in helpers in main.py.
# No browser, no Chrome: the manager and factory are fakes.
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main


class FakeMgr:
    def __init__(self, spawn_result=None, raises=None):
        self.spawn_result = spawn_result or ("sub_1", None)
        self.raises = raises
        self.spawn_args = None
        self.cancelled = None

    def list_text(self):
        if self.raises:
            raise self.raises
        return "No sub-agents."

    def cancel(self, sub_id):
        if self.raises:
            raise self.raises
        self.cancelled = sub_id
        return "cancelled " + sub_id

    def spawn(self, role, brief, scope=None, wait=False, write_paths=None):
        if self.raises:
            raise self.raises
        self.spawn_args = (role, brief, scope, wait)
        return self.spawn_result


class TestRunSubagentTool(unittest.TestCase):
    def test_no_manager_gives_off_error(self):
        for tool in ("spawn_subagent", "list_subagents", "cancel_subagent"):
            out = main._run_subagent_tool(None, tool, {})
            self.assertTrue(out.startswith("ERROR:"), out)
            self.assertIn("failed to start", out)

    def test_list(self):
        self.assertEqual(main._run_subagent_tool(FakeMgr(), "list_subagents", {}), "No sub-agents.")

    def test_cancel_strips_id(self):
        m = FakeMgr()
        out = main._run_subagent_tool(m, "cancel_subagent", {"id": "  sub_9 "})
        self.assertEqual(m.cancelled, "sub_9")
        self.assertEqual(out, "cancelled sub_9")

    def test_spawn_parallel_message(self):
        m = FakeMgr(spawn_result=("sub_7", None))
        out = main._run_subagent_tool(m, "spawn_subagent", {"role": "explore", "brief": "look"})
        self.assertIn("sub_7", out)
        self.assertEqual(m.spawn_args, ("explore", "look", None, False))

    def test_spawn_wait_returns_text(self):
        m = FakeMgr(spawn_result=("sub_2", "the summary"))
        out = main._run_subagent_tool(m, "spawn_subagent",
                                      {"role": "explore", "brief": "b", "wait": "TRUE"})
        self.assertEqual(out, "the summary")
        self.assertTrue(m.spawn_args[3])

    def test_scope_string_is_split(self):
        m = FakeMgr()
        main._run_subagent_tool(m, "spawn_subagent",
                                {"role": "worker", "brief": "b", "scope": "a.py, b/c.py\nd.py"})
        self.assertEqual(m.spawn_args[2], ["a.py", "b/c.py", "d.py"])

    def test_empty_scope_becomes_none(self):
        m = FakeMgr()
        main._run_subagent_tool(m, "spawn_subagent", {"role": "worker", "brief": "b", "scope": " , "})
        self.assertIsNone(m.spawn_args[2])

    def test_errors_never_raise(self):
        m = FakeMgr(raises=RuntimeError("boom"))
        for tool in ("spawn_subagent", "list_subagents", "cancel_subagent"):
            out = main._run_subagent_tool(m, tool, {"role": "x", "brief": "y", "id": "z"})
            self.assertTrue(out.startswith("ERROR:"), out)
            self.assertIn("boom", out)


class TestStartSubagents(unittest.TestCase):
    def _run(self, sweep_raises=False):
        captured = {}

        class FakeFactory:
            def __init__(self, work_root, profiles=None, **kw):
                captured["work_root"] = work_root
                captured["profiles"] = profiles

            def startup_sweep(self):
                captured["swept"] = True
                if sweep_raises:
                    raise RuntimeError("sweep failed")

        class FakeManager:
            def __init__(self, factory, **kw):
                captured["manager_kwargs"] = kw
                captured["factory"] = factory

        with mock.patch("subagents.factory.SubFactory", FakeFactory), \
             mock.patch("subagents.manager.Manager", FakeManager), \
             mock.patch.object(main, "get_all_available_profiles",
                               return_value=["Default", "Profile 2", "Profile 7"]):
            mgr = main._start_subagents("Profile 2")
        return mgr, captured

    def test_main_profile_is_excluded(self):
        mgr, cap = self._run()
        self.assertEqual(cap["swept"], True)
        with mock.patch.object(main, "get_all_available_profiles",
                               return_value=["Default", "Profile 2", "Profile 7"]):
            self.assertEqual(list(cap["profiles"]()), ["Default", "Profile 7"])

    def test_manager_gets_finish_callback_and_source_root(self):
        mgr, cap = self._run()
        kw = cap["manager_kwargs"]
        self.assertTrue(callable(kw.get("on_finish")))
        self.assertEqual(kw.get("source_root"), os.getcwd())

    def test_work_root_is_outside_the_repo(self):
        mgr, cap = self._run()
        self.assertIn("subagents_work", cap["work_root"])
        self.assertTrue(os.path.isabs(cap["work_root"]))

    def test_sweep_failure_still_returns_manager(self):
        mgr, cap = self._run(sweep_raises=True)
        self.assertIsNotNone(mgr)


if __name__ == "__main__":
    unittest.main()
