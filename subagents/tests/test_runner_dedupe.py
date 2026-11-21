"""Exact repeated tool calls inside ONE reply run once (runner dedupe). Offline, fake channel."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from subagents.fake import tool_reply, summary_reply
from subagents.tests.test_runner import Base, WRITE_TOML


def two_calls(a, b):
    return a + "\n" + b


class TestDedupe(Base):
    def marker_lines(self):
        p = os.path.join(self.work, "marks.txt")
        if not os.path.exists(p):
            return 0
        with open(p) as f:
            return len(f.read().splitlines())

    def mark(self, tag="x"):
        return tool_reply("run_command", command="echo %s >> marks.txt" % tag)

    def test_identical_repeat_runs_once_and_model_is_told(self):
        reply = two_calls(self.mark(), self.mark())
        res, ch, _ = self.run_it([reply, summary_reply("ok")], toml=WRITE_TOML, writer=True)
        self.assertEqual(res.status, "done")
        self.assertEqual(self.marker_lines(), 1)
        self.assertIn("identical repeated tool call", ch.prompts[1])

    def test_different_calls_both_run(self):
        reply = two_calls(self.mark("a"), self.mark("b"))
        res, ch, _ = self.run_it([reply, summary_reply("ok")], toml=WRITE_TOML, writer=True)
        self.assertEqual(res.status, "done")
        self.assertEqual(self.marker_lines(), 2)
        self.assertNotIn("identical repeated", ch.prompts[1])

    def test_repeat_across_replies_still_runs_each_time(self):
        res, ch, _ = self.run_it([self.mark(), self.mark(), summary_reply("ok")],
                                 toml=WRITE_TOML, writer=True)
        self.assertEqual(res.status, "done")
        self.assertEqual(self.marker_lines(), 2)

    def test_three_identical_replies_still_fail_as_loop(self):
        res, _, _ = self.run_it([self.mark(), self.mark(), self.mark(), summary_reply("ok")],
                                toml=WRITE_TOML, writer=True)
        self.assertEqual((res.status, res.reason), ("failed", "loop"))

    def test_dropped_repeat_does_not_count_toward_loop(self):
        reply = two_calls(self.mark(), self.mark())
        res, _, _ = self.run_it([reply, summary_reply("ok")], toml=WRITE_TOML, writer=True)
        self.assertEqual(res.status, "done")


if __name__ == "__main__":
    unittest.main()
