import os
import tempfile
import unittest

from subagents import orphans as O


def fake_proc(entries):
    root = tempfile.mkdtemp()
    for pid, raw in entries.items():
        os.makedirs(os.path.join(root, str(pid)))
        with open(os.path.join(root, str(pid), "cmdline"), "wb") as f:
            f.write(raw)
    return root


WORK = "/tmp/bf_argv_work"


def one_string(port, name):
    s = ("/opt/google/chrome/chrome --user-data-dir=%s/chrome_%s --profile-directory=Profile 1 "
         "--remote-debugging-port=%d --window-size=500,1040 --no-first-run" % (WORK, name, port))
    return s.encode() + b"\0"


class TestSingleStringArgv(unittest.TestCase):
    def test_single_string_cmdline_is_split(self):
        root = fake_proc({101: one_string(9231, "abc")})
        args = O._read_args(root, "101")
        self.assertIn("--user-data-dir=%s/chrome_abc" % WORK, args)
        self.assertIn("--profile-directory=Profile 1", args)

    def test_finder_sees_single_string_chrome(self):
        root = fake_proc({101: one_string(9231, "abc")})
        self.assertEqual(O.find_orphans(WORK, proc_root=root), {"abc": [101]})

    def test_main_chrome_port_still_protected(self):
        root = fake_proc({102: one_string(9222, "abc")})
        self.assertEqual(O.find_orphans(WORK, proc_root=root), {})

    def test_live_run_is_skipped(self):
        root = fake_proc({103: one_string(9232, "live1")})
        self.assertEqual(O.find_orphans(WORK, live=["live1"], proc_root=root), {})

    def test_nul_separated_cmdline_unchanged(self):
        raw = b"chrome\0--user-data-dir=%s/chrome_x\0--remote-debugging-port=9233\0" % WORK.encode()
        root = fake_proc({104: raw})
        self.assertEqual(O.find_orphans(WORK, proc_root=root), {"x": [104]})


if __name__ == "__main__":
    unittest.main()
