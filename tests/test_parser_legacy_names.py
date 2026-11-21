import unittest
import paths  # noqa: sets up sub-folder imports
import parser as P

LT = "<"
GT = ">"


def tag(name, body):
    return LT + name + GT + body + LT + "/" + name + GT


def call(name, body=None):
    if body is None:
        body = tag("path", "a.txt")
    return tag("command", tag("tool_name", name) + body)


def first(result):
    action = result[0] if isinstance(result, list) else result
    tools = action.get("tools")
    if tools:
        return tools[0]
    return action


class TestLegacyNames(unittest.TestCase):
    def tool_of(self, name):
        return first(P.smart_parse_ai_output(call(name))).get("tool")







if __name__ == "__main__":
    unittest.main()
