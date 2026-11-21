import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "core")):
    if p not in sys.path:
        sys.path.insert(0, p)

from adapters import gemini  # noqa: E402
from adapters.deepseek import DeepSeekWebAdapter  # noqa: E402
from subagents import runner as R, contracts as C  # noqa: E402

LT, GT = chr(60), chr(62)


def t(s):
    return s.replace("{", LT).replace("}", GT)


WRITE_WITH_MESSAGE_LINE = t(
    "{write path=\"a.xml\"}\n{message}hi{/message}\nmore text\n{/write}\n{sh}\necho done\n{/sh}")
SAY_DONE = t("{say}Done.{/say}") + "\n__DONE__"


class TestCleanersKeepWholeReply(unittest.TestCase):
    def _cleaners(self):
        return [gemini.clean_response_payload,
                DeepSeekWebAdapter().clean_payload]

    def test_write_body_with_old_tag_line_survives(self):
        for fn in self._cleaners():
            self.assertEqual(fn(WRITE_WITH_MESSAGE_LINE), WRITE_WITH_MESSAGE_LINE)

    def test_trailing_done_is_stripped(self):
        for fn in self._cleaners():
            self.assertEqual(fn(SAY_DONE), t("{say}Done.{/say}"))

    def test_empty_input(self):
        for fn in self._cleaners():
            self.assertEqual(fn(""), "")


class TestSummaryNotTakenFromBodies(unittest.TestCase):
    tag = C.SUMMARY_TAG

    def test_quoted_in_write(self):
        s = t("{write path=\"a.md\"}\n{" + self.tag + "}x{/" + self.tag + "}\n{/write}\n")
        self.assertEqual(R.extract_summary(s), (None, None))

    def test_quoted_in_sh(self):
        s = t("{sh}\necho '{" + self.tag + "}x{/" + self.tag + "}'\n{/sh}\n")
        self.assertEqual(R.extract_summary(s), (None, None))

    def test_quoted_in_labelled_write(self):
        s = t("{write:x1 path=\"a.md\"}\n{/write}\n{" + self.tag + "}q{/" + self.tag + "}\n{/write:x1}\n")
        self.assertEqual(R.extract_summary(s), (None, None))

    def test_plain_summary_still_found(self):
        s = t("{" + self.tag + "}All done.{/" + self.tag + "}")
        self.assertEqual(R.extract_summary(s), ("All done.", None))


if __name__ == "__main__":
    unittest.main()
