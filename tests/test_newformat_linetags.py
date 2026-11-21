import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "core")):
    if p not in sys.path:
        sys.path.insert(0, p)

from adapters import gemini  # noqa: E402
from adapters.deepseek import DeepSeekWebAdapter  # noqa: E402
from core import parser as P  # noqa: E402

LT, GT = chr(60), chr(62)


def t(s):
    return s.replace("{", LT).replace("}", GT)


class TestLoneLineTags(unittest.TestCase):
    def _cleaners(self):
        return [gemini.clean_response_payload,
                DeepSeekWebAdapter().clean_payload]

    def test_lone_line_tags_are_not_pure_thought(self):
        for s in (t('{read path="a.txt"/}'), t('{ls path="."/}'), t("{bg_list/}")):
            self.assertFalse(P.is_pure_thought_text(s))

    def test_lone_line_tag_runs_through_every_cleaner(self):
        cases = {
            t('{read path="a.txt"/}'): "read_file",
            t('{ls path="."/}'): "list_dir",
            t("{bg_list/}"): "list_background_jobs",
        }
        for raw, tool in cases.items():
            for fn in self._cleaners():
                out = fn(raw)
                self.assertEqual(out, raw)
                r = P.smart_parse_ai_output(out)
                self.assertNotIsInstance(r, str)
                self.assertEqual([x["tool"] for x in r[0]["tools"]], [tool])

    def test_bad_attribute_gives_specific_error(self):
        raw = t('{read pth="a.txt"/}')
        for fn in self._cleaners():
            r = P.smart_parse_ai_output(fn(raw))
            self.assertEqual(r, "PARSE_ERROR")
            self.assertIn("pth", " ".join(P.LAST_PARSE_ERRORS))

    def test_real_thought_stays_a_thought(self):
        s = "**Analyzing the Request**\nI need to consider the user's prompt."
        self.assertTrue(P.is_pure_thought_text(s))
        self.assertTrue(P.is_pure_thought_text(""))


if __name__ == "__main__":
    unittest.main()
