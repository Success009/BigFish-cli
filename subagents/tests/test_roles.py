"""The shipped role files must load through the real loader (plan items P3, P4)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from subagents import contracts as C


class TestShippedRoles(unittest.TestCase):
    def test_explore_is_read_only(self):
        r = C.load_role("explore")
        self.assertFalse(r.writes)
        self.assertEqual(r.provider, "gemini")
        self.assertIn("read_file", r.tools)

    def test_worker_can_write(self):
        r = C.load_role("worker")
        self.assertTrue(r.writes)
        self.assertIn("edit_file", r.tools)

    def test_no_model_name_outside_provider_field(self):
        for name in ("explore", "worker"):
            r = C.load_role(name)
            self.assertNotIn("gemini", r.prompt.lower())
            self.assertNotIn("deepseek", r.prompt.lower())

    def test_prompts_do_not_name_the_finish_tag_or_paths(self):
        # The harness owns the finish format and the result path (prompts.py), not the role text.
        for name in ("explore", "worker"):
            r = C.load_role(name)
            self.assertNotIn("result.md", r.prompt)
            self.assertNotIn("<summary", r.prompt)


if __name__ == "__main__":
    unittest.main()
