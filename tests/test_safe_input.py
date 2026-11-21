# tests/test_safe_input.py - the plain input() fallback must work when the key layer is broken
import os, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import paths  # noqa
import ui


class TestSafeInput(unittest.TestCase):
    def test_env_var_enables_safe_mode(self):
        with mock.patch.dict(os.environ, {"BIGFISH_SAFE_INPUT": "1"}):
            self.assertTrue(ui._safe_input())

    def test_flag_enables_safe_mode(self):
        with mock.patch.object(sys, "argv", ["main.py", "--safe"]):
            self.assertTrue(ui._safe_input())

    def test_off_by_default(self):
        env = {k: v for k, v in os.environ.items() if k != "BIGFISH_SAFE_INPUT"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "argv", ["main.py"]):
            self.assertFalse(ui._safe_input())

    def test_safe_mode_uses_plain_input(self):
        with mock.patch.dict(os.environ, {"BIGFISH_SAFE_INPUT": "1"}), \
             mock.patch("builtins.input", return_value="hello") as m, \
             mock.patch.object(ui.session, "prompt") as sp:
            self.assertEqual(ui.show_prompt(10, provider="X"), "hello")
            m.assert_called_once()
            sp.assert_not_called()

    def test_safe_mode_eof_exits(self):
        with mock.patch.dict(os.environ, {"BIGFISH_SAFE_INPUT": "1"}), \
             mock.patch("builtins.input", side_effect=EOFError):
            self.assertEqual(ui.show_prompt(), "/exit")


if __name__ == "__main__":
    unittest.main()
