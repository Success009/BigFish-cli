# Transfer target selection: only signed-in, not rate limited profiles qualify
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import paths  # noqa
import tokens  # noqa

MAILS = {"Default": "user0@example.com", "Profile 1": "user1@example.com", "Profile 2": ""}


class TestTransferTarget(unittest.TestCase):
    def setUp(self):
        self.ledger = os.path.join(tempfile.mkdtemp(), "ledger.json")
        self.profiles = ["Default", "Profile 1", "Profile 2"]

    def pick(self, cur, profs=None, mails=None):
        table = MAILS if mails is None else mails
        return tokens.pick_transfer_target(profs or self.profiles, cur, "gemini",
                                           ledger_file=self.ledger, email_of=lambda p: table.get(p, ""))

    def test_skips_signed_out_profile(self):
        self.assertEqual(self.pick("Default"), "Profile 1")

    def test_none_when_only_other_usable_profile_is_limited(self):
        tokens.mark_rate_limited("Profile 1", "gemini", ledger_file=self.ledger)
        self.assertIsNone(self.pick("Default"))

    def test_none_when_every_other_profile_is_limited(self):
        tokens.mark_rate_limited("Default", "gemini", ledger_file=self.ledger)
        tokens.mark_rate_limited("Profile 1", "gemini", ledger_file=self.ledger)
        self.assertIsNone(self.pick("Profile 2"))

    def test_none_with_single_profile(self):
        self.assertIsNone(self.pick("Default", ["Default"]))

    def test_current_profile_is_never_returned(self):
        self.assertNotEqual(self.pick("Profile 1"), "Profile 1")

    def test_signed_in_check_skipped_when_no_account_is_readable(self):
        self.assertEqual(self.pick("Default", mails={}), "Profile 1")

    def test_limit_on_other_provider_does_not_block(self):
        tokens.mark_rate_limited("Profile 1", "deepseek", ledger_file=self.ledger)
        self.assertEqual(self.pick("Default"), "Profile 1")


if __name__ == "__main__":
    unittest.main()
