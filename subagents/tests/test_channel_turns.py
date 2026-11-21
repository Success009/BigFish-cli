"""Tests for the WebChannel on_turn accounting hook. Offline: fake slot and controller."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from subagents.channel_web import WebChannel
from subagents.runner import ChannelError, RateLimited
from subagents.tests.test_manager_close import FakeCtl, FakeSlot


class SniffCtl(FakeCtl):
    def __init__(self, replies, sniffed=None):
        super().__init__(replies)
        self.sniffed = sniffed

    def get_last_prompt_tokens(self):
        return self.sniffed


class TestOnTurn(unittest.TestCase):
    def setUp(self):
        FakeSlot.instances = []
        self.calls = []

    def hook(self, profile, provider, p_tok, r_tok):
        self.calls.append((profile, provider, p_tok, r_tok))

    def channel(self, ctl, on_turn="default"):
        if on_turn == "default":
            on_turn = self.hook
        return WebChannel("gemini", "/src", "Profile 3", "/work", "a1",
                          slot_factory=FakeSlot, controller_factory=lambda c, p: ctl,
                          start_url="x", on_turn=on_turn, rate_wait=0, limiter=False)

    def test_sniffed_count_used(self):
        ch = self.channel(SniffCtl(["reply text"], sniffed=777))
        ch.send("p" * 100)
        self.assertEqual(self.calls, [("Profile 3", "gemini", 777, int(len("reply text") / 3.8))])

    def test_estimate_when_sniffed_none_zero_or_missing(self):
        for ctl in (SniffCtl(["r"], sniffed=None), SniffCtl(["r"], sniffed=0), FakeCtl(["r"])):
            self.calls.clear()
            ch = self.channel(ctl)
            ch.send("x" * 380)
            self.assertEqual(self.calls[0][2], 100)

    def test_hook_error_never_breaks_send(self):
        def boom(*a):
            raise RuntimeError("ledger locked")
        ch = self.channel(FakeCtl(["fine"]), on_turn=boom)
        self.assertEqual(ch.send("p"), "fine")

    def test_no_hook_call_on_failures(self):
        for bad, exc in (("RATE_LIMIT_DETECTED", RateLimited), ("FAILED_TO_PASTE", ChannelError), ("", ChannelError)):
            ch = self.channel(FakeCtl([bad, bad]))
            with self.assertRaises(exc):
                ch.send("p")
        self.assertEqual(self.calls, [])

    def test_no_hook_is_fine(self):
        ch = self.channel(FakeCtl(["ok"]), on_turn=None)
        self.assertEqual(ch.send("p"), "ok")

    def test_one_call_per_send(self):
        ch = self.channel(FakeCtl(["a", "b"]))
        ch.send("1")
        ch.send("2")
        self.assertEqual(len(self.calls), 2)


if __name__ == "__main__":
    unittest.main()
