# tests/test_tools.py - Comprehensive unit tests for tools.py
import os
import sys
import stat
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tools

class TestFileReadWrite(unittest.TestCase):
    """File read/write operations."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _path(self, name):
        return os.path.join(self.tmp.name, name)

    def test_write_creates_file(self):
        p = self._path("new.txt")
        res = tools.write_file(p, "hello")
        self.assertIn("Successfully wrote", res)
        self.assertTrue(os.path.exists(p))

    def test_write_and_read_roundtrip(self):
        p = self._path("rt.txt")
        tools.write_file(p, "alpha\nbeta\ngamma")
        out = tools.read_file(p)
        self.assertIn("alpha", out)
        self.assertIn("   1 | alpha", out)
        self.assertIn("   3 | gamma", out)

    def test_read_line_range(self):
        p = self._path("lr.txt")
        tools.write_file(p, "\n".join([f"line{i}" for i in range(1, 21)]))
        out = tools.read_file(p, start_line=5, end_line=10)
        self.assertIn("line5", out)
        self.assertIn("line10", out)
        self.assertNotIn("line1\n", out)
        self.assertNotIn("line11", out)

    def test_read_search_term_context(self):
        p = self._path("st.txt")
        content = "\n".join([f"line{i}" for i in range(1, 50)])
        tools.write_file(p, content)
        out = tools.read_file(p, search_term="line25")
        self.assertIn("line25", out)

    def test_read_all_bypasses_truncation(self):
        p = self._path("big.txt")
        tools.write_file(p, "\n".join([f"L{i}" for i in range(1, 600)]))
        out = tools.read_file(p, read_all=True)
        self.assertIn("L599", out)

    def test_write_creates_parent_dirs(self):
        p = self._path("deep/nested/file.txt")
        res = tools.write_file(p, "nested content")
        self.assertIn("Successfully wrote", res)
        self.assertTrue(os.path.exists(p))

    def test_read_missing_file_returns_error(self):
        out = tools.read_file(self._path("ghost.txt"))
        self.assertIn("ERROR", out)

    def test_read_search_term_not_found(self):
        p = self._path("ns.txt")
        tools.write_file(p, "apple orange banana")
        out = tools.read_file(p, search_term="mango")
        self.assertIn("ERROR", out)
        self.assertIn("not found", out)


class TestDirectoryOps(unittest.TestCase):
    """Directory creation, listing, deletion."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _path(self, name):
        return os.path.join(self.tmp.name, name)

    def test_create_directory(self):
        d = self._path("newdir")
        res = tools.create_directory(d)
        self.assertIn("Successfully created directory", res)
        self.assertTrue(os.path.isdir(d))

    def test_create_nested_directories(self):
        d = self._path("a/b/c")
        res = tools.create_directory(d)
        self.assertIn("Successfully created", res)
        self.assertTrue(os.path.isdir(d))

    def test_list_directory_shows_files_and_dirs(self):
        tools.write_file(self._path("f1.txt"), "abc")
        os.makedirs(self._path("subdir"))
        out = tools.list_directory(self.tmp.name)
        self.assertIn("[FILE] f1.txt", out)
        self.assertIn("[DIR]  subdir/", out)

    def test_list_directory_line_counts(self):
        tools.write_file(self._path("counted.py"), "a\nb\nc\n")
        out = tools.list_directory(self.tmp.name)
        self.assertIn("counted.py", out)
        self.assertIn("3 lines", out)

    def test_list_empty_directory(self):
        d = self._path("emptydir")
        os.makedirs(d)
        out = tools.list_directory(d)
        self.assertIn("empty", out)

    def test_list_missing_directory(self):
        out = tools.list_directory(self._path("ghost"))
        self.assertIn("ERROR", out)

    def test_delete_file(self):
        p = self._path("del.txt")
        tools.write_file(p, "bye")
        res = tools.delete_item(p)
        self.assertIn("Successfully deleted file", res)
        self.assertFalse(os.path.exists(p))

    def test_delete_directory_tree(self):
        d = self._path("tree")
        os.makedirs(d)
        tools.write_file(os.path.join(d, "inner.txt"), "x")
        res = tools.delete_item(d)
        self.assertIn("Successfully deleted directory", res)
        self.assertFalse(os.path.exists(d))

    def test_delete_missing_item_returns_error(self):
        res = tools.delete_item(self._path("ghost"))
        self.assertIn("ERROR", res)

    def test_rename_file(self):
        src = self._path("src.txt")
        dst = self._path("dst.txt")
        tools.write_file(src, "data")
        res = tools.rename_item(src, dst)
        self.assertIn("Successfully renamed", res)
        self.assertFalse(os.path.exists(src))
        self.assertTrue(os.path.exists(dst))

    def test_rename_missing_file_returns_error(self):
        res = tools.rename_item(self._path("ghost.txt"), self._path("out.txt"))
        self.assertIn("ERROR", res)


