import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import editor


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _read(path):
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


class TestLandmarkAnchor(unittest.TestCase):
    def test_end_anchor_searched_after_start_anchor(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.txt")
            _write(p, "alpha beta\nmiddle\nbeta end\n")
            r = editor.edit_file(p, start_string="alpha beta", end_string="beta", replace_text="X")
            self.assertNotIn("ERROR", r, r)
            self.assertEqual(_read(p), "X end\n")


class TestLineRangeValidation(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.p = os.path.join(self.d.name, "a.txt")
        _write(self.p, "one\ntwo\nthree\n")

    def tearDown(self):
        self.d.cleanup()

    def test_start_greater_than_end_rejected(self):
        r = editor.edit_file(self.p, start_line=3, end_line=1, replace_text="x")
        self.assertTrue(r.startswith("ERROR"), r)
        self.assertEqual(_read(self.p), "one\ntwo\nthree\n")

    def test_start_below_one_rejected(self):
        r = editor.edit_file(self.p, start_line=0, end_line=1, replace_text="x")
        self.assertTrue(r.startswith("ERROR"), r)
        self.assertEqual(_read(self.p), "one\ntwo\nthree\n")

    def test_non_integer_rejected(self):
        r = editor.edit_file(self.p, start_line="a", end_line="b", replace_text="x")
        self.assertTrue(r.startswith("ERROR"), r)

    def test_valid_range_works(self):
        r = editor.edit_file(self.p, start_line=2, end_line=2, replace_text="TWO")
        self.assertNotIn("ERROR", r, r)
        self.assertEqual(_read(self.p), "one\nTWO\nthree\n")


class TestNoOpEdit(unittest.TestCase):
    def test_identical_replacement_reports_no_change(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.txt")
            _write(p, "hello world\n")
            r = editor.edit_file(p, search_text="hello", replace_text="hello")
            self.assertIn("No changes made", r)
            self.assertNotIn("Successfully", r)


class TestAtomicEdit(unittest.TestCase):
    def test_permissions_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "run.sh")
            _write(p, "echo old\n")
            os.chmod(p, 0o755)
            r = editor.edit_file(p, search_text="old", replace_text="new")
            self.assertNotIn("ERROR", r, r)
            self.assertEqual(_read(p), "echo new\n")
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o755)

    def test_symlink_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "real.txt")
            link = os.path.join(d, "link.txt")
            _write(real, "old value\n")
            os.symlink(real, link)
            r = editor.edit_file(link, search_text="old", replace_text="new")
            self.assertNotIn("ERROR", r, r)
            self.assertTrue(os.path.islink(link))
            self.assertEqual(_read(real), "new value\n")

    def test_no_temp_files_left_behind(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.txt")
            _write(p, "abc\n")
            editor.edit_file(p, search_text="abc", replace_text="xyz")
            self.assertEqual(os.listdir(d), ["a.txt"])

    def test_crlf_kept(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.txt")
            _write(p, "one\r\ntwo\r\n")
            r = editor.edit_file(p, search_text="two", replace_text="TWO")
            self.assertNotIn("ERROR", r, r)
            self.assertEqual(_read(p), "one\r\nTWO\r\n")


class TestNonUtf8Refused(unittest.TestCase):
    def test_binary_file_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bin.dat")
            data = b"\xff\xfe\x00abc\x80"
            with open(p, "wb") as f:
                f.write(data)
            r = editor.edit_file(p, search_text="abc", replace_text="xyz")
            self.assertIn("not valid UTF-8", r)
            with open(p, "rb") as f:
                self.assertEqual(f.read(), data)


if __name__ == "__main__":
    unittest.main()
