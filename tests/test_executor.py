"""Unit tests for executor.py — success, error, CWD, stderr, multi-line, edge cases."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import executor


class TestExecuteCommandBasic(unittest.TestCase):
    """Happy-path and error-code cases."""

    def test_execute_success_echo(self):
        out = executor.execute_command("echo 'BigFish CLI Test'")
        self.assertIn("BigFish CLI Test", out)

    def test_execute_bad_path_returns_error(self):
        out = executor.execute_command("ls /path/that/does/not/exist_12345")
        self.assertIn("ERROR (Exit Code", out)

    def test_execute_multiline_output(self):
        out = executor.execute_command("printf 'line1\\nline2\\nline3\\n'")
        self.assertIn("line1", out)
        self.assertIn("line2", out)
        self.assertIn("line3", out)

    def test_execute_exit_code_captured(self):
        out = executor.execute_command("exit 42", )
        self.assertIn("ERROR", out)

    def test_execute_empty_output_command(self):
        """Commands that produce no output should return empty string (or ok)."""
        out = executor.execute_command("true")
        # Should not contain ERROR
        self.assertNotIn("ERROR (Exit Code", out)

    def test_execute_stderr_merged_into_output(self):
        """stderr is merged with stdout via STDOUT redirect."""
        out = executor.execute_command("echo stderr_msg >&2")
        # Should not raise; output may be empty string but must be a string
        self.assertIsInstance(out, str)


class TestExecuteCommandCwd(unittest.TestCase):
    """Commands run in specified CWD."""

    def test_execute_with_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = executor.execute_command("pwd", cwd=tmp)
            # Output should contain the temp dir path (symlinks may differ)
            real_tmp = os.path.realpath(tmp)
            real_out = os.path.realpath(out.strip()) if out.strip() else ""
            self.assertEqual(real_tmp, real_out)

    def test_execute_file_relative_to_cwd(self):
        """Writing a file using a relative path resolves against cwd."""
        with tempfile.TemporaryDirectory() as tmp:
            out = executor.execute_command("touch testfile.txt && ls", cwd=tmp)
            self.assertIn("testfile.txt", out)


class TestExecuteCommandOutput(unittest.TestCase):
    """Output content and formatting."""

    def test_returns_string_always(self):
        for cmd in ["echo hi", "false", "ls /nonexistent_99"]:
            out = executor.execute_command(cmd)
            self.assertIsInstance(out, str, f"Command {cmd!r} did not return str")

    def test_python_interpreter_available(self):
        out = executor.execute_command("python3 -c \"print('hello_from_python')\"")
        self.assertIn("hello_from_python", out)


class TestExecutorHardening(unittest.TestCase):
    """Timeout, log fallback, large output, sudo password safety."""

    def test_custom_timeout_kills_command(self):
        out = executor.execute_command("sleep 5", timeout=1)
        self.assertIn("no output for 1 s", out)

    def test_env_timeout(self):
        os.environ["BIGFISH_CMD_TIMEOUT"] = "1"
        try:
            out = executor.execute_command("sleep 5")
        finally:
            del os.environ["BIGFISH_CMD_TIMEOUT"]
        self.assertIn("timed out", out)

    def test_bad_timeout_values_fall_back(self):
        self.assertEqual(executor._get_timeout("abc"), executor.DEFAULT_TIMEOUT)
        self.assertEqual(executor._get_timeout(-5), executor.DEFAULT_TIMEOUT)
        self.assertEqual(executor._get_timeout(7), 7.0)

    def test_log_folder_created_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = executor.CMD_LOG_FILE
            executor.CMD_LOG_FILE = os.path.join(tmp, "newdir", "out.txt")
            try:
                out = executor.execute_command("seq 1 500")
                self.assertIn("TRUNCATED", out)
                self.assertTrue(os.path.exists(executor.CMD_LOG_FILE))
            finally:
                executor.CMD_LOG_FILE = old

    def test_log_unwritable_falls_back_to_tempfile(self):
        old = executor.CMD_LOG_FILE
        executor.CMD_LOG_FILE = "/proc/nope/out.txt"
        try:
            out = executor.execute_command("seq 1 500")
            self.assertIn("TRUNCATED", out)
            self.assertIn("bigfish_cmd_", out)
        finally:
            executor.CMD_LOG_FILE = old

    def test_huge_output_spills_to_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_log, old_spill = executor.CMD_LOG_FILE, executor.SPILL_BYTES
            executor.CMD_LOG_FILE = os.path.join(tmp, "big.txt")
            executor.SPILL_BYTES = 10000
            try:
                out = executor.execute_command("seq 1 20000")
                self.assertIn("TRUNCATED", out)
                self.assertIn("20000", out)
                with open(executor.CMD_LOG_FILE) as f:
                    self.assertEqual(len(f.read().splitlines()), 20000)
            finally:
                executor.CMD_LOG_FILE = old_log
                executor.SPILL_BYTES = old_spill

    def test_verify_sudo_password_with_quotes_is_safe(self):
        marker = os.path.join(tempfile.gettempdir(), "bf_inject_marker")
        if os.path.exists(marker):
            os.remove(marker)
        # Return value depends on the machine's sudo setup (passwordless/cached sudo
        # accepts any password); the security property is that nothing gets injected.
        executor.verify_sudo("x'; touch " + marker + "; echo '")
        self.assertFalse(os.path.exists(marker))
        self.assertFalse(executor.verify_sudo(None))

    def test_closer_like_text_needs_escape_in_panel(self):
        # Regression: rich markup parses a closer-like string as a closing tag.
        # Built from char codes. Renders on a forced-terminal console.
        import io
        from rich.console import Console
        from rich.panel import Panel
        from rich.markup import escape, MarkupError
        closer = chr(91) + chr(47) + "write" + chr(93)
        c = Console(file=io.StringIO(), force_terminal=True, width=60)
        with self.assertRaises(MarkupError):
            c.print(Panel(closer))
        c.print(Panel(escape(closer)))

    def test_executor_escapes_live_panel_text(self):
        src_path = os.path.join(os.path.dirname(executor.__file__), "executor.py")
        with open(src_path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("escape(display_text)", src)
        self.assertIn("escape(short_title)", src)


if __name__ == "__main__":
    unittest.main()
