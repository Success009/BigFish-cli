# Chrome is never raised by default, and the Gemini fallback clears old text before pasting
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import paths  # noqa
from browser import WebAgentController  # noqa
from adapters import gemini  # noqa


class _Page:
    called = 0

    def is_closed(self):
        return False

    def bring_to_front(self):
        _Page.called += 1


class TestNoFocusSteal(unittest.TestCase):
    def test_bring_to_front_is_off_by_default(self):
        c = WebAgentController.__new__(WebAgentController)
        c.page = _Page()
        os.environ.pop("BF_RAISE_BROWSER", None)
        _Page.called = 0
        c.bring_to_front()
        self.assertEqual(_Page.called, 0)

    def test_bring_to_front_can_be_enabled(self):
        c = WebAgentController.__new__(WebAgentController)
        c.page = _Page()
        os.environ["BF_RAISE_BROWSER"] = "1"
        try:
            _Page.called = 0
            c.bring_to_front()
            self.assertEqual(_Page.called, 1)
        finally:
            os.environ.pop("BF_RAISE_BROWSER", None)

    def test_gemini_fallback_clears_textbox_first(self):
        src = open(gemini.__file__, encoding="utf-8").read()
        self.assertIn('chat_box.fill("")', src)
        self.assertLess(src.index('chat_box.fill("")'), src.index("Copy prompt text to clipboard"))


if __name__ == "__main__":
    unittest.main()
