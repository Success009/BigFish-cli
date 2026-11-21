"""Tests for subagents/factory.py. Offline: fake channel class, profiles, recorder and /proc."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from subagents import contracts as C
from subagents import profile_pick as PP
from subagents.factory import SubFactory

ROLE = C.Role(name="explore", provider="gemini", tools=["read_file"], prompt="p")
PROFILES = ["Default", "Profile 1", "Profile 2"]


class FakeChannel:
    instances = []

    def __init__(self, provider, src, profile, work_root, name, **kw):
        self.provider, self.src, self.profile, self.work_root, self.name, self.kw = \
            provider, src, profile, work_root, name, kw
        self.closed = 0
        self.close_raises = False
        FakeChannel.instances.append(self)

    def close(self):
        self.closed += 1
        if self.close_raises:
            raise RuntimeError("boom")


class Base(unittest.TestCase):
    def setUp(self):
        FakeChannel.instances = []
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.work = os.path.join(self.tmp, "work")
        self.ledger = os.path.join(self.tmp, "ledger.json")
        self.recorded = []
        self.f = SubFactory(self.work, profiles=lambda: PROFILES, src_dir="/src", ledger_file=self.ledger,
                            channel_cls=FakeChannel,
                            record=lambda *a: self.recorded.append(a))

    def task(self):
        return C.make_task(ROLE, "goal")


class TestFactory(Base):
    def test_builds_channel_with_profile_and_name(self):
        t = self.task()
        ch = self.f(t)
        self.assertEqual((ch.provider, ch.src, ch.name, ch.work_root), ("gemini", "/src", t.id, self.work))
        self.assertIn(ch.profile, PROFILES)
        self.assertEqual(self.f.in_use(), {t.id: ch.profile})

    def test_two_concurrent_subs_get_different_profiles(self):
        a, b = self.f(self.task()), self.f(self.task())
        self.assertNotEqual(a.profile, b.profile)

    def test_all_in_use_raises_and_holds_nothing_extra(self):
        for _ in PROFILES:
            self.f(self.task())
        with self.assertRaises(PP.PickError):
            self.f(self.task())
        self.assertEqual(len(self.f.in_use()), 3)

    def test_release_on_close_even_if_close_raises(self):
        t = self.task()
        ch = self.f(t)
        ch.close_raises = True
        # close was wrapped after construction, so patch the original through the instance
        with self.assertRaises(RuntimeError):
            ch.close()
        self.assertEqual(self.f.in_use(), {})
        self.assertEqual(ch.closed, 1)

    def test_release_on_normal_close(self):
        ch = self.f(self.task())
        ch.close()
        self.assertEqual(self.f.in_use(), {})

    def test_channel_build_failure_releases(self):
        class Bad(FakeChannel):
            def __init__(self, *a, **k):
                raise RuntimeError("no")
        f = SubFactory(self.work, profiles=lambda: PROFILES, src_dir="/src", ledger_file=self.ledger,
                       channel_cls=Bad)
        with self.assertRaises(RuntimeError):
            f(self.task())
        self.assertEqual(f.in_use(), {})

    def test_resume_reuses_profile_and_url(self):
        st = C.SavedState(id="sub_abcdef", status="running", provider="gemini", profile="Profile 2",
                          chat_url="https://x.invalid/c/9")
        ch = self.f(self.task(), st)
        self.assertEqual(ch.profile, "Profile 2")
        self.assertEqual(ch.kw["resume_url"], "https://x.invalid/c/9")

    def test_fresh_run_has_no_resume_url(self):
        ch = self.f(self.task())
        self.assertIsNone(ch.kw["resume_url"])

    def test_on_turn_records_via_injected_recorder(self):
        ch = self.f(self.task())
        ch.kw["on_turn"]("Profile 1", "gemini", 10, 5)
        self.assertEqual(self.recorded, [("Profile 1", "gemini", 10, 5)])

    def test_on_turn_default_writes_real_ledger_file(self):
        f = SubFactory(self.work, profiles=lambda: PROFILES, src_dir="/src", ledger_file=self.ledger,
                       channel_cls=FakeChannel)
        ch = f(self.task())
        ch.kw["on_turn"]("Profile 1", "gemini", 100, 50)
        import json
        d = json.load(open(self.ledger))
        self.assertEqual(d["providers"]["gemini"]["Profile 1"]["total_tokens"], 150)

    def test_next_pick_sees_recorded_usage(self):
        f = SubFactory(self.work, profiles=lambda: PROFILES, src_dir="/src", ledger_file=self.ledger,
                       channel_cls=FakeChannel)
        a = f(self.task())
        a.kw["on_turn"](a.profile, "gemini", 5000, 1000)
        a.close()
        b = f(self.task())
        self.assertNotEqual(b.profile, a.profile)


class TestStartupSweep(Base):
    def test_removes_stale_copy_keeps_live(self):
        os.makedirs(os.path.join(self.work, "chrome_stale"))
        live = self.f(self.task())
        os.makedirs(os.path.join(self.work, "chrome_" + live.name))
        proc = os.path.join(self.tmp, "proc")
        os.makedirs(proc)
        pids, removed = self.f.startup_sweep(proc_root=proc, kill=lambda p, s: None, grace=0.1)
        self.assertEqual(removed, ["chrome_stale"])
        self.assertTrue(os.path.isdir(os.path.join(self.work, "chrome_" + live.name)))


if __name__ == "__main__":
    unittest.main()
