# tests/test_keys.py - headless end-to-end key tests for the real prompt bindings.
# Feeds fake keystrokes through a real PromptSession (pipe input, dummy output) using ui.bindings,
# so a dead Enter key is caught without a terminal.
import os, signal, sys, unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import paths  # noqa
import ui

from prompt_toolkit import PromptSession
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput


class _Timeout(Exception):
    pass


def _alarm(signum, frame):
    raise _Timeout("prompt did not return: key was not handled")


def feed(keys, timeout=5):
    """Send `keys` to a fresh prompt using the real bindings; return the submitted text."""
    old = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(timeout)
    try:
        with create_pipe_input() as inp:
            inp.send_text(keys)
            sess = PromptSession(input=inp, output=DummyOutput(),
                                 key_bindings=ui.bindings, multiline=False)
            return sess.prompt()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


class TestEnterSubmits(unittest.TestCase):
    ENTER_VARIANTS = {
        "classic CR": "\r",
        "LF (some terminals)": "\n",
        "kitty KKP plain": "\x1b[13u",
        "kitty KKP modifier=1": "\x1b[13;1u",
        "xterm CSI modifier=1": "\x1b[27;1;13~",
    }

    def test_every_plain_enter_variant_submits(self):
        for name, seq in self.ENTER_VARIANTS.items():
            with self.subTest(enter=name):
                self.assertEqual(feed("hello" + seq), "hello")

    def test_enter_on_empty_buffer_returns_empty(self):
        for name, seq in self.ENTER_VARIANTS.items():
            with self.subTest(enter=name):
                self.assertEqual(feed(seq), "")

    def test_enter_does_not_insert_newline(self):
        self.assertNotIn("\n", feed("one line\r"))


class TestAltEnterNewline(unittest.TestCase):
    def test_alt_enter_inserts_newline_then_enter_submits(self):
        self.assertEqual(feed("a\x1b\rb\r"), "a\nb")

    def test_two_alt_enters(self):
        self.assertEqual(feed("a\x1b\rb\x1b\rc\r"), "a\nb\nc")


class TestModifiedEnterNewline(unittest.TestCase):
    """Shift/Ctrl+Enter must insert a newline and must not swallow the next key."""
    SEQS = {
        "kitty shift+enter": "\x1b[13;2u",
        "kitty ctrl+enter": "\x1b[13;5u",
        "kitty ctrl+shift+enter": "\x1b[13;6u",
        "xterm shift+enter": "\x1b[27;2;13~",
        "xterm ctrl+enter": "\x1b[27;5;13~",
        "xterm ctrl+shift+enter": "\x1b[27;6;13~",
    }

    def test_modified_enter_inserts_newline_and_keeps_next_key(self):
        for name, seq in self.SEQS.items():
            with self.subTest(key=name):
                self.assertEqual(feed("a" + seq + "b\r"), "a\nb")


if __name__ == "__main__":
    unittest.main()
