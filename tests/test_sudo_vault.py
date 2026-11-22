# /sudo vault: only a verified password is saved, no built-in default password
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import paths  # noqa
import executor as ex  # noqa


class TestSudoVault(unittest.TestCase):
    def setUp(self):
        self.vault = os.path.join(tempfile.mkdtemp(), "vault.json")
        self._old = (ex.VAULT_FILE, ex.verify_sudo)
        ex.VAULT_FILE = self.vault
        ex.verify_sudo = lambda p: p == "right"

    def tearDown(self):
        ex.VAULT_FILE, ex.verify_sudo = self._old

    def test_wrong_password_is_not_saved(self):
        self.assertFalse(ex.set_sudo("wrong"))
        self.assertFalse(os.path.exists(self.vault))

    def test_right_password_is_saved_and_loaded(self):
        self.assertTrue(ex.set_sudo("right"))
        self.assertEqual(ex.load_sudo(), "right")
        self.assertEqual(oct(os.stat(self.vault).st_mode & 0o777), oct(0o600))

    def test_stale_saved_password_is_dropped(self):
        ex.save_sudo("old-password")
        self.assertIsNone(ex.load_sudo())

    def test_empty_password_rejected(self):
        self.assertFalse(ex.set_sudo(""))

    def test_clear_removes_vault(self):
        ex.set_sudo("right")
        self.assertTrue(ex.clear_sudo())
        self.assertFalse(os.path.exists(self.vault))
        self.assertFalse(ex.clear_sudo())

    def test_no_hardcoded_password_in_source(self):
        src = open(ex.__file__, encoding="utf-8").read()
        self.assertNotIn("failsafe_pwd", src)


if __name__ == "__main__":
    unittest.main()
