"""subagents.fake - a scripted channel for offline tests (plan item R1).

FakeChannel plays a list of replies. Each item is a reply string, an exception instance
(raised from send), or a callable taking the prompt and returning a reply string.
Helper builders make tool replies and summary replies without literal tags in test code.
"""
from __future__ import annotations

from typing import Callable, List, Union

from subagents.runner import ChannelError

LT = "<"
CD_OPEN = LT + "![" + "CDATA["
CD_CLOSE = "]]" + ">"


def T(name: str, body: str) -> str:
    return LT + name + ">" + body + LT + "/" + name + ">"


# NEWFMT: tool replies use the one-tag-per-tool syntax. Unmapped tools keep the old wrapper.
_LINE_TAGS = {
    "read_file": ("read", {"path": "path", "search_term": "find", "surrounding_lines": "around", "read_all": "all"}),
    "list_dir": ("ls", {"path": "path"}),
    "search_text": ("grep", {"path": "dir", "search_text": "text"}),
    "delete_file": ("rm", {"path": "path"}),
    "change_directory": ("cd", {"path": "path"}),
    "rename_file": ("mv", {"path": "from", "new_path": "to"}),
}


def tool_reply(tool: str, text: str = "", **params) -> str:
    """A reply with one tool call in the new tag syntax."""
    lt, gt = chr(60), chr(62)
    pre = (text + "\n") if text else ""
    p = {k: str(v) for k, v in params.items()}

    def attrs(pairs):
        return "".join(' %s="%s"' % (k, v) for k, v in pairs if v != "")

    if tool == "run_command":
        return pre + lt + "sh" + gt + p.get("command", "") + lt + "/sh" + gt
    if tool == "write_file":
        return (pre + lt + "write" + attrs([("path", p.get("path", ""))]) + gt + "\n"
                + p.get("content", "") + "\n" + lt + "/write" + gt)
    if tool in _LINE_TAGS:
        name, mp = _LINE_TAGS[tool]
        pairs = [(mp[k], v) for k, v in p.items() if k in mp]
        if "start_line" in p:
            pairs.append(("lines", p["start_line"] + "-" + p.get("end_line", p["start_line"])))
        return pre + lt + name + attrs(pairs) + "/" + gt
    s = pre + lt + "command" + gt + "\n" + T("tool_name", tool) + "\n"
    for k, v in params.items():
        if k == "content":
            s += lt + "content" + gt + CD_OPEN + "\n" + str(v) + "\n" + CD_CLOSE + lt + "/content" + gt + "\n"
        else:
            s += T(k, str(v)) + "\n"
    return s + lt + "/command" + gt


def summary_reply(text: str) -> str:
    return T("summary", text)


class FakeChannel:
    def __init__(self, replies: List[Union[str, BaseException, Callable]],
                 profile: str = "fake-profile", chat_url: str = "fake://chat/1"):
        self.replies = list(replies)
        self.prompts: List[str] = []
        self.profile = profile
        self.chat_url = chat_url

    def send(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.replies:
            raise ChannelError("fake channel ran out of scripted replies")
        r = self.replies.pop(0)
        if isinstance(r, BaseException):
            raise r
        if callable(r):
            return r(prompt)
        return r
