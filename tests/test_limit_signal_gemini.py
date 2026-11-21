# tests/test_limit_signal_gemini.py
# Limit-signal detection in WebAgentController.monitor_active_response, fed with AI Studio-STYLE bodies.
# IMPORTANT: no real AI Studio limit response has ever been captured. These bodies follow Google's
# API error shape (HTTP 429, status RESOURCE_EXHAUSTED, quota wording) and are ASSUMED shapes.
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import browser


class FakeAdapter:
    display_name = "Fake"
    poll_interval = 0.01

    def __init__(self, name="gemini", busy_sequence=None):
        self.name = name
        self.busy_sequence = list(busy_sequence or [])

    def is_generating(self, page):
        if self.busy_sequence:
            return self.busy_sequence.pop(0)
        return False

    def check_page_errors(self, page):
        return None

    def trigger_submit(self, page):
        return True

    def parse_stream_payload(self, raw):
        return "REPLY_TEXT"

    def clean_payload(self, text):
        return text

    def is_pure_thought(self, text):
        return False

    def extract_ui_response(self, page, marker):
        return "STALE_PREVIOUS_REPLY"


def make_controller(adapter, live_payloads=None, rate_status=None, auto=True):
    c = browser.WebAgentController.__new__(browser.WebAgentController)
    c.adapter = adapter
    c.page = mock.MagicMock()
    c.connect = lambda *a, **k: True
    c.scroll_chat_to_bottom = lambda: None
    c.live_payloads = list(live_payloads or [])
    c.captured_responses = []
    c.stream_finished = True
    c.cdp_captured = False
    c.rate_status = rate_status
    c.auto_transfer_on_rate_limit = auto
    c._tl = lambda marker, **kw: None
    return c


def run(controller):
    clock = {"t": 1000.0}

    def fake_time():
        clock["t"] += 0.5
        return clock["t"]

    with mock.patch("browser.time.sleep", lambda s: None), \
         mock.patch("browser.time.time", fake_time), \
         mock.patch("debuglog.log", lambda *a, **k: None):
        return controller.monitor_active_response("[M]")


# ASSUMED AI Studio-style shapes (not real captures).
QUOTA_BODY = ('{"error":{"code":429,"message":"You exceeded your current quota, please check your plan '
              'and billing details.","status":"RESOURCE_EXHAUSTED"}}')
STATUS_ONLY_BODY = '{"error":{"code":429,"status":"RESOURCE_EXHAUSTED"}}'   # no wording the pattern knows
NOT_LIMIT_BODY = '{"error":{"code":500,"message":"Internal error","status":"INTERNAL"}}'
LONG_QUOTA_TALK = ("Here is how a quota works for an API. " * 500)[:15000]


class TestGeminiLimitSignal(unittest.TestCase):
    def detect(self, payloads, status, **kw):
        return run(make_controller(FakeAdapter(**kw.pop("adapter_kw", {})), payloads, status, **kw))

    def test_429_with_quota_body_detected(self):
        self.assertEqual(self.detect([QUOTA_BODY], 429), "RATE_LIMIT_DETECTED")

    def test_429_with_status_only_body_detected(self):
        self.assertEqual(self.detect([STATUS_ONLY_BODY], 429), "RATE_LIMIT_DETECTED")

    def test_429_with_unrelated_short_body_detected(self):
        self.assertEqual(self.detect([NOT_LIMIT_BODY], 429), "RATE_LIMIT_DETECTED")

    def test_no_status_short_quota_body_detected(self):
        self.assertEqual(self.detect([QUOTA_BODY], None), "RATE_LIMIT_DETECTED")

    def test_500_with_quota_body_detected(self):
        self.assertEqual(self.detect([QUOTA_BODY], 500), "RATE_LIMIT_DETECTED")

    def test_500_with_unrelated_body_not_detected(self):
        self.assertNotEqual(self.detect([NOT_LIMIT_BODY], 500), "RATE_LIMIT_DETECTED")

    def test_status_only_body_without_http_status_is_not_detected(self):
        # Documents a gap: RESOURCE_EXHAUSTED alone is not in the wording pattern.
        self.assertNotEqual(self.detect([STATUS_ONLY_BODY], None), "RATE_LIMIT_DETECTED")

    def test_long_reply_about_quotas_not_detected(self):
        self.assertNotEqual(self.detect([LONG_QUOTA_TALK], None), "RATE_LIMIT_DETECTED")

    def test_deepseek_with_429_not_detected(self):
        r = self.detect([QUOTA_BODY], 429, adapter_kw={"name": "deepseek"})
        self.assertNotEqual(r, "RATE_LIMIT_DETECTED")

    def test_flag_off_with_429_not_detected(self):
        self.assertNotEqual(self.detect([QUOTA_BODY], 429, auto=False), "RATE_LIMIT_DETECTED")

    def test_busy_then_idle_detected_after_idle(self):
        r = self.detect([QUOTA_BODY], 429, adapter_kw={"busy_sequence": [True, True, False]})
        self.assertEqual(r, "RATE_LIMIT_DETECTED")


if __name__ == "__main__":
    unittest.main()
