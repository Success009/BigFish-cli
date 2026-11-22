# Page-scrape turns get their own detailed log and a marker in the status line
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import paths  # noqa
import debuglog  # noqa
debuglog._LOG_PATH = os.path.join(tempfile.mkdtemp(), "test_debug.log")
from browser import WebAgentController  # noqa
import ui  # noqa


class _Adapter:
    name = "gemini"


class _Page:
    url = "https://example.test/chat"


class TestUiFallbackLog(unittest.TestCase):
    def test_record_is_written_with_key_fields(self):
        c = WebAgentController.__new__(WebAgentController)
        c.adapter, c.page = _Adapter(), _Page()
        c._last_ui_reason = "no_payload"
        c._profile_tag = lambda: "Profile 99"
        c.UI_FALLBACK_LOG = os.path.join(tempfile.mkdtemp(), "ui_fallback.log")
        c._log_ui_fallback("[SYS_MARKER_1]", "hello world")
        data = open(c.UI_FALLBACK_LOG, encoding="utf-8").read()
        for needle in ("UI FALLBACK", "profile=Profile 99", "reason=no_payload", "ui_text_len=11", "provider=gemini"):
            self.assertIn(needle, data)

    def test_status_line_marker_only_for_page_source(self):
        ui._prompt_info.update({"tokens": 5, "provider": "Gemini", "source": "ui"})
        self.assertIn("Gemini - page", ui._prompt_message()[1][1])
        ui._prompt_info["source"] = "network"
        self.assertNotIn("page", ui._prompt_message()[1][1])


if __name__ == "__main__":
    unittest.main()
