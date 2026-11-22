# Profile translation layer: Chrome folder <-> account number
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import paths  # noqa
import profile_ids as pid  # noqa

FOLDERS = {
    "Default": "successstorages1@gmail.com",
    "Profile 1": "successstorages2@gmail.com",
    "Profile 2": "successstorages3@gmail.com",
    "Profile 8": "successstorages9@gmail.com",
    "Profile 11": "successstorages10@gmail.com",
    "Profile 14": "successstorages13@gmail.com",
    "Profile 13": "successstorages12@gmail.com",
}


def _make_dir(folders):
    d = tempfile.mkdtemp()
    cache = {k: {"user_name": v} for k, v in folders.items()}
    with open(os.path.join(d, "Local State"), "w", encoding="utf-8") as f:
        json.dump({"profile": {"info_cache": cache}}, f)
    return d


class TestProfileIds(unittest.TestCase):
    def setUp(self):
        self.d = _make_dir(FOLDERS)

    def test_label_uses_account_number_not_folder(self):
        self.assertEqual(pid.label("Default", self.d), "Profile 1")
        self.assertEqual(pid.label("Profile 14", self.d), "Profile 13")
        self.assertEqual(pid.label("Profile 11", self.d), "Profile 10")

    def test_unknown_folder_falls_back_to_name(self):
        self.assertEqual(pid.label("Profile 99", self.d), "Profile 99")
        self.assertEqual(pid.label("", self.d), "?")

    def test_order_is_sequential_by_account_with_gaps(self):
        order = pid.ordered_dirs(self.d)
        nums = [pid.account_number(p, self.d) for p in order]
        self.assertEqual(nums, sorted(nums))
        self.assertEqual(order[0], "Default")
        self.assertEqual(order[-1], "Profile 14")

    def test_dir_for_account(self):
        self.assertEqual(pid.dir_for_account(13, self.d), "Profile 14")
        self.assertEqual(pid.dir_for_account("1", self.d), "Default")
        self.assertIsNone(pid.dir_for_account(7, self.d))
        self.assertIsNone(pid.dir_for_account("x", self.d))

    def test_resolve_accepts_number_label_and_folder(self):
        self.assertEqual(pid.resolve("13", self.d), "Profile 14")
        self.assertEqual(pid.resolve("profile 13", self.d), "Profile 14")
        self.assertEqual(pid.resolve("Profile 13", self.d), "Profile 14")
        self.assertEqual(pid.resolve("default", self.d), "Default")
        self.assertIsNone(pid.resolve("", self.d))
        self.assertIsNone(pid.resolve("42", self.d))

    def test_missing_or_broken_local_state_is_safe(self):
        empty = tempfile.mkdtemp()
        self.assertEqual(pid.ordered_dirs(empty), [])
        self.assertEqual(pid.label("Default", empty), "Default")
        bad = tempfile.mkdtemp()
        with open(os.path.join(bad, "Local State"), "w") as f:
            f.write("{not json")
        self.assertEqual(pid.ordered_dirs(bad), [])

    def test_cache_refreshes_when_file_changes(self):
        self.assertEqual(pid.label("Profile 2", self.d), "Profile 3")
        p = os.path.join(self.d, "Local State")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"profile": {"info_cache": {"Profile 2": {"user_name": "successstorages7@gmail.com"}}}}, f)
        os.utime(p, (os.path.getmtime(p) + 5, os.path.getmtime(p) + 5))
        self.assertEqual(pid.label("Profile 2", self.d), "Profile 7")


if __name__ == "__main__":
    unittest.main()
