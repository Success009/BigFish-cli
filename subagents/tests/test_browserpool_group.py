import os
import subprocess
import sys
import tempfile
import time
import unittest

from subagents import browserpool as BP

PARENT_CODE = (
    "import subprocess, sys, time\n"
    "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print(c.pid, flush=True)\n"
    "time.sleep(60)\n"
)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


class TestStopKillsGroup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.extra = []

    def tearDown(self):
        for p in self.extra:
            try:
                p.kill()
                p.wait(timeout=2)
            except Exception:
                pass

    def make_slot(self):
        slot = BP.BrowserSlot(self.tmp, "Profile 1", self.tmp, "t1")
        return slot

    def test_stop_ends_parent_and_child(self):
        slot = self.make_slot()
        slot.proc = subprocess.Popen([sys.executable, "-c", PARENT_CODE], stdout=subprocess.PIPE,
                                     start_new_session=True)
        child_pid = int(slot.proc.stdout.readline().decode().strip())
        parent_pid = slot.proc.pid
        self.assertTrue(alive(child_pid))
        slot.stop(grace=3)
        time.sleep(0.3)
        self.assertFalse(alive(parent_pid))
        self.assertFalse(alive(child_pid))
        self.assertIsNone(slot.proc)

    def test_stop_leaves_other_sessions_alone(self):
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 start_new_session=True)
        self.extra.append(other)
        slot = self.make_slot()
        slot.proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                     start_new_session=True)
        slot.stop(grace=3)
        self.assertTrue(alive(other.pid))

    def test_stop_without_process_is_noop(self):
        self.make_slot().stop()


if __name__ == "__main__":
    unittest.main()
