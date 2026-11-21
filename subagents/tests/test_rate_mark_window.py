import json
import os
import tempfile
import time
import unittest

from subagents.factory import SubFactory


class KwChannel:
    def __init__(self, *a, **kw):
        self.kw = kw

    def close(self):
        pass


H = 3600


class TestMarkWindow(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.ledger = os.path.join(self.dir, "ledger.json")
        self.f = SubFactory(self.dir, profiles=lambda: ["Profile 7"], src_dir="/src",
                            channel_cls=KwChannel, ledger_file=self.ledger)

    def until(self, provider):
        before = time.time()
        self.f._on_rate_limit("Profile 7", provider)
        with open(self.ledger, encoding="utf-8") as fh:
            data = json.load(fh)
        entry = data["providers"][provider]["Profile 7"]
        return entry["rate_limited_until"] - before

    def test_gemini_mark_lasts_24_hours(self):
        d = self.until("gemini")
        self.assertGreater(d, 24 * H - 60)
        self.assertLess(d, 24 * H + 60)

    def test_claude_mark_lasts_5_hours(self):
        d = self.until("claude")
        self.assertGreater(d, 5 * H - 60)
        self.assertLess(d, 5 * H + 60)

    def test_provider_without_window_keeps_default(self):
        d = self.until("deepseek")
        self.assertGreater(d, 5 * H - 60)
        self.assertLess(d, 5 * H + 60)


if __name__ == "__main__":
    unittest.main()
