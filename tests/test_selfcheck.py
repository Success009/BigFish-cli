# tests/test_selfcheck.py - startup self-check and known-good restore, all in a temp folder.
import io, os, shutil, subprocess, sys, tempfile, unittest
from contextlib import redirect_stderr

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
import selfcheck  # noqa


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class SelfcheckBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        write(os.path.join(self.tmp, "main.py"), "x = 1\n")
        write(os.path.join(self.tmp, "core", "util.py"), "def f():\n    return 2\n")
        # files in excluded folders must be ignored
        write(os.path.join(self.tmp, "backups", "old.py"), "this is not python (\n")
        write(os.path.join(self.tmp, "tests", "t.py"), "also not python (\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_check(self, argv=None, environ=None):
        err = io.StringIO()
        with redirect_stderr(err):
            code = selfcheck.run(self.tmp, argv or [], environ or {})
        return code, err.getvalue()

    def snap(self, rel):
        return os.path.join(self.tmp, "known_good", rel)


class TestSelfcheck(SelfcheckBase):
    def test_excluded_folders_ignored(self):
        files = selfcheck.find_py_files(self.tmp)
        self.assertEqual(files, ["core/util.py", "main.py"])

    def test_tilde_profile_folder_ignored(self):
        # The sandboxed Chrome profile lives in <project>/~ and can hold 250k files.
        write(os.path.join(self.tmp, "~", ".config", "x.py"), "x = 1\n")
        files = selfcheck.find_py_files(self.tmp)
        self.assertEqual(files, ["core/util.py", "main.py"])

    def test_clean_tree_creates_snapshot(self):
        code, _ = self.run_check()
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.snap("main.py")))
        self.assertTrue(os.path.exists(self.snap("core/util.py")))
        self.assertTrue(os.path.exists(self.snap(".fingerprint")))
        self.assertFalse(os.path.exists(self.snap("backups/old.py")))

    def test_unchanged_tree_takes_fast_path(self):
        self.run_check()
        mtime = os.path.getmtime(self.snap(".fingerprint"))
        write(os.path.join(self.tmp, "known_good", "marker.txt"), "keep")
        code, _ = self.run_check()
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.snap("marker.txt")))  # snapshot not rebuilt
        self.assertEqual(os.path.getmtime(self.snap(".fingerprint")), mtime)

    def test_changed_tree_updates_snapshot(self):
        self.run_check()
        write(os.path.join(self.tmp, "main.py"), "x = 99\n")
        code, _ = self.run_check()
        self.assertEqual(code, 0)
        with open(self.snap("main.py")) as f:
            self.assertEqual(f.read(), "x = 99\n")

    def test_syntax_error_blocks_and_keeps_old_snapshot(self):
        self.run_check()
        write(os.path.join(self.tmp, "main.py"), "def broken(:\n")
        code, err = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("main.py", err)
        self.assertIn("restore-known-good", err)
        with open(self.snap("main.py")) as f:
            self.assertEqual(f.read(), "x = 1\n")  # good snapshot untouched

    def test_syntax_error_without_snapshot_says_so(self):
        write(os.path.join(self.tmp, "main.py"), "def broken(:\n")
        code, err = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("No known-good snapshot", err)

    def test_safe_mode_never_blocks_or_snapshots(self):
        write(os.path.join(self.tmp, "main.py"), "def broken(:\n")
        code, _ = self.run_check(argv=["--safe"])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.snap("main.py")))

    def test_safe_env_var_never_blocks(self):
        write(os.path.join(self.tmp, "main.py"), "def broken(:\n")
        code, _ = self.run_check(environ={"BIGFISH_SAFE_INPUT": "1"})
        self.assertEqual(code, 0)

    def test_skip_check_flag_and_env(self):
        write(os.path.join(self.tmp, "main.py"), "def broken(:\n")
        self.assertEqual(self.run_check(argv=["--skip-check"])[0], 0)
        self.assertEqual(self.run_check(environ={"BIGFISH_SKIP_SELFCHECK": "1"})[0], 0)

    def test_failing_key_tests_block_and_do_not_snapshot(self):
        write(os.path.join(self.tmp, "tests", "test_keys.py"),
              "import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.fail('dead enter')\n")
        write(os.path.join(self.tmp, "tests", "__init__.py"), "")
        code, err = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("key tests failed", err)
        self.assertFalse(os.path.exists(self.snap("main.py")))

    def test_passing_key_tests_allow_snapshot(self):
        write(os.path.join(self.tmp, "tests", "test_keys.py"),
              "import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        pass\n")
        write(os.path.join(self.tmp, "tests", "__init__.py"), "")
        code, _ = self.run_check()
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.snap("main.py")))





if __name__ == "__main__":
    unittest.main()
