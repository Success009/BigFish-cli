"""Tags written inside single backticks in prose must not open or close tool blocks."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths  # noqa: F401
import parser as P

LT = "<"
BT = "`"
FENCE = BT * 3


def T(name, body):
    return LT + name + ">" + body + LT + "/" + name + ">"


def call(cmd):
    return LT + "command>\n" + T("tool_name", "run_command") + "\n" + T("command", cmd) + "\n" + LT + "/command>"


def tools_of(text):
    res = P.smart_parse_ai_output(text)
    return [] if isinstance(res, str) else list(res[0]["tools"])


class TestInlineCodeTags(unittest.TestCase):


    def test_backticked_tag_alone_is_no_call(self):
        text = "The format is " + BT + LT + "command>" + BT + " and " + BT + LT + "/command>" + BT + "."
        self.assertEqual(tools_of(text), [])



    def test_mask_keeps_length(self):
        text = "a " + BT + LT + "command>" + BT + " b"
        self.assertEqual(len(P._mask_inline_code_tags(text)), len(text))

    def test_mask_ignores_fence_adjacent_backticks(self):
        text = FENCE + LT + "command>" + FENCE
        self.assertEqual(P._mask_inline_code_tags(text), text)



if __name__ == "__main__":
    unittest.main()
