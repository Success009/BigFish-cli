"""Tests for subagents/isolation.py and the path helpers (plan items W1, W2, W3 diff)."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from subagents import contracts as C
from subagents import isolation as ISO


def put(path, text, mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode) as f:
        f.write(text)


def get(path, mode="r"):
    with open(path, mode) as f:
        return f.read()


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self.repo = os.path.join(self.root, "repo")
        self.runs = os.path.join(self.repo, "subagents", "runs")
        os.makedirs(self.runs)
        put(os.path.join(self.repo, "pkg", "a.py"), "old\n")
        put(os.path.join(self.repo, "pkg", "sub", "b.py"), "b\n")
        put(os.path.join(self.repo, "other", "c.py"), "c\n")

    def tearDown(self):
        self._tmp.cleanup()

    def norm(self, scope):
        return ISO.normalize_scope(scope, self.repo, self.runs)


class TestScope(Base):
    def test_relative_and_absolute(self):
        self.assertEqual(self.norm(["pkg", os.path.join(self.repo, "other", "c.py")]), ["other/c.py", "pkg"])

    def test_nested_and_duplicates_dropped(self):
        self.assertEqual(self.norm(["pkg/sub", "pkg", "pkg", "pkg/a.py"]), ["pkg"])

    def test_rejected(self):
        outside = os.path.join(self.root, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.repo, "pkg", "link"))
        for bad in ([], [""], ["  "], ["."], [self.repo], ["../outside"], [outside], ["nope"], ["subagents"],
                    ["pkg/link"], [5]):
            with self.assertRaises(ISO.IsolationError, msg=repr(bad)):
                self.norm(bad)

    def test_overlaps(self):
        self.assertTrue(ISO.overlaps(["pkg"], ["pkg/sub"]))
        self.assertTrue(ISO.overlaps(["pkg/sub/b.py"], ["pkg"]))
        self.assertTrue(ISO.overlaps(["pkg"], ["pkg"]))
        self.assertFalse(ISO.overlaps(["pkg"], ["pkg2"]))
        self.assertFalse(ISO.overlaps(["pkg"], ["other"]))
        self.assertFalse(ISO.overlaps(["pkg/a.py"], ["pkg/sub"]))


class TestPrepare(Base):
    def prep(self, rels):
        rd = os.path.join(self.runs, "sub_abcdef")
        os.makedirs(rd)
        return rd, ISO.prepare(rd, self.repo, rels)

    def test_copies_scope_to_base_and_work(self):
        rd, work = self.prep(["pkg", "other/c.py"])
        self.assertEqual(work, os.path.join(rd, "work"))
        for d in ("base", "work"):
            self.assertEqual(get(os.path.join(rd, d, "pkg", "a.py")), "old\n")
            self.assertEqual(get(os.path.join(rd, d, "pkg", "sub", "b.py")), "b\n")
            self.assertEqual(get(os.path.join(rd, d, "other", "c.py")), "c\n")
        self.assertFalse(os.path.exists(os.path.join(rd, "work", "other", "other")))

    def test_only_scope_is_copied(self):
        rd, _ = self.prep(["pkg"])
        self.assertFalse(os.path.exists(os.path.join(rd, "work", "other")))

    def test_skip_dirs_not_copied(self):
        put(os.path.join(self.repo, "pkg", "__pycache__", "x.pyc"), "x")
        put(os.path.join(self.repo, "pkg", ".git", "HEAD"), "x")
        rd, _ = self.prep(["pkg"])
        self.assertFalse(os.path.exists(os.path.join(rd, "work", "pkg", "__pycache__")))
        self.assertFalse(os.path.exists(os.path.join(rd, "work", "pkg", ".git")))

    def test_too_many_files(self):
        with mock.patch.object(ISO, "MAX_FILES", 1):
            with self.assertRaises(ISO.IsolationError):
                self.prep(["pkg"])

    def test_too_big(self):
        with mock.patch.object(ISO, "MAX_BYTES", 3):
            with self.assertRaises(ISO.IsolationError):
                self.prep(["pkg"])

    def test_symlink_kept_as_link(self):
        os.symlink("a.py", os.path.join(self.repo, "pkg", "lnk"))
        rd, _ = self.prep(["pkg"])
        self.assertTrue(os.path.islink(os.path.join(rd, "work", "pkg", "lnk")))


class TestDiff(Base):
    def setUp(self):
        super().setUp()
        self.rd = os.path.join(self.runs, "sub_abcdef")
        os.makedirs(self.rd)
        ISO.prepare(self.rd, self.repo, ["pkg"])
        self.base, self.work = os.path.join(self.rd, "base"), os.path.join(self.rd, "work")

    def test_no_change(self):
        self.assertEqual(ISO.make_diff(self.base, self.work), "No changes.\n")

    def test_modify_add_delete(self):
        put(os.path.join(self.work, "pkg", "a.py"), "new\n")
        put(os.path.join(self.work, "pkg", "n.py"), "fresh\n")
        os.remove(os.path.join(self.work, "pkg", "sub", "b.py"))
        d = ISO.make_diff(self.base, self.work)
        self.assertIn("--- a/pkg/a.py\n+++ b/pkg/a.py", d)
        self.assertIn("-old\n+new\n", d)
        self.assertIn("--- /dev/null\n+++ b/pkg/n.py", d)
        self.assertIn("--- a/pkg/sub/b.py\n+++ /dev/null", d)

    def test_no_trailing_newline_marker(self):
        put(os.path.join(self.work, "pkg", "a.py"), "old")
        self.assertIn("\\ No newline at end of file", ISO.make_diff(self.base, self.work))

    def test_binary_and_empty_file_notes(self):
        put(os.path.join(self.work, "pkg", "img.bin"), b"\x00\x01\x02", "wb")
        put(os.path.join(self.work, "pkg", "empty.txt"), "")
        d = ISO.make_diff(self.base, self.work)
        self.assertIn("Binary files a/pkg/img.bin and b/pkg/img.bin differ", d)
        self.assertIn("pkg/empty.txt changed in a way a patch cannot express", d)

    def test_symlinks_ignored(self):
        os.symlink("a.py", os.path.join(self.work, "pkg", "newlink"))
        self.assertEqual(ISO.make_diff(self.base, self.work), "No changes.\n")

    def test_write_diff_file(self):
        put(os.path.join(self.work, "pkg", "a.py"), "new\n")
        p = C.diff_path("sub_abcdef", self.runs)
        ISO.write_diff(self.base, self.work, p)
        self.assertIn("+new", get(p))
        self.assertFalse(os.path.exists(p + ".tmp"))

    @unittest.skipUnless(shutil.which("patch"), "patch is not installed")
    def test_patch_round_trip_applies_cleanly(self):
        put(os.path.join(self.work, "pkg", "a.py"), "new\nmore")            # no trailing newline
        put(os.path.join(self.work, "pkg", "n.py"), "fresh\r\nline\r\n")     # CRLF, new file
        put(os.path.join(self.work, "pkg", "u.txt"), b"caf\xe9\n", "wb")     # not valid UTF-8
        os.remove(os.path.join(self.work, "pkg", "sub", "b.py"))
        p = os.path.join(self.root, "changes.diff")
        ISO.write_diff(self.base, self.work, p)
        target = os.path.join(self.root, "applied")
        shutil.copytree(self.repo, target, ignore=shutil.ignore_patterns("subagents"))
        r = subprocess.run(["patch", "-p1", "-d", target, "-i", p], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for rel in ("pkg/a.py", "pkg/n.py", "pkg/u.txt"):
            self.assertEqual(get(os.path.join(target, rel), "rb"), get(os.path.join(self.work, rel), "rb"), rel)
        self.assertFalse(os.path.exists(os.path.join(target, "pkg", "sub", "b.py")))


class TestPaths(unittest.TestCase):
    def test_paths_are_inside_the_run_folder(self):
        for fn, name in ((C.work_path, "work"), (C.base_path, "base"), (C.diff_path, "changes.diff")):
            self.assertEqual(fn("sub_abcdef", "/r"), "/r/sub_abcdef/" + name)
            with self.assertRaises(C.ContractError):
                fn("../x", "/r")


if __name__ == "__main__":
    unittest.main()
