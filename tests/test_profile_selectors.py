# /next and /transfer selector rules and capture-log tags use profile_ids
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import paths  # noqa
import profile_ids as pid  # noqa

REAL_LIKE = {
    "Default": "successstorages1@gmail.com",
    "Profile 1": "successstorages2@gmail.com",
    "Profile 8": "successstorages9@gmail.com",
    "Profile 11": "successstorages10@gmail.com",
    "Profile 12": "successstorages11@gmail.com",
    "Profile 13": "successstorages12@gmail.com",
    "Profile 14": "successstorages13@gmail.com",
}


def _dir():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "Local State"), "w", encoding="utf-8") as f:
        json.dump({"profile": {"info_cache": {k: {"user_name": v} for k, v in REAL_LIKE.items()}}}, f)
    return d


class TestSelectors(unittest.TestCase):
    def setUp(self):
        self.d = _dir()
        self.avail = pid.ordered_dirs(self.d)

    def test_account_13_is_folder_14_and_in_list(self):
        sel = pid.resolve("13", self.d)
        self.assertEqual(sel, "Profile 14")
        self.assertIn(sel, self.avail)

    def test_label_text_roundtrips(self):
        for folder in self.avail:
            self.assertEqual(pid.resolve(pid.label(folder, self.d), self.d), folder)

    def test_account_with_no_profile_is_rejected(self):
        self.assertIsNone(pid.resolve("5", self.d))
        self.assertIsNone(pid.resolve("0", self.d))
        self.assertIsNone(pid.resolve("-1", self.d))
        self.assertIsNone(pid.resolve("13; rm -rf", self.d))

    def test_list_is_sequential_by_account(self):
        nums = [pid.account_number(p, self.d) for p in self.avail]
        self.assertEqual(nums, [1, 2, 9, 10, 11, 12, 13])


if __name__ == "__main__":
    unittest.main()
