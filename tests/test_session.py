"""Unit tests for session.py — compact, save/load, latest, force_update, uniqueness."""
import os
import sys
import tempfile
import unittest
import json
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import session


BASE_URL = "https://aistudio.google.com/app/prompts/123456"


class TestSessionCompact(unittest.TestCase):
    """compact_history trims oversized AI turns."""

    def test_short_history_unchanged(self):
        h = [{"role": "User", "content": "hi"}, {"role": "AI", "content": "hello"}]
        out = session.compact_history(h)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["content"], "hi")
        self.assertEqual(out[1]["content"], "hello")

    def test_long_ai_turn_truncated(self):
        h = [
            {"role": "User", "content": "Short prompt"},
            {"role": "AI", "content": "A" * 5000}
        ]
        out = session.compact_history(h)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["content"], "Short prompt")
        self.assertIn("Truncated", out[1]["content"])

    def test_multiple_large_turns(self):
        h = [{"role": "AI", "content": "B" * 5000} for _ in range(5)]
        out = session.compact_history(h)
        for turn in out:
            self.assertIn("Truncated", turn["content"])

    def test_empty_history(self):
        out = session.compact_history([])
        self.assertEqual(out, [])


class TestSaveLoadSession(unittest.TestCase):
    """save_current_session and load_session_by_filename round-trip."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_dir = session.SESSION_DIR
        session.SESSION_DIR = self.tmp.name

    def tearDown(self):
        session.SESSION_DIR = self._orig_dir
        self.tmp.cleanup()

    def test_basic_save_and_load(self):
        fn = session.save_current_session(
            first_prompt="Hello Big Fish",
            current_url=BASE_URL,
            chat_history=[{"role": "User", "content": "Hello Big Fish"}],
            current_cwd=os.getcwd(),
            profile_email="test@gmail.com",
            profile_dir="Profile 1"
        )
        self.assertIsNotNone(fn)
        self.assertTrue(fn.endswith(".json"))

        url, loaded_fn, preview, hist, cwd, email, pdir = session.load_session_by_filename(fn)
        self.assertEqual(url, BASE_URL)
        self.assertEqual(loaded_fn, fn)
        self.assertIn("Hello Big Fish", preview)
        self.assertEqual(email, "test@gmail.com")
        self.assertEqual(pdir, "Profile 1")

    def test_url_locked_on_second_save(self):
        """URL must not change once set (unless force_update)."""
        fn = session.save_current_session(
            "p1", BASE_URL, [], "/cwd", "a@b.com", "P1"
        )
        new_url = "https://aistudio.google.com/app/prompts/NEWURL"
        session.save_current_session(
            "p1", new_url, [], "/cwd", "a@b.com", "P1", filename=fn
        )
        url, *_ = session.load_session_by_filename(fn)
        self.assertEqual(url, BASE_URL, "URL should be locked after first save")

    def test_force_update_overrides_email(self):
        """force_update=True must allow overriding locked email."""
        fn = session.save_current_session(
            "p", BASE_URL, [], "/cwd", "original@g.com", "P1"
        )
        session.save_current_session(
            "p", BASE_URL, [], "/cwd", "new@g.com", "P2",
            filename=fn, force_update=True
        )
        *_, email, pdir = session.load_session_by_filename(fn)
        self.assertEqual(email, "new@g.com")
        self.assertEqual(pdir, "P2")

    def test_skips_new_chat_url(self):
        """Sessions with 'new_chat' in URL must not be saved."""
        fn = session.save_current_session(
            "p", "https://aistudio.google.com/new_chat", [], "/cwd", "e", "P1"
        )
        self.assertIsNone(fn)

    def test_skips_empty_prompt(self):
        fn = session.save_current_session(
            "", BASE_URL, [], "/cwd", "e", "P1"
        )
        self.assertIsNone(fn)

    def test_session_filename_uniqueness(self):
        """Two saves with different timestamps produce different filenames."""
        fn1 = session.save_current_session("prompt A", BASE_URL, [], "/cwd", "e", "P1")
        time.sleep(1.1)  # ensure different second
        fn2 = session.save_current_session("prompt B", BASE_URL, [], "/cwd", "e", "P1")
        self.assertNotEqual(fn1, fn2)

    def test_preview_truncated_at_60_chars(self):
        long_prompt = "X" * 100
        fn = session.save_current_session(long_prompt, BASE_URL, [], "/cwd", "e", "P1")
        _, _, preview, *_ = session.load_session_by_filename(fn)
        self.assertLessEqual(len(preview), 65)  # 60 + "..."

    def test_get_latest_session_empty(self):
        self.assertIsNone(session.get_latest_session())

    def test_get_latest_session_after_save(self):
        session.save_current_session("first", BASE_URL, [], "/cwd", "e", "P1")
        latest = session.get_latest_session()
        self.assertIsNotNone(latest)

    def test_history_preserved_in_file(self):
        hist = [{"role": "User", "content": "test msg"}]
        fn = session.save_current_session("test msg", BASE_URL, hist, "/cwd", "e", "P1")
        *_, loaded_hist, _, _, _ = session.load_session_by_filename(fn)
        self.assertTrue(any("test msg" in str(t) for t in loaded_hist))

    def test_set_active_session_file_persists(self):
        fn1 = session.save_current_session("session 1", BASE_URL, [], "/cwd", "e", "P1")
        time.sleep(0.1)
        fn2 = session.save_current_session("session 2", BASE_URL, [], "/cwd", "e", "P1")
        # fn2 was saved last, but if user switches/activates fn1:
        session.set_active_session_file(fn1)
        self.assertEqual(session.get_latest_session(), fn1)

    def test_active_session_priority_and_mtime_touch(self):
        fn1 = session.save_current_session("p1", BASE_URL, [], "/cwd", "e", "P1")
        time.sleep(0.1)
        fn2 = session.save_current_session("p2", BASE_URL, [], "/cwd", "e", "P1")
        # Activate fn1
        session.set_active_session_file(fn1)
        # Even if config is cleared, fn1 mtime was touched so it remains latest
        session.save_cli_config({"last_active_session": None})
        self.assertEqual(session.get_latest_session(), fn1)

    def test_get_latest_session_by_provider(self):
        # Save a Gemini session
        fn_gem = session.save_current_session("gem prompt", "https://aistudio.google.com/app/prompts/123", [], "/cwd", "e", "P1", provider="gemini")
        time.sleep(0.1)
        # Save a DeepSeek session
        fn_ds = session.save_current_session("ds prompt", "https://chat.deepseek.com/a/chat/s/456", [], "/cwd", "e", "P1", provider="deepseek")
        
        # When querying without provider, returns latest (deepseek)
        self.assertEqual(session.get_latest_session(), fn_ds)
        # When querying for deepseek, returns deepseek
        self.assertEqual(session.get_latest_session(provider="deepseek"), fn_ds)
        # When querying for gemini, returns gemini
        self.assertEqual(session.get_latest_session(provider="gemini"), fn_gem)


if __name__ == "__main__":
    unittest.main()
