
# tests/test_ratelimit_gate.py - tests for WebAgentController._confirm_rate_limit
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import browser


class FakeAdapter:
    def __init__(self, name="claude", states=None):
        self.name = name
        self.states = list(states or [])
        self.calls = 0

    def check_page_errors(self, page):
        self.calls += 1
        if self.states:
            if len(self.states) > 1:
                return self.states.pop(0)
            return self.states[0]
        return None


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, secs):
        self.now += secs


def make_controller(adapter, flag=True, status=None, generating=False):
    c = browser.WebAgentController.__new__(browser.WebAgentController)
    c.adapter = adapter
    c.page = object()
    c.auto_transfer_on_rate_limit = flag
    c.rate_status = status
    c.is_page_generating = lambda: generating
    return c


class TestConfirmRateLimit(unittest.TestCase):
    def run_gate(self, controller):
        clock = FakeClock()
        with mock.patch.object(browser.time, "time", clock.time), \
                mock.patch.object(browser.time, "sleep", clock.sleep):
            result = controller._confirm_rate_limit()
        return result, clock

    def test_flag_off_never_confirms(self):
        ad = FakeAdapter(states=["RATE_LIMIT"])
        result, _ = self.run_gate(make_controller(ad, flag=False))
        self.assertFalse(result)
        self.assertEqual(ad.calls, 0)

    def test_deepseek_never_confirms(self):
        ad = FakeAdapter(name="deepseek", states=["RATE_LIMIT"])
        result, _ = self.run_gate(make_controller(ad))
        self.assertFalse(result)

    def test_no_notice_not_confirmed(self):
        ad = FakeAdapter(states=[None])
        result, _ = self.run_gate(make_controller(ad))
        self.assertFalse(result)

    def test_page_generating_not_confirmed(self):
        ad = FakeAdapter(states=["RATE_LIMIT"])
        result, _ = self.run_gate(make_controller(ad, generating=True))
        self.assertFalse(result)

    def test_notice_vanishes_not_confirmed(self):
        ad = FakeAdapter(states=["RATE_LIMIT", "RATE_LIMIT", "RATE_LIMIT", None])
        result, _ = self.run_gate(make_controller(ad))
        self.assertFalse(result)

    def test_persistent_notice_confirmed_after_4s(self):
        ad = FakeAdapter(states=["RATE_LIMIT"])
        result, clock = self.run_gate(make_controller(ad))
        self.assertTrue(result)
        self.assertGreaterEqual(clock.now - 1000.0, 4.0)
        self.assertLess(clock.now - 1000.0, 8.0)

    def test_http_429_shortens_wait(self):
        ad = FakeAdapter(states=["RATE_LIMIT"])
        result, clock = self.run_gate(make_controller(ad, status=429))
        self.assertTrue(result)
        self.assertGreaterEqual(clock.now - 1000.0, 1.0)
        self.assertLess(clock.now - 1000.0, 4.0)

    def test_check_exception_not_confirmed(self):
        ad = FakeAdapter(states=["RATE_LIMIT"])
        controller = make_controller(ad)
        calls = {"n": 0}

        def flaky(page):
            calls["n"] += 1
            if calls["n"] == 1:
                return "RATE_LIMIT"
            raise RuntimeError("page closed")

        ad.check_page_errors = flaky
        result, _ = self.run_gate(controller)
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
