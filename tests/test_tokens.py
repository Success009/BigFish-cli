# tests/test_tokens.py - Unit tests for token accounting and intelligent routing
import os
import sys
import json
import time
import tempfile
import unittest

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tokens
import ui

class TestTokenManagementAndRouting(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ledger_file = os.path.join(self.temp_dir.name, "test_account_token_ledger.json")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_calculate_tokens(self):
        self.assertEqual(tokens.calculate_tokens(""), 0)
        self.assertEqual(tokens.calculate_tokens(None), 0)
        
        # 38 characters -> 10 tokens
        sample_38 = "a" * 38
        self.assertEqual(tokens.calculate_tokens(sample_38), 10)
        
        # 39 characters -> ceil(39 / 3.8) = ceil(10.26) = 11 tokens
        sample_39 = "a" * 39
        self.assertEqual(tokens.calculate_tokens(sample_39), 11)

    def test_daily_reset_calculation(self):
        now = time.time()
        next_ts = tokens.get_next_daily_reset_timestamp(now)
        self.assertGreater(next_ts, now)
        # Should be within next 24 hours + a few seconds
        self.assertLessEqual(next_ts - now, 86400)

    def test_load_save_ledger(self):
        # File doesn't exist yet
        ledger = tokens.load_ledger(self.ledger_file)
        self.assertEqual(ledger.get("version"), "1.0")
        self.assertEqual(ledger.get("profiles"), {})

        # Save data
        ledger["profiles"]["Default"] = {
            "email": "test@gmail.com",
            "tokens_sent": 100,
            "tokens_received": 50,
            "total_tokens": 150,
            "daily_tokens": 150,
            "daily_reset_timestamp": int(time.time() + 3600),
            "last_used_timestamp": int(time.time()),
            "session_turns_count": 1
        }
        tokens.save_ledger(ledger, self.ledger_file)
        self.assertTrue(os.path.exists(self.ledger_file))

        # Reload and verify
        reloaded = tokens.load_ledger(self.ledger_file)
        self.assertIn("Default", reloaded["profiles"])
        self.assertEqual(reloaded["profiles"]["Default"]["email"], "test@gmail.com")
        self.assertEqual(reloaded["profiles"]["Default"]["total_tokens"], 150)

    def test_record_turn_tokens(self):
        entry = tokens.record_turn_tokens(
            profile_dir="Default",
            prompt_tokens=1000,
            response_tokens=250,
            email="primary@gmail.com",
            ledger_file=self.ledger_file
        )
        self.assertEqual(entry["tokens_sent"], 1000)
        self.assertEqual(entry["tokens_received"], 250)
        self.assertEqual(entry["total_tokens"], 1250)
        self.assertEqual(entry["daily_tokens"], 1250)
        self.assertEqual(entry["session_turns_count"], 1)
        self.assertEqual(entry["email"], "primary@gmail.com")

        # Second turn on same profile
        entry2 = tokens.record_turn_tokens(
            profile_dir="Default",
            prompt_tokens=500,
            response_tokens=100,
            email="primary@gmail.com",
            ledger_file=self.ledger_file
        )
        self.assertEqual(entry2["tokens_sent"], 1500)
        self.assertEqual(entry2["tokens_received"], 350)
        self.assertEqual(entry2["total_tokens"], 1850)
        self.assertEqual(entry2["daily_tokens"], 1850)
        self.assertEqual(entry2["session_turns_count"], 2)

    def test_daily_reset_rollover(self):
        # Create an entry whose reset timestamp has already passed
        expired_ts = time.time() - 100
        ledger = {
            "version": "1.0",
            "profiles": {
                "Profile 1": {
                    "email": "p1@gmail.com",
                    "tokens_sent": 10000,
                    "tokens_received": 5000,
                    "total_tokens": 15000,
                    "daily_tokens": 15000,
                    "daily_reset_timestamp": expired_ts,
                    "last_used_timestamp": expired_ts,
                    "session_turns_count": 5
                }
            }
        }
        tokens.save_ledger(ledger, self.ledger_file)

        # Recording a new turn should reset daily_tokens before adding current turn tokens
        updated = tokens.record_turn_tokens(
            profile_dir="Profile 1",
            prompt_tokens=300,
            response_tokens=100,
            ledger_file=self.ledger_file
        )
        # Lifetime total accumulated
        self.assertEqual(updated["total_tokens"], 15400)
        # Daily tokens reset to 0, then +400 = 400
        self.assertEqual(updated["daily_tokens"], 400)
        self.assertGreater(updated["daily_reset_timestamp"], time.time())

    def test_least_used_profile_routing(self):
        future_ts = time.time() + 86400
        ledger = {
            "version": "1.0",
            "profiles": {
                "Default": {
                    "email": "heavy@gmail.com",
                    "tokens_sent": 50000,
                    "tokens_received": 20000,
                    "total_tokens": 70000,
                    "daily_tokens": 50000,
                    "daily_reset_timestamp": future_ts,
                    "last_used_timestamp": time.time(),
                    "session_turns_count": 20
                },
                "Profile 1": {
                    "email": "medium@gmail.com",
                    "tokens_sent": 15000,
                    "tokens_received": 5000,
                    "total_tokens": 20000,
                    "daily_tokens": 12000,
                    "daily_reset_timestamp": future_ts,
                    "last_used_timestamp": time.time(),
                    "session_turns_count": 8
                },
                "Profile 2": {
                    "email": "idle@gmail.com",
                    "tokens_sent": 2000,
                    "tokens_received": 1000,
                    "total_tokens": 3000,
                    "daily_tokens": 1500,
                    "daily_reset_timestamp": future_ts,
                    "last_used_timestamp": time.time(),
                    "session_turns_count": 2
                }
            }
        }
        tokens.save_ledger(ledger, self.ledger_file)
        available = ["Default", "Profile 1", "Profile 2"]

        # If currently on Default, should route to Profile 2 (lowest daily usage)
        selected = tokens.get_least_used_profile(available, "Default", ledger_file=self.ledger_file)
        self.assertEqual(selected, "Profile 2")

        # If currently on Profile 2, should route to Profile 1
        selected_p2 = tokens.get_least_used_profile(available, "Profile 2", ledger_file=self.ledger_file)
        self.assertEqual(selected_p2, "Profile 1")

        # Unrecorded profile (e.g. Profile 3) should have 0 tokens and win selection
        available_with_fresh = ["Default", "Profile 1", "Profile 2", "Profile 3"]
        selected_fresh = tokens.get_least_used_profile(available_with_fresh, "Default", ledger_file=self.ledger_file)
        self.assertEqual(selected_fresh, "Profile 3")

    def test_least_used_profile_tie_breaker(self):
        future_ts = time.time() + 86400
        ledger = {
            "version": "1.0",
            "profiles": {
                "Profile 1": {
                    "email": "p1@gmail.com",
                    "daily_tokens": 5000,
                    "total_tokens": 50000,
                    "daily_reset_timestamp": future_ts
                },
                "Profile 2": {
                    "email": "p2@gmail.com",
                    "daily_tokens": 5000,  # Same daily tokens
                    "total_tokens": 10000,  # Lower lifetime tokens -> should win tie-break
                    "daily_reset_timestamp": future_ts
                }
            }
        }
        tokens.save_ledger(ledger, self.ledger_file)
        selected = tokens.get_least_used_profile(["Default", "Profile 1", "Profile 2"], "Default", ledger_file=self.ledger_file)
        self.assertEqual(selected, "Profile 2")

    def test_single_or_empty_candidates_graceful_fallback(self):
        # When only 1 profile exists
        self.assertEqual(tokens.get_least_used_profile(["Default"], "Default"), "Default")
        # When no profiles exist
        self.assertIsNone(tokens.get_least_used_profile([], "Default"))

    def test_profiles_usage_summary_and_ui_rendering(self):
        tokens.record_turn_tokens("Default", 100, 50, email="user@gmail.com", ledger_file=self.ledger_file)
        summary = tokens.get_profiles_usage_summary(["Default", "Profile 1"], ledger_file=self.ledger_file)
        
        self.assertEqual(len(summary), 2)
        self.assertEqual(summary[0]["profile_dir"], "Default")
        self.assertEqual(summary[0]["tokens_sent"], 100)
        self.assertEqual(summary[0]["tokens_received"], 50)
        self.assertEqual(summary[1]["profile_dir"], "Profile 1")
        self.assertEqual(summary[1]["daily_tokens"], 0)

        # Verify UI render methods do not crash
        try:
            ui.show_token_ledger_table(summary, active_profile="Default")
            ui.show_profile_usage_analysis(summary, selected_profile="Profile 1")
        except Exception as e:
            self.fail(f"UI token rendering raised an exception: {e}")

if __name__ == "__main__":
    unittest.main()
