
# tests/test_parser_content.py - regression tests for <content> handling in parser.py
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import parser

L, G, S = chr(60), chr(62), chr(47)
NL = chr(10)
BS = chr(92)
CD_OPEN = L + "![CDATA["
CD_CLOSE = chr(93) * 2 + G
DONE = "__DO" + "NE__"


def build(inner):
    return (
        L + "message" + G + "x" + L + S + "message" + G
        + L + "command" + G
        + L + "tool_name" + G + "write_file" + L + S + "tool_name" + G
        + L + "path" + G + "a.txt" + L + S + "path" + G
        + L + "content" + G + inner + L + S + "content" + G
        + L + S + "command" + G + DONE
    )


def content_of(inner):
    res = parser.smart_parse_ai_output(build(inner))
    return res[0]["tools"][0]["parameters"]["content"]




if __name__ == "__main__":
    unittest.main()
