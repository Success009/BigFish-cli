import tempfile
import unittest

from subagents import contracts as C
from subagents.channel_web import WebChannel
from subagents.factory import SubFactory
from subagents.runner import RateLimited


class Slot:
    def __init__(self, *a, **kw):
        pass

    def start(self):
        return "http://127.0.0.1:1"

    def close(self):
        pass


class Ctl:
    def __init__(self, reply):
        self.reply = reply
        self.page = type("P", (), {"url": "http://x"})()

    def connect(self, timeout=0, target_url=None):
        return True

    def send_prompt_and_get_response(self, prompt, marker, auto_submit=True):
        return self.reply

    def close(self):
        pass


def chan(reply, hook):
    return WebChannel("gemini", "/s", "Profile 7", "/w", "n", slot_factory=Slot,
                      controller_factory=lambda c, p: Ctl(reply), start_url="http://x",
                      on_rate_limit=hook, rate_wait=0, limiter=False)


class TestChannelHook(unittest.TestCase):
    def test_hook_called_with_profile_and_provider(self):
        seen = []
        with self.assertRaises(RateLimited):
            chan("RATE_LIMIT_DETECTED", lambda p, v: seen.append((p, v))).send("x")
        self.assertEqual(seen, [("Profile 7", "gemini")])

    def test_hook_error_does_not_hide_rate_limit(self):
        def boom(p, v):
            raise RuntimeError("ledger broken")
        with self.assertRaises(RateLimited):
            chan("RATE_LIMIT_DETECTED", boom).send("x")

    def test_normal_reply_does_not_call_hook(self):
        seen = []
        self.assertEqual(chan("fine", lambda p, v: seen.append(1)).send("x"), "fine")
        self.assertEqual(seen, [])


class KwChannel:
    def __init__(self, *a, **kw):
        self.kw = kw

    def close(self):
        pass


class TestFactoryHook(unittest.TestCase):
    def make(self, **kw):
        role = C.Role(name="explore", provider="gemini", tools=["read_file"], prompt="p")
        f = SubFactory(tempfile.mkdtemp(), profiles=lambda: ["Profile 7"], src_dir="/src",
                       channel_cls=KwChannel, **kw)
        return f, f(C.make_task(role, "brief"))

    def test_factory_passes_hook_that_marks_the_profile(self):
        calls = []
        f, ch = self.make(mark_limited=lambda p, v: calls.append((p, v)))
        ch.kw["on_rate_limit"]("Profile 7", "gemini")
        self.assertEqual(calls, [("Profile 7", "gemini")])

    def test_marking_failure_is_swallowed(self):
        def boom(p, v):
            raise RuntimeError("x")
        f, ch = self.make(mark_limited=boom)
        ch.kw["on_rate_limit"]("Profile 7", "gemini")


if __name__ == "__main__":
    unittest.main()
