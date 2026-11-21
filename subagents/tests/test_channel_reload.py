import unittest

from subagents.channel_web import WebChannel, ChannelError

SENT = "PERMISSION_DENIED_RELOADED"


class Slot:
    def __init__(self, *a, **kw):
        pass

    def start(self):
        return "http://127.0.0.1:1"

    def close(self):
        pass


class Ctl:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []
        self.page = type("P", (), {"url": "http://x/chat"})()

    def connect(self, timeout=0, target_url=None):
        return True

    def send_prompt_and_get_response(self, prompt, marker, auto_submit=True):
        self.prompts.append(prompt)
        return self.replies.pop(0)

    def close(self):
        pass


def chan(replies):
    ctl = Ctl(replies)
    ch = WebChannel("gemini", "/s", "Profile 1", "/w", "n", slot_factory=Slot,
                    controller_factory=lambda c, p: ctl, start_url="http://x", limiter=False)
    return ch, ctl


class TestReload(unittest.TestCase):
    def test_reload_then_good_reply_resends_same_prompt(self):
        ch, ctl = chan([SENT, "answer"])
        self.assertEqual(ch.send("do the task"), "answer")
        self.assertEqual(ctl.prompts, ["do the task", "do the task"])

    def test_two_reloads_then_ok(self):
        ch, ctl = chan([SENT, SENT, "answer"])
        self.assertEqual(ch.send("p"), "answer")
        self.assertEqual(len(ctl.prompts), 3)

    def test_too_many_reloads_raise_channel_error(self):
        ch, ctl = chan([SENT, SENT, SENT])
        with self.assertRaises(ChannelError):
            ch.send("p")
        self.assertEqual(len(ctl.prompts), 3)

    def test_normal_reply_sent_once(self):
        ch, ctl = chan(["fine"])
        self.assertEqual(ch.send("p"), "fine")
        self.assertEqual(len(ctl.prompts), 1)


if __name__ == "__main__":
    unittest.main()
