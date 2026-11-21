"""Prompt text checks (plan items P1-P4)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from subagents import contracts as C
from subagents import prompts as PR


class TestPrompts(unittest.TestCase):
    def setUp(self):
        self.role = C.load_role("explore")
        self.task = C.make_task(self.role, "find X", scope=["core/"])

    def test_first_prompt_has_finish_rule_cap_and_task(self):
        t = PR.first_prompt(self.role.prompt, self.task)
        self.assertIn(PR.SUMMARY_OPEN, t)
        self.assertIn("NO tool call", t)
        self.assertIn("under %d characters" % self.task.summary_cap, t)
        self.assertIn("find X", t)
        self.assertIn("core/", t)

    def test_main_section_names_tools_and_has_no_tags(self):
        s = PR.main_agent_section()
        for w in ("spawn_subagent", "list_subagents", "cancel_subagent", "brief", "wait"):
            self.assertIn(w, s)
        self.assertNotIn("<", s)
        self.assertNotIn("scratchpad", s)

    def test_other_prompts_nonempty(self):
        for t in (PR.wrapup_text(), PR.summary_with_tools_prompt(), PR.bad_format_prompt("x"), PR.resume_prompt(self.task)):
            self.assertTrue(t.strip())


if __name__ == "__main__":
    unittest.main()
