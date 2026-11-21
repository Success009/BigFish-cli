"""Tests for subagents/profile_pick.py. Offline: temp ledger dicts, fixed timestamps."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from subagents import profile_pick as PP

NOW = 2_000_000_000
H = 3600
PROFILES = ["Default", "Profile 1", "Profile 2"]


def entry(daily, last, total=None, reset=None):
    return {"daily_tokens": daily, "last_used_timestamp": last, "total_tokens": total if total is not None else daily,
            "daily_reset_timestamp": reset if reset is not None else NOW + 20 * H}


def ledger(prov="gemini", **per_profile):
    return {"providers": {prov: {k.replace("_", " ", 1) if k.startswith("Profile_") else k: v
                                 for k, v in per_profile.items()}}}


class TestPick(unittest.TestCase):
    def test_least_used_wins(self):
        lg = {"providers": {"claude": {"Default": entry(900, NOW - H), "Profile 1": entry(100, NOW - H),
                                       "Profile 2": entry(500, NOW - H)}}}
        self.assertEqual(PP.pick_profile(PROFILES, [], "claude", ledger=lg, now_ts=NOW), "Profile 1")

    def test_in_use_skipped(self):
        lg = {"providers": {"claude": {"Default": entry(900, NOW - H), "Profile 1": entry(100, NOW - H),
                                       "Profile 2": entry(500, NOW - H)}}}
        self.assertEqual(PP.pick_profile(PROFILES, ["Profile 1"], "claude", ledger=lg, now_ts=NOW), "Profile 2")

    def test_all_in_use_raises(self):
        with self.assertRaises(PP.PickError):
            PP.pick_profile(PROFILES, PROFILES, "claude", ledger={}, now_ts=NOW)

    def test_unused_profile_beats_used(self):
        lg = {"providers": {"gemini": {"Default": entry(50, NOW - H), "Profile 1": entry(60, NOW - H)}}}
        self.assertEqual(PP.pick_profile(PROFILES, [], "gemini", ledger=lg, now_ts=NOW), "Profile 2")

    def test_rate_limited_skipped_only_for_that_provider(self):
        lg = {"providers": {
            "claude": {"Default": {"rate_limited_until": NOW + H, **entry(0, 0)}, "Profile 1": entry(500, NOW - H),
                       "Profile 2": entry(900, NOW - H)},
            "gemini": {"Default": entry(0, 0)}}}
        self.assertEqual(PP.pick_profile(PROFILES, [], "claude", ledger=lg, now_ts=NOW), "Profile 1")
        self.assertEqual(PP.pick_profile(PROFILES, [], "gemini", ledger=lg, now_ts=NOW), "Default")

    def test_all_limited_returns_soonest(self):
        lg = {"providers": {"claude": {
            "Default": {"rate_limited_until": NOW + 3 * H, **entry(0, 0)},
            "Profile 1": {"rate_limited_until": NOW + 1 * H, **entry(0, 0)},
            "Profile 2": {"rate_limited_until": NOW + 2 * H, **entry(0, 0)}}}}
        self.assertEqual(PP.pick_profile(PROFILES, [], "claude", ledger=lg, now_ts=NOW), "Profile 1")

    def test_gemini_24h_window(self):
        # used 23h ago: still counts. used 25h ago: counts as fresh (even though the daily reset is later)
        lg = {"providers": {"gemini": {
            "Default": entry(1000, NOW - 23 * H), "Profile 1": entry(1000, NOW - 25 * H),
            "Profile 2": entry(500, NOW - H)}}}
        self.assertEqual(PP.pick_profile(PROFILES, [], "gemini", ledger=lg, now_ts=NOW), "Profile 1")

    def test_claude_5h_window(self):
        lg = {"providers": {"claude": {
            "Default": entry(1000, NOW - 6 * H), "Profile 1": entry(10, NOW - H), "Profile 2": entry(10, NOW - H)}}}
        self.assertEqual(PP.pick_profile(PROFILES, [], "claude", ledger=lg, now_ts=NOW), "Default")

    def test_gemini_legacy_bucket_read(self):
        lg = {"profiles": {"Default": entry(1000, NOW - H), "Profile 1": entry(5, NOW - H),
                           "Profile 2": entry(1000, NOW - H)}}
        self.assertEqual(PP.pick_profile(PROFILES, [], "gemini", ledger=lg, now_ts=NOW), "Profile 1")

    def test_generator_input(self):
        got = PP.pick_profile((p for p in PROFILES), iter(["Default"]), "claude", ledger={}, now_ts=NOW)
        self.assertIn(got, ("Profile 1", "Profile 2"))

    def test_tie_breaks_on_total_then_name(self):
        lg = {"providers": {"claude": {"Default": entry(0, 0, total=900), "Profile 1": entry(0, 0, total=100),
                                       "Profile 2": entry(0, 0, total=100)}}}
        self.assertEqual(PP.pick_profile(PROFILES, [], "claude", ledger=lg, now_ts=NOW), "Profile 1")


if __name__ == "__main__":
    unittest.main()
