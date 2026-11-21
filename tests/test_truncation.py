"""Tests for parser.detect_truncation: flags replies cut off inside a tool call,
and must never fire on normal replies (false alarms)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths  # noqa: F401
import parser

# Built by concatenation so this file does not contain a literal CDATA marker pair.
CD_OPEN = "<![CD" + "ATA["
CD_CLOSE = "]" + "]>"


class TestCutOff(unittest.TestCase):
    def flagged(self, text):
        self.assertIsNotNone(parser.detect_truncation(text), text[:60])







class TestNoFalseAlarm(unittest.TestCase):
    def ok(self, text):
        self.assertIsNone(parser.detect_truncation(text), (text or "")[:60])

    def test_empty_and_none(self):
        self.ok("")
        self.ok(None)

    def test_plain_message(self):
        self.ok("<message>All done, nothing to run.</message>")

    def test_plain_text_no_tags(self):
        self.ok("Sure, here is the answer.")

    def test_message_mentions_command_tag(self):
        self.ok("<message>Use a <command> block to run things.</message>")

    def test_backtick_mention(self):
        self.ok("<message>The `<command>` tag opens a call.</message>")

    def test_complete_command(self):
        self.ok("<message>Go.</message>\n<command>\n<tool_name>run_command</tool_name>\n<command>ls</command>\n</command>")

    def test_cdata_containing_close_tag_text(self):
        self.ok("<command>\n<tool_name>write_file</tool_name>\n<path>n.md</path>\n<content>" + CD_OPEN
                + "\nuse </command> to close\n" + CD_CLOSE + "</content>\n</command>")

    def test_message_then_complete_command(self):
        self.ok("<message>Reading.</message>\n<command><tool_name>read_file</tool_name><path>a.py</path></command>")


if __name__ == "__main__":
    unittest.main()
