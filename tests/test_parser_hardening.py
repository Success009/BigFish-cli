import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import parser as P


def T(n, b):
    return "<" + n + ">" + b + "</" + n + ">"


def CD(s):
    return "<![" + "CDATA[" + s + "]]" + ">"


def OPEN(n):
    return "<" + n + ">"


def CLOSE(n):
    return "</" + n + ">"


def CMD(tool, *params):
    s = OPEN("command") + "\n" + T("tool_name", tool) + "\n"
    for k, v in params:
        s += v if k is None else T(k, v) + "\n"
    return s + CLOSE("command")


DONE = "\n__DONE__"




if __name__ == "__main__":
    unittest.main()
