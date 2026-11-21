import shutil
"""Tests for subagents/toolbox.py (plan items R3, R4, part of R14)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from subagents.toolbox import Toolbox

READ = ["read_file", "list_dir", "search_text"]
WRITE = READ + ["write_file", "edit_file", "create_dir", "delete_file",
                "rename_file", "run_command"]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self.work = os.path.join(self.root, "work")
        self.other = os.path.join(self.root, "other")
        os.makedirs(self.work)
        os.makedirs(self.other)
        with open(os.path.join(self.work, "a.txt"), "w") as f:
            f.write("hello world\nsecond line\n")
        with open(os.path.join(self.other, "secret.txt"), "w") as f:
            f.write("secret")

    def tearDown(self):
        self._tmp.cleanup()

    def writer(self, **kw):
        return Toolbox(WRITE, workdir=self.work, read_roots=[self.work], **kw)

    def explorer(self):
        return Toolbox(READ, read_roots=[self.work])


class TestRoleFilter(Base):
    def test_disallowed_tool_refused(self):
        r = self.explorer().execute("write_file", {"path": "x.txt", "content": "x"})
        self.assertIn("not available", r)
        self.assertFalse(os.path.exists(os.path.join(self.work, "x.txt")))

    def test_unknown_tool_refused(self):
        self.assertIn("not available", self.writer().execute("start_background_job", {}))

    def test_forbidden_tool_cannot_be_granted(self):
        with self.assertRaises(ValueError):
            Toolbox(READ + ["web_search"])

    def test_explorer_has_no_write_folder(self):
        tb = Toolbox(["read_file", "write_file"], read_roots=[self.work])
        self.assertIn("no folder", tb.execute("write_file", {"path": "x", "content": "y"}))


class TestReads(Base):
    def test_read_inside(self):
        r = self.explorer().execute("read_file", {"path": "a.txt", "read_all": "true"})
        self.assertIn("hello world", r)

    def test_list_and_search(self):
        tb = self.explorer()
        self.assertIn("a.txt", tb.execute("list_dir", {"path": "."}))
        self.assertIn("hello", tb.execute("search_text", {"path": ".", "search_text": "hello"}))

    def test_read_outside_denied(self):
        tb = self.explorer()
        for p in (os.path.join(self.other, "secret.txt"), "../other/secret.txt"):
            r = tb.execute("read_file", {"path": p})
            self.assertIn("Access denied", r, p)
            self.assertNotIn("secret", r.replace("secret.txt", ""))

    def test_list_outside_denied(self):
        self.assertIn("Access denied", self.explorer().execute("list_dir", {"path": self.other}))

    def test_symlink_out_denied(self):
        os.symlink(os.path.join(self.other, "secret.txt"), os.path.join(self.work, "link.txt"))
        self.assertIn("Access denied", self.explorer().execute("read_file", {"path": "link.txt"}))

    def test_unreadable_file_denied_without_sudo(self):
        p = os.path.join(self.work, "locked.txt")
        with open(p, "w") as f:
            f.write("x")
        os.chmod(p, 0)
        try:
            if os.access(p, os.R_OK):
                self.skipTest("running as a user that ignores file modes")
            self.assertIn("not readable", self.explorer().execute("read_file", {"path": "locked.txt"}))
        finally:
            os.chmod(p, 0o600)


class TestWrites(Base):
    def test_write_edit_in_workdir(self):
        tb = self.writer()
        out = tb.execute("write_file", {"path": "n.txt", "content": "one\ntwo\n"})
        self.assertIn("Successfully wrote", out)
        tb.execute("edit_file", {"path": "n.txt", "search": "two", "replace": "three"})
        with open(os.path.join(self.work, "n.txt")) as f:
            self.assertEqual(f.read(), "one\nthree\n")
        self.assertEqual(tb.files_changed, [os.path.join(self.work, "n.txt")])

    def test_failed_write_not_recorded(self):
        tb = self.writer()
        tb.execute("write_file", {"path": "a.txt", "content": ""})  # refuses empty overwrite
        self.assertEqual(tb.files_changed, [])

    def test_write_outside_denied(self):
        tb = self.writer()
        for p in (os.path.join(self.other, "x.txt"), "../other/x.txt", "/tmp/bf_sub_escape.txt"):
            self.assertIn("Access denied", tb.execute("write_file", {"path": p, "content": "x"}), p)
        self.assertFalse(os.path.exists(os.path.join(self.other, "x.txt")))
        self.assertFalse(os.path.exists("/tmp/bf_sub_escape.txt"))

    def test_write_through_symlink_denied(self):
        os.symlink(self.other, os.path.join(self.work, "out"))
        r = self.writer().execute("write_file", {"path": "out/x.txt", "content": "x"})
        self.assertIn("Access denied", r)
        self.assertFalse(os.path.exists(os.path.join(self.other, "x.txt")))

    def test_delete_outside_denied_and_workdir_protected(self):
        tb = self.writer()
        self.assertIn("Access denied", tb.execute("delete_file", {"path": os.path.join(self.other, "secret.txt")}))
        self.assertTrue(os.path.exists(os.path.join(self.other, "secret.txt")))
        self.assertIn("Refusing", tb.execute("delete_file", {"path": "."}))
        self.assertTrue(os.path.isdir(self.work))

    def test_rename_destination_outside_denied(self):
        r = self.writer().execute("rename_file", {"path": "a.txt", "new_path": os.path.join(self.other, "a.txt")})
        self.assertIn("Access denied", r)
        self.assertTrue(os.path.exists(os.path.join(self.work, "a.txt")))

    def test_rename_inside_ok(self):
        r = self.writer().execute("rename_file", {"path": "a.txt", "new_path": "b.txt"})
        self.assertFalse(r.startswith("ERROR"), r)
        self.assertTrue(os.path.exists(os.path.join(self.work, "b.txt")))

    def test_create_dir(self):
        tb = self.writer()
        self.assertIn("Successfully", tb.execute("create_dir", {"path": "sub/dir"}))
        self.assertIn("Access denied", tb.execute("create_dir", {"path": self.other + "/new"}))

    def test_missing_params(self):
        tb = self.writer()
        self.assertIn("requires a path", tb.execute("write_file", {"content": "x"}))
        self.assertIn("requires a content", tb.execute("write_file", {"path": "x.txt"}))
        self.assertIn("requires new_path", tb.execute("rename_file", {"path": "a.txt"}))


class TestRunCommand(Base):
    def test_runs_in_workdir(self):
        r = self.writer().execute("run_command", {"command": "pwd"})
        self.assertIn(self.work, r)
        self.assertIn("[exit code: 0]", r)

    def test_exit_code_and_stderr(self):
        r = self.writer().execute("run_command", {"command": "echo oops >&2; exit 3"})
        self.assertIn("oops", r)
        self.assertIn("[exit code: 3]", r)

    def test_sudo_refused(self):
        tb = self.writer()
        for c in ("sudo ls", "ls; sudo rm x", "echo hi | su root", "doas id"):
            self.assertIn("not allowed", tb.execute("run_command", {"command": c}), c)

    def test_word_containing_su_is_fine(self):
        self.assertIn("[exit code: 0]", self.writer().execute("run_command", {"command": "echo result suffix"}))

    def test_timeout_kills(self):
        tb = self.writer(cmd_timeout=1)
        r = tb.execute("run_command", {"command": "sleep 30"})
        self.assertIn("timed out", r)

    def test_output_cap(self):
        tb = self.writer(output_cap=100)
        r = tb.execute("run_command", {"command": "yes x | head -c 5000"})
        self.assertIn("truncated", r)
        self.assertLess(len(r), 300)

    def test_no_stdin(self):
        r = self.writer().execute("run_command", {"command": "cat"})
        self.assertIn("[exit code: 0]", r)

    def test_empty_command(self):
        self.assertIn("requires a command", self.writer().execute("run_command", {"command": "  "}))

    def test_no_workdir_no_commands(self):
        tb = Toolbox(["run_command"], read_roots=[self.work])
        self.assertIn("no folder", tb.execute("run_command", {"command": "ls"}))



class TestWritePaths(Base):
    def test_write_outside_write_paths_denied(self):
        tb = self.writer(write_paths=["src"])
        out = tb.execute("write_file", {"path": "other_file.txt", "content": "x"})
        self.assertIn("Access denied", out)
        self.assertFalse(os.path.exists(os.path.join(self.work, "other_file.txt")))

    def test_write_inside_write_paths_allowed(self):
        os.makedirs(os.path.join(self.work, "src"))
        tb = self.writer(write_paths=["src"])
        out = tb.execute("write_file", {"path": "src/m.py", "content": "x = 1\n"})
        self.assertIn("Successfully wrote", out)

    def test_empty_write_paths_keeps_old_behaviour(self):
        tb = self.writer(write_paths=[])
        out = tb.execute("write_file", {"path": "anything.txt", "content": "x"})
        self.assertIn("Successfully wrote", out)

    def test_diff_drops_out_of_bounds_file(self):
        from subagents import isolation as ISO
        base = os.path.join(self.root, "base")
        os.makedirs(os.path.join(base, "src"))
        shutil.copytree(self.work, os.path.join(self.root, "w2"))
        w2 = os.path.join(self.root, "w2")
        os.makedirs(os.path.join(w2, "src"), exist_ok=True)
        with open(os.path.join(w2, "src", "m.py"), "w") as f:
            f.write("x = 1\n")
        with open(os.path.join(w2, "stray.txt"), "w") as f:
            f.write("stray\n")
        d = ISO.make_diff(base, w2, ["src"])
        self.assertIn("src/m.py", d)
        self.assertNotIn("+stray", d)
        self.assertIn("ignored changes outside write_paths", d)
        self.assertIn("stray.txt", d)

if __name__ == "__main__":
    unittest.main()
