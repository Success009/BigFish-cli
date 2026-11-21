"""Unit tests for editor.py — line-range, landmark, search-replace, and error paths."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import editor


def _write(path, text):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def _read(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


class TestLineRangeEdit(unittest.TestCase):
    """Edit by numeric line range."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(mode='w', delete=False,
                                               suffix='.py', encoding='utf-8')
        self.tmp.write("line 1\n  def foo():\n    return 42\nline 4\n")
        self.tmp.close()

    def tearDown(self):
        if os.path.exists(self.tmp.name):
            os.remove(self.tmp.name)

    def test_basic_line_range_edit(self):
        res = editor.edit_file(self.tmp.name, start_line=2, end_line=3,
                               replace_text="  def bar():\n    return 100\n")
        self.assertIn("Successfully edited", res)
        content = _read(self.tmp.name)
        self.assertIn("def bar():", content)
        self.assertNotIn("def foo():", content)

    def test_surrounding_lines_preserved(self):
        """Lines outside the edited range must be untouched."""
        editor.edit_file(self.tmp.name, start_line=2, end_line=3,
                         replace_text="  def bar():\n    return 100\n")
        content = _read(self.tmp.name)
        self.assertIn("line 1", content)
        self.assertIn("line 4", content)

    def test_single_line_replacement(self):
        res = editor.edit_file(self.tmp.name, start_line=4, end_line=4,
                               replace_text="line FOUR\n")
        self.assertIn("Successfully edited", res)
        content = _read(self.tmp.name)
        self.assertIn("line FOUR", content)
        self.assertNotIn("line 4", content)

    def test_edit_missing_file_returns_error(self):
        res = editor.edit_file("/nonexistent/path/file.py", start_line=1,
                               end_line=1, replace_text="x")
        self.assertIn("ERROR", res.upper())

    def test_edit_out_of_range_line_graceful(self):
        """start_line beyond EOF should not crash — return error or noop."""
        res = editor.edit_file(self.tmp.name, start_line=999, end_line=999,
                               replace_text="impossible\n")
        # Either an error message or unchanged file — just must not raise
        self.assertIsInstance(res, str)


class TestLandmarkEdit(unittest.TestCase):
    """Edit by start_string / end_string landmark."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(mode='w', delete=False,
                                               suffix='.py', encoding='utf-8')
        self.tmp.write("line 1\n  def foo():\n    return 42\nline 4\n")
        self.tmp.close()

    def tearDown(self):
        if os.path.exists(self.tmp.name):
            os.remove(self.tmp.name)

    def test_landmark_boundary_edit(self):
        res = editor.edit_file(self.tmp.name,
                               start_string="def foo()",
                               end_string="return 42",
                               replace_text="def foo():\n    return 99")
        self.assertIn("Successfully edited", res)
        self.assertIn("return 99", _read(self.tmp.name))

    def test_landmark_missing_start_returns_error(self):
        res = editor.edit_file(self.tmp.name,
                               start_string="DOES_NOT_EXIST",
                               end_string="return 42",
                               replace_text="noop")
        self.assertIn("ERROR", res.upper())

    def test_landmark_missing_end_returns_error(self):
        res = editor.edit_file(self.tmp.name,
                               start_string="def foo()",
                               end_string="DOES_NOT_EXIST",
                               replace_text="noop")
        self.assertIn("ERROR", res.upper())


class TestSearchReplaceEdit(unittest.TestCase):
    """Edit by exact search_text (unique occurrence)."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(mode='w', delete=False,
                                               suffix='.txt', encoding='utf-8')
        self.tmp.write("alpha\nbeta\ngamma\nalpha_duplicate\nalpha_duplicate\n")
        self.tmp.close()

    def tearDown(self):
        if os.path.exists(self.tmp.name):
            os.remove(self.tmp.name)

    def test_search_replace_unique(self):
        res = editor.edit_file(self.tmp.name, search_text="beta",
                               replace_text="BETA")
        self.assertIn("Successfully edited", res)
        self.assertIn("BETA", _read(self.tmp.name))
        self.assertNotIn("beta", _read(self.tmp.name))

    def test_search_not_found_returns_error(self):
        res = editor.edit_file(self.tmp.name, search_text="ZZZNOTHERE",
                               replace_text="nope")
        self.assertIn("ERROR", res.upper())

    def test_ambiguous_search_returns_error(self):
        """Duplicate occurrences of search_text should be an error, not silent."""
        res = editor.edit_file(self.tmp.name, search_text="alpha_duplicate",
                               replace_text="single")
        # Implementation should refuse to replace when ambiguous
        self.assertIn("ERROR", res.upper())

    def test_search_replace_preserves_other_content(self):
        editor.edit_file(self.tmp.name, search_text="gamma", replace_text="GAMMA")
        content = _read(self.tmp.name)
        self.assertIn("alpha", content)
        self.assertIn("beta", content)
        self.assertIn("GAMMA", content)


class TestMultipleEdits(unittest.TestCase):
    """Sequential edits on the same file must compose correctly."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(mode='w', delete=False,
                                               suffix='.txt', encoding='utf-8')
        self.tmp.write("A\nB\nC\nD\n")
        self.tmp.close()

    def tearDown(self):
        if os.path.exists(self.tmp.name):
            os.remove(self.tmp.name)

    def test_two_sequential_edits(self):
        editor.edit_file(self.tmp.name, search_text="A", replace_text="AAA")
        editor.edit_file(self.tmp.name, search_text="C", replace_text="CCC")
        content = _read(self.tmp.name)
        self.assertIn("AAA", content)
        self.assertIn("B", content)
        self.assertIn("CCC", content)
        self.assertIn("D", content)

    def test_empty_search_text_returns_clear_error(self):
        res = editor.edit_file(self.tmp.name, search_text="", replace_text="foo")
        self.assertIn("ERROR", res)
        self.assertIn("<search> parameter cannot be empty", res)

        res_spaces = editor.edit_file(self.tmp.name, search_text="   ", replace_text="foo")
        self.assertIn("ERROR", res_spaces)
        self.assertIn("<search> parameter cannot be empty", res_spaces)


if __name__ == "__main__":
    unittest.main()
