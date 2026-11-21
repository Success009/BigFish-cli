"""Tests for the plain-shell shorthand: a command block whose body is just shell text.

Tags are built from fragments (T, OPEN, CLOSE) so this file never contains a literal
tool tag that the parser or the harness could mistake for a real action.

Target behaviour (see tmp_plan/PLAN_shell_shorthand_bug.md):
  - plain shell text in a command block runs as run_command
  - angle-bracket words inside that shell text must NOT drop the action
  - a block with parameter tags but no tool_name is NOT shell
  - thought-like text is never shell
  - bash/sh outer tags are not recognised (dead branch, removed by the fix)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths  # noqa: F401
import parser as P

LT = "<"
DONE = "\n__DONE__"


def T(n, b):
    return LT + n + ">" + b + LT + "/" + n + ">"


def OPEN(n):
    return LT + n + ">"


def CLOSE(n):
    return LT + "/" + n + ">"


def SH(body, outer="command"):
    """A shorthand shell block: outer tag holding only shell text."""
    return OPEN(outer) + body + CLOSE(outer)


def CMD(tool, *params):
    """A structured tool block with tool_name and named parameter tags."""
    s = OPEN("command") + "\n" + T("tool_name", tool) + "\n"
    for k, v in params:
        s += T(k, v) + "\n"
    return s + CLOSE("command")


def tools_of(raw):
    """Run the real parser and return the list of tool calls (empty on PARSE_ERROR)."""
    r = P.smart_parse_ai_output(raw)
    if isinstance(r, str):
        return []
    return r[0]["tools"]


def shell_commands(raw):
    return [t["parameters"].get("command") for t in tools_of(raw) if t["tool"] == "run_command"]






class TestShellShorthandMustNotOverreach(unittest.TestCase):


    def test_param_tags_without_tool_name_is_not_shell(self):
        body = T("path", "a.txt") + T("search", "x") + T("replace", "y")
        raw = T("message", "edit") + "\n" + SH(body) + DONE
        self.assertEqual(shell_commands(raw), [])

    def test_content_tag_without_tool_name_is_not_shell(self):
        body = T("path", "a.txt") + OPEN("content") + "hello" + CLOSE("content")
        raw = T("message", "write") + "\n" + SH(body) + DONE
        self.assertEqual(shell_commands(raw), [])

    def test_thought_header_is_not_shell(self):
        raw = SH("**Analyzing the shell shorthand**") + DONE
        self.assertEqual(shell_commands(raw), [])

    def test_thought_with_angle_word_is_not_shell(self):
        body = "I suspect the " + OPEN("div") + " handling failed"
        raw = SH(body) + DONE
        self.assertEqual(shell_commands(raw), [])

    def test_shell_inside_message_body_is_not_executed(self):
        raw = T("message", "Example: " + SH("rm -rf build")) + DONE
        self.assertEqual(shell_commands(raw), [])

    def test_nothing_after_done_runs(self):
        raw = T("message", "ok") + DONE + "\n" + SH("rm -rf x")
        self.assertEqual(shell_commands(raw), [])


class TestBashTagIsDeadBranch(unittest.TestCase):
    """bash and sh are not in OPEN_TOOL_TAGS, so they never produce a tool call."""

    def test_bash_block_yields_no_tool(self):
        raw = SH("ls -la", outer="bash") + DONE
        self.assertEqual(tools_of(raw), [])


    def test_bash_not_in_open_tags(self):
        self.assertNotIn("bash", P.OPEN_TOOL_TAGS)
        self.assertNotIn("sh", P.OPEN_TOOL_TAGS)


if __name__ == "__main__":
    unittest.main()