class TestGrepSearch(unittest.TestCase):
    """Grep/text search functionality."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _path(self, name):
        return os.path.join(self.tmp.name, name)

    def test_grep_finds_match(self):
        tools.write_file(self._path("code.py"), "def my_unique_function():\n    pass\n")
        out = tools.grep_search(self.tmp.name, "my_unique_function")
        self.assertIn("my_unique_function", out)
        self.assertIn("code.py", out)

    def test_grep_no_match_message(self):
        tools.write_file(self._path("empty.py"), "nothing_special_here")
        out = tools.grep_search(self.tmp.name, "xyz_no_match_token_abc")
        self.assertIn("No matches found", out)

    def test_grep_includes_line_numbers(self):
        tools.write_file(self._path("nums.py"), "line one\nSEARCH_TOKEN\nline three\n")
        out = tools.grep_search(self.tmp.name, "SEARCH_TOKEN")
        self.assertIn(":2:", out)  # line number 2

    def test_grep_searches_subdirectories(self):
        sub = self._path("sub")
        os.makedirs(sub)
        tools.write_file(os.path.join(sub, "deep.txt"), "DEEP_TOKEN found here")
        out = tools.grep_search(self.tmp.name, "DEEP_TOKEN")
        self.assertIn("DEEP_TOKEN", out)

    def test_grep_excludes_venv_dir(self):
        venv_dir = self._path("venv")
        os.makedirs(venv_dir)
        tools.write_file(os.path.join(venv_dir, "lib.py"), "VENV_TOKEN")
        out = tools.grep_search(self.tmp.name, "VENV_TOKEN")
        # venv is excluded, should not find it
        self.assertIn("No matches found", out)


class TestScratchpad(unittest.TestCase):
    """Scratchpad write/append."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = tools.SCRATCHPAD_FILE
        tools.SCRATCHPAD_FILE = os.path.join(self.tmp.name, "scratch.txt")

    def tearDown(self):
        tools.SCRATCHPAD_FILE = self._orig
        self.tmp.cleanup()

    def test_append_mode(self):
        tools.write_to_scratchpad("line A", mode='a')
        tools.write_to_scratchpad("line B", mode='a')
        with open(tools.SCRATCHPAD_FILE) as f:
            content = f.read()
        self.assertIn("line A", content)
        self.assertIn("line B", content)

    def test_overwrite_mode(self):
        tools.write_to_scratchpad("original", mode='w')
        tools.write_to_scratchpad("replacement", mode='w')
        with open(tools.SCRATCHPAD_FILE) as f:
            content = f.read()
        self.assertNotIn("original", content)
        self.assertIn("replacement", content)


