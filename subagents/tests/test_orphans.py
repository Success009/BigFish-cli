"""Tests for subagents/orphans.py (plan item A7). Offline: fake /proc, fake kill, no real process is signalled."""
import os
import shutil
import signal
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from subagents import orphans as O


def _fake_proc(root, pid, args):
    d = os.path.join(root, str(pid))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "cmdline"), "wb") as f:
        f.write(b"\0".join(a.encode() for a in args) + b"\0")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.proc = os.path.join(self.tmp, "proc")
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.proc)
        os.makedirs(self.work)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def chrome(self, pid, name, port=9230, root=None):
        root = root or self.work
        _fake_proc(self.proc, pid, ["/opt/google/chrome/chrome",
                                    "--user-data-dir=" + os.path.join(root, "chrome_" + name),
                                    "--remote-debugging-port=%d" % port])


class TestFind(Base):
    def test_finds_orphan(self):
        self.chrome(101, "sub_a")
        self.chrome(102, "sub_a")
        self.assertEqual(O.find_orphans(self.work, proc_root=self.proc), {"sub_a": [101, 102]})

    def test_live_name_skipped(self):
        self.chrome(101, "sub_a")
        self.chrome(102, "sub_b")
        self.assertEqual(O.find_orphans(self.work, live=["sub_a"], proc_root=self.proc), {"sub_b": [102]})

    def test_reserved_port_skipped(self):
        self.chrome(101, "sub_a", port=9222)
        self.assertEqual(O.find_orphans(self.work, proc_root=self.proc), {})

    def test_other_user_data_dir_skipped(self):
        _fake_proc(self.proc, 103, ["chrome", "--user-data-dir=/tmp/bf_sub_test", "--remote-debugging-port=9223"])
        self.chrome(104, "sub_a", root="/somewhere/else")
        self.assertEqual(O.find_orphans(self.work, proc_root=self.proc), {})

    def test_similar_names_map_separately(self):
        self.chrome(105, "x")
        self.chrome(106, "xy")
        self.assertEqual(O.find_orphans(self.work, proc_root=self.proc), {"x": [105], "xy": [106]})

    def test_missing_proc_root(self):
        self.assertEqual(O.find_orphans(self.work, proc_root=os.path.join(self.tmp, "nope")), {})

    def test_non_numeric_and_empty_entries_ignored(self):
        os.makedirs(os.path.join(self.proc, "self"))
        os.makedirs(os.path.join(self.proc, "200"))  # no cmdline file
        self.assertEqual(O.find_orphans(self.work, proc_root=self.proc), {})


class TestSweep(Base):
    def test_term_then_folders(self):
        self.chrome(101, "sub_a")
        for n in ("chrome_sub_a", "chrome_sub_live"):
            os.makedirs(os.path.join(self.work, n))
        os.makedirs(os.path.join(self.work, "unrelated"))
        calls = []

        def kill(pid, sig):
            calls.append((pid, sig))
            shutil.rmtree(os.path.join(self.proc, str(pid)), ignore_errors=True)  # process exits on TERM

        pids, removed = O.sweep(self.work, live=["sub_live"], grace=1.0, proc_root=self.proc, kill=kill)
        self.assertEqual(pids, [101])
        self.assertEqual(calls, [(101, signal.SIGTERM)])
        self.assertEqual(removed, ["chrome_sub_a"])
        self.assertTrue(os.path.isdir(os.path.join(self.work, "chrome_sub_live")))
        self.assertTrue(os.path.isdir(os.path.join(self.work, "unrelated")))

    def test_kill_sent_to_survivors(self):
        self.chrome(101, "sub_a")
        calls = []
        O.sweep(self.work, grace=0.3, proc_root=self.proc, kill=lambda p, s: calls.append((p, s)))
        self.assertEqual(calls, [(101, signal.SIGTERM), (101, signal.SIGKILL)])

    def test_kill_error_is_ignored(self):
        self.chrome(101, "sub_a")

        def kill(pid, sig):
            raise ProcessLookupError()

        pids, _ = O.sweep(self.work, grace=0.2, proc_root=self.proc, kill=kill)
        self.assertEqual(pids, [101])

    def test_reserved_port_never_signalled(self):
        self.chrome(101, "sub_a", port=9225)
        calls = []
        O.sweep(self.work, grace=0.2, proc_root=self.proc, kill=lambda p, s: calls.append(p))
        self.assertEqual(calls, [])

    def test_missing_work_root(self):
        pids, removed = O.sweep(os.path.join(self.tmp, "gone"), proc_root=self.proc, kill=lambda p, s: None)
        self.assertEqual((pids, removed), ([], []))


if __name__ == "__main__":
    unittest.main()
