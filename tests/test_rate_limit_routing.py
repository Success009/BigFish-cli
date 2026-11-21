# tests/test_rate_limit_routing.py - per-provider rate-limit tracking, routing and capture
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tokens
import cdp_stream


class TestPerProviderRateLimit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = os.path.join(self.tmp.name, "ledger.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_mark_and_check(self):
        now = 1_000_000
        until = tokens.mark_rate_limited("Default", "claude", ledger_file=self.ledger, now_ts=now)
        self.assertEqual(until, now + 5 * 3600)
        self.assertTrue(tokens.is_rate_limited("Default", "claude", ledger_file=self.ledger, now_ts=now + 60))

    def test_limit_expires_after_five_hours(self):
        now = 1_000_000
        tokens.mark_rate_limited("Default", "claude", ledger_file=self.ledger, now_ts=now)
        self.assertFalse(tokens.is_rate_limited("Default", "claude", ledger_file=self.ledger, now_ts=now + 5 * 3600 + 1))

    def test_other_provider_same_profile_unaffected(self):
        now = 1_000_000
        tokens.mark_rate_limited("Default", "claude", ledger_file=self.ledger, now_ts=now)
        self.assertFalse(tokens.is_rate_limited("Default", "gemini", ledger_file=self.ledger, now_ts=now + 1))

    def test_other_profile_same_provider_unaffected(self):
        now = 1_000_000
        tokens.mark_rate_limited("Default", "claude", ledger_file=self.ledger, now_ts=now)
        self.assertFalse(tokens.is_rate_limited("Profile 1", "claude", ledger_file=self.ledger, now_ts=now + 1))

    def test_clear_rate_limit(self):
        tokens.mark_rate_limited("Default", "claude", ledger_file=self.ledger)
        self.assertTrue(tokens.clear_rate_limit("Default", "claude", ledger_file=self.ledger))
        self.assertFalse(tokens.is_rate_limited("Default", "claude", ledger_file=self.ledger))


class TestProviderAwareRouting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = os.path.join(self.tmp.name, "ledger.json")
        self.profiles = ["Default", "Profile 1", "Profile 2"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_picks_least_used_for_that_provider(self):
        tokens.record_turn_tokens("Profile 1", 5000, 5000, ledger_file=self.ledger, provider="claude")
        tokens.record_turn_tokens("Profile 2", 100, 100, ledger_file=self.ledger, provider="claude")
        pick = tokens.get_least_used_profile(self.profiles, "Default", ledger_file=self.ledger, provider="claude")
        self.assertEqual(pick, "Profile 2")

    def test_skips_rate_limited_profile(self):
        # Profile 2 is the least used but limited, so Profile 1 must win.
        tokens.record_turn_tokens("Profile 1", 5000, 5000, ledger_file=self.ledger, provider="claude")
        tokens.record_turn_tokens("Profile 2", 100, 100, ledger_file=self.ledger, provider="claude")
        tokens.mark_rate_limited("Profile 2", "claude", ledger_file=self.ledger)
        pick = tokens.get_least_used_profile(self.profiles, "Default", ledger_file=self.ledger, provider="claude")
        self.assertEqual(pick, "Profile 1")

    def test_gemini_limit_does_not_block_claude_routing(self):
        tokens.record_turn_tokens("Profile 1", 5000, 5000, ledger_file=self.ledger, provider="claude")
        tokens.record_turn_tokens("Profile 2", 100, 100, ledger_file=self.ledger, provider="claude")
        tokens.mark_rate_limited("Profile 2", "gemini", ledger_file=self.ledger)
        pick = tokens.get_least_used_profile(self.profiles, "Default", ledger_file=self.ledger, provider="claude")
        self.assertEqual(pick, "Profile 2")

    def test_all_limited_returns_soonest_reset(self):
        now = time.time()
        tokens.mark_rate_limited("Profile 1", "claude", ledger_file=self.ledger, now_ts=now - 3600)
        tokens.mark_rate_limited("Profile 2", "claude", ledger_file=self.ledger, now_ts=now)
        pick = tokens.get_least_used_profile(self.profiles, "Default", ledger_file=self.ledger, provider="claude")
        self.assertEqual(pick, "Profile 1")

    def test_excludes_current_profile(self):
        pick = tokens.get_least_used_profile(self.profiles, "Default", ledger_file=self.ledger, provider="claude")
        self.assertNotEqual(pick, "Default")

    def test_legacy_call_without_provider_still_works(self):
        pick = tokens.get_least_used_profile(self.profiles, "Default", ledger_file=self.ledger)
        self.assertIn(pick, ("Profile 1", "Profile 2"))


class TestCapturePersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_http_error_looks_like_limit(self):
        self.assertTrue(cdp_stream.looks_like_limit(429, ""))
        self.assertTrue(cdp_stream.looks_like_limit(500, "oops"))

    def test_short_body_with_limit_wording(self):
        body = 'event: error\ndata: {"error":{"type":"rate_limit_error"}}'
        self.assertTrue(cdp_stream.looks_like_limit(200, body))

    def test_long_normal_reply_not_a_limit(self):
        body = "This reply discusses a rate limit. " + ("filler " * 1000)
        self.assertFalse(cdp_stream.looks_like_limit(200, body))

    def test_plain_reply_not_a_limit(self):
        self.assertFalse(cdp_stream.looks_like_limit(200, "Hello, here is your answer."))

    def test_save_capture_writes_full_json(self):
        path = cdp_stream.save_capture(
            "https://claude.ai/api/organizations/x/chat_conversations/y/completion",
            429, {"retry-after": "3600"}, '{"error":"rate_limit"}', False,
            meta={"provider": "claude"}, capture_dir=self.tmp.name)
        self.assertTrue(path and os.path.exists(path))
        data = json.load(open(path, encoding="utf-8"))
        self.assertEqual(data["status"], 429)
        self.assertEqual(data["headers"]["retry-after"], "3600")
        self.assertEqual(data["body"], '{"error":"rate_limit"}')
        self.assertEqual(data["meta"]["provider"], "claude")

    def test_capture_folder_is_capped(self):
        for i in range(cdp_stream.MAX_CAPTURE_FILES + 10):
            cdp_stream.save_capture("u", 429, {}, "b%d" % i, False, capture_dir=self.tmp.name)
            time.sleep(0.002)
        files = [f for f in os.listdir(self.tmp.name) if f.endswith(".json")]
        self.assertLessEqual(len(files), cdp_stream.MAX_CAPTURE_FILES)


if __name__ == "__main__":
    unittest.main()
