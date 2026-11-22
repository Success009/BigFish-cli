# The stream listener must follow the adapter AND the page object
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import paths  # noqa
import tempfile  # noqa
import debuglog  # noqa
debuglog._LOG_PATH = os.path.join(tempfile.mkdtemp(), "test_debug.log")   # keep tests out of the live log
from browser import WebAgentController  # noqa


class _Adapter:
    def __init__(self, name):
        self.name = name
        self.calls = 0

    def register_stream_listener(self, page, controller):
        self.calls += 1


class _Page:
    url = "https://example.test/chat"


def _controller(adapter, page):
    c = WebAgentController.__new__(WebAgentController)
    c.adapter = adapter
    c.page = page
    return c


class TestListenerRegistration(unittest.TestCase):
    def test_same_page_registers_once(self):
        a, p = _Adapter("deepseek"), _Page()
        c = _controller(a, p)
        c.register_network_listener()
        c.register_network_listener()
        self.assertEqual(a.calls, 1)

    def test_new_page_registers_again(self):
        a = _Adapter("deepseek")
        c = _controller(a, _Page())
        c.register_network_listener()
        c.page = _Page()
        c.register_network_listener()
        self.assertEqual(a.calls, 2)

    def test_adapter_switch_and_back_registers_again(self):
        p = _Page()
        ds, gemini = _Adapter("deepseek"), _Adapter("gemini")
        c = _controller(ds, p)
        c.register_network_listener()
        c.adapter = gemini
        c.register_network_listener()
        c.adapter = ds
        c.register_network_listener()
        self.assertEqual((ds.calls, gemini.calls), (2, 1))


if __name__ == "__main__":
    unittest.main()