class TestSkillLoader(unittest.TestCase):
    """Dynamic skill loading system."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_dir = tools.SKILLS_DIR
        tools.SKILLS_DIR = self.tmp.name
        # Create test skills
        with open(os.path.join(self.tmp.name, "python.md"), "w") as f:
            f.write("# Python Skill\nUse type hints. Write docstrings.")
        with open(os.path.join(self.tmp.name, "docker.md"), "w") as f:
            f.write("# Docker Skill\nAlways pin base image versions.")

    def tearDown(self):
        tools.SKILLS_DIR = self._orig_dir
        self.tmp.cleanup()

    def test_load_existing_skill(self):
        out = tools.load_skill("python")
        self.assertIn("SKILL LOADED: python", out)
        self.assertIn("Use type hints", out)
        self.assertIn("END SKILL", out)

    def test_load_skill_case_insensitive(self):
        out = tools.load_skill("PYTHON")
        self.assertIn("SKILL LOADED", out)

    def test_load_skill_fuzzy_match(self):
        out = tools.load_skill("dock")  # partial match -> docker
        self.assertIn("SKILL LOADED: docker", out)
        self.assertIn("pin base image", out)

    def test_list_skills(self):
        out = tools.load_skill("list")
        self.assertIn("python", out)
        self.assertIn("docker", out)

    def test_load_nonexistent_skill_returns_error(self):
        out = tools.load_skill("kubernetes_nonexistent_xyz")
        self.assertIn("ERROR", out)
        self.assertIn("not found", out)
        self.assertIn("Available", out)

    def test_load_skill_empty_dir(self):
        empty_tmp = tempfile.TemporaryDirectory()
        tools.SKILLS_DIR = empty_tmp.name
        out = tools.load_skill("list")
        self.assertIn("No skills available", out)
        tools.SKILLS_DIR = self.tmp.name
        empty_tmp.cleanup()


class TestWebSearch(unittest.TestCase):
    """Web search (live network, marked as integration)."""
    def test_web_search_returns_string(self):
        res = tools.web_search("python", num_results=1)
        self.assertIsInstance(res, str)
        self.assertGreater(len(res), 5)

    def test_set_browser_controller_stores_ref(self):
        # Verify set_browser_controller is callable and stores value
        tools.set_browser_controller(None)
        self.assertIsNone(tools._cdp_controller)
        mock = object()
        tools.set_browser_controller(mock)
        self.assertIs(tools._cdp_controller, mock)
        tools.set_browser_controller(None)  # cleanup


class TestToolsHardening(unittest.TestCase):
    """Skill-name matching and delete safety guard."""

    def test_load_skill_name_ending_in_m_or_d(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            old = tools.SKILLS_DIR
            tools.SKILLS_DIR = tmp
            try:
                with open(os.path.join(tmp, "random.md"), "w") as f:
                    f.write("hello skill")
                self.assertIn("SKILL LOADED: random", tools.load_skill("random"))
                self.assertIn("SKILL LOADED: random", tools.load_skill("random.md"))
            finally:
                tools.SKILLS_DIR = old

    def test_delete_refuses_root_home_and_install_dir(self):
        home = os.path.expanduser("~")
        install = os.path.dirname(os.path.abspath(tools.__file__))
        for target in ("/", home, os.path.dirname(home), install):
            out = tools.delete_item(target)
            self.assertIn("ERROR", out, target)
            self.assertTrue(os.path.exists(target), target)

    def test_delete_normal_file_still_works(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            f = os.path.join(tmp, "x.txt")
            open(f, "w").close()
            self.assertIn("Successfully deleted", tools.delete_item(f))
            self.assertFalse(os.path.exists(f))


class TestAtomicWrite(unittest.TestCase):
    """write_file uses temp file + rename; must keep perms, symlinks, and leave no debris."""

    def test_overwrite_and_new_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = os.path.join(tmp, "a.txt")
            self.assertIn("Successfully wrote", tools.write_file(f, "one"))
            self.assertIn("Successfully wrote", tools.write_file(f, "two"))
            with open(f) as fh:
                self.assertEqual(fh.read(), "two")

    def test_new_file_in_new_subdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = os.path.join(tmp, "sub", "deep", "b.txt")
            self.assertIn("Successfully wrote", tools.write_file(f, "x"))
            self.assertTrue(os.path.exists(f))

    def test_permissions_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = os.path.join(tmp, "run.sh")
            with open(f, "w") as fh:
                fh.write("old")
            os.chmod(f, 0o755)
            tools.write_file(f, "new")
            self.assertEqual(os.stat(f).st_mode & 0o777, 0o755)

    def test_symlink_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real.txt")
            link = os.path.join(tmp, "link.txt")
            with open(real, "w") as fh:
                fh.write("old")
            os.symlink(real, link)
            tools.write_file(link, "new")
            self.assertTrue(os.path.islink(link))
            with open(real) as fh:
                self.assertEqual(fh.read(), "new")

    def test_no_temp_files_left(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools.write_file(os.path.join(tmp, "c.txt"), "data")
            self.assertEqual(os.listdir(tmp), ["c.txt"])

    def test_crlf_and_unicode_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = os.path.join(tmp, "d.txt")
            body = "line1\r\nline2 \u00e9\u4e2d\r\n"
            tools.write_file(f, body)
            with open(f, "r", encoding="utf-8", newline="") as fh:
                self.assertEqual(fh.read(), body)


if __name__ == "__main__":
    unittest.main()
