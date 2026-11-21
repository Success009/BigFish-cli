import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest

from subagents import browserpool as B


def make_src(root):
    src = os.path.join(root, "src")
    def put(path, text):
        with open(path, "w") as fh:
            fh.write(text)
    for d in ("Cache", "Code Cache", "GPUCache", "Service Worker", "IndexedDB"):
        os.makedirs(os.path.join(src, "Profile 1", d))
        put(os.path.join(src, "Profile 1", d, "f"), "x")
    put(os.path.join(src, "Local State"), "{}")
    put(os.path.join(src, "Profile 1", "Cookies"), "c")
    os.makedirs(os.path.join(src, "Profile 2"))
    return src


def write_fake_binary(root, body):
    path = os.path.join(root, "fakechrome.py")
    with open(path, "w") as f:
        f.write("#!%s\n%s\n" % (sys.executable, body))
    os.chmod(path, 0o755)
    return path


class TestCopy(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.src = make_src(self.t)

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def test_recipe(self):
        dest = os.path.join(self.t, "d")
        B.build_profile_copy(self.src, "Profile 1", dest)
        self.assertEqual(sorted(os.listdir(dest)), ["Local State", "Profile 1"])
        self.assertEqual(sorted(os.listdir(os.path.join(dest, "Profile 1"))), ["Cookies", "IndexedDB"])

    def test_other_profiles_not_copied(self):
        dest = os.path.join(self.t, "d")
        B.build_profile_copy(self.src, "Profile 1", dest)
        self.assertFalse(os.path.exists(os.path.join(dest, "Profile 2")))

    def test_bad_names(self):
        for bad in ("", "..", ".", "../x", "a/b", "a\\b"):
            with self.assertRaises(B.PoolError):
                B.build_profile_copy(self.src, bad, os.path.join(self.t, "d"))
            self.assertFalse(os.path.exists(os.path.join(self.t, "d")))

    def test_missing_profile(self):
        with self.assertRaises(B.PoolError):
            B.build_profile_copy(self.src, "Profile 9", os.path.join(self.t, "d"))

    def test_dest_exists(self):
        dest = os.path.join(self.t, "d")
        os.makedirs(dest)
        with self.assertRaises(B.PoolError):
            B.build_profile_copy(self.src, "Profile 1", dest)
        self.assertTrue(os.path.isdir(dest))

    def test_source_untouched(self):
        B.build_profile_copy(self.src, "Profile 1", os.path.join(self.t, "d"))
        self.assertTrue(os.path.isfile(os.path.join(self.src, "Profile 1", "Cache", "f")))


class TestPorts(unittest.TestCase):
    def test_avoids_reserved(self):
        self.assertNotIn(B.pick_free_port(), B.RESERVED_PORTS)
        self.assertGreaterEqual(B.pick_free_port(start=9222), B.FIRST_PORT)

    def test_taken_skipped(self):
        p = B.pick_free_port()
        self.assertNotEqual(B.pick_free_port(taken=[p]), p)

    def test_bound_port_skipped(self):
        p = B.pick_free_port()
        s = socket.socket()
        s.bind(("127.0.0.1", p))
        try:
            self.assertNotEqual(B.pick_free_port(), p)
        finally:
            s.close()

    def test_exhausted(self):
        with self.assertRaises(B.PoolError):
            B.pick_free_port(start=9230, last=9231, taken=[9230, 9231])


class TestSlot(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.src = make_src(self.t)
        self.work = os.path.join(self.t, "work")
        os.makedirs(self.work)
        self.sleeper = write_fake_binary(self.t, "import time\ntime.sleep(60)")
        self.slots = []

    def tearDown(self):
        for s in self.slots:
            s.close()
        shutil.rmtree(self.t, ignore_errors=True)

    def slot(self, name="a", **kw):
        kw.setdefault("binary", self.sleeper)
        kw.setdefault("ready", lambda port: True)
        s = B.BrowserSlot(self.src, "Profile 1", self.work, name, **kw)
        self.slots.append(s)
        return s

    def test_start_and_close(self):
        s = self.slot()
        url = s.start()
        self.assertEqual(url, "http://127.0.0.1:%d" % s.port)
        self.assertIsNone(s.proc.poll())
        self.assertTrue(os.path.isdir(s.dest_dir))
        s.close()
        self.assertIsNone(s.proc)
        self.assertFalse(os.path.exists(s.dest_dir))

    def test_two_slots_get_different_ports_and_profiles(self):
        a, b = self.slot("a"), self.slot("b")
        a.start()
        b.port_taken = (a.port,)
        b.start()
        self.assertNotEqual(a.port, b.port)
        self.assertNotEqual(a.dest_dir, b.dest_dir)

    def test_stop_kills_only_own_process(self):
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            s = self.slot()
            s.start()
            s.stop()
            self.assertIsNone(other.poll())
        finally:
            other.kill()
            other.wait()

    def test_early_exit_cleans_up(self):
        quitter = write_fake_binary(self.t, "import sys\nsys.exit(3)")
        s = self.slot(binary=quitter, ready=lambda port: False)
        with self.assertRaises(B.PoolError) as cm:
            s.start()
        self.assertIn("exited early", str(cm.exception))
        self.assertFalse(os.path.exists(s.dest_dir))
        self.assertIsNone(s.proc)

    def test_ready_timeout_cleans_up(self):
        s = self.slot(ready=lambda port: False, ready_timeout=0.6)
        with self.assertRaises(B.PoolError):
            s.start()
        self.assertFalse(os.path.exists(s.dest_dir))
        self.assertIsNone(s.proc)

    def test_double_start_refused(self):
        s = self.slot()
        s.start()
        with self.assertRaises(B.PoolError):
            s.start()

    def test_bad_profile_name_refused_at_construction(self):
        with self.assertRaises(B.PoolError):
            B.BrowserSlot(self.src, "../x", self.work, "a")

    def test_args_use_own_dir_port_and_no_shell(self):
        rec = os.path.join(self.t, "args.txt")
        rb = write_fake_binary(self.t, "import sys,time\nopen(%r,'w').write('\\n'.join(sys.argv[1:]))\ntime.sleep(60)" % rec)
        s = self.slot(binary=rb, start_url="https://example.invalid/x")
        s.start()
        for _ in range(50):
            if os.path.exists(rec) and os.path.getsize(rec):
                break
            import time
            time.sleep(0.1)
        args = open(rec).read().split("\n")
        self.assertIn("--user-data-dir=" + s.dest_dir, args)
        self.assertIn("--profile-directory=Profile 1", args)
        self.assertIn("--remote-debugging-port=%d" % s.port, args)
        self.assertEqual(args[-1], "https://example.invalid/x")

    def test_stop_is_idempotent(self):
        s = self.slot()
        s.start()
        s.stop()
        s.stop()


if __name__ == "__main__":
    unittest.main()
