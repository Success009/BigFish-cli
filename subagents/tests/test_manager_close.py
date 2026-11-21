"""Channel closing in the manager, and WebChannel with fake slot and controller (plan item A2). Offline."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from subagents import contracts as C
from subagents.channel_web import WebChannel
from subagents.fake import FakeChannel, summary_reply
from subagents.manager import Manager
from subagents.runner import ChannelError, RateLimited

READ_TOML = 'provider = "gemini"\ntools = ["read_file", "list_dir"]\nprompt = "Read only."\n'


class ClosableChannel(FakeChannel):
    def __init__(self, replies, close_raises=False):
        super().__init__(replies)
        self.closed = 0
        self.close_raises = close_raises

    def close(self):
        self.closed += 1
        if self.close_raises:
            raise RuntimeError("close boom")


class TestManagerCloses(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self.runs = os.path.join(self.root, "runs")
        self.roles = os.path.join(self.root, "roles")
        os.makedirs(self.roles)
        with open(os.path.join(self.roles, "explore.toml"), "w") as f:
            f.write(READ_TOML)

    def tearDown(self):
        self._tmp.cleanup()

    def mgr(self, factory):
        return Manager(factory, runs_dir=self.runs, roles_dir=self.roles)

    def test_closed_after_normal_run(self):
        ch = ClosableChannel([summary_reply("ok")])
        m = self.mgr(lambda t, s: ch)
        m.spawn("explore", "x", wait=True)
        m.wait_all(10)
        self.assertEqual(ch.closed, 1)

    def test_closed_after_failed_run(self):
        ch = ClosableChannel([ChannelError("page crashed")])
        m = self.mgr(lambda t, s: ch)
        _, text = m.spawn("explore", "x", wait=True)
        m.wait_all(10)
        self.assertEqual(ch.closed, 1)

    def test_close_error_does_not_break_the_run(self):
        ch = ClosableChannel([summary_reply("ok")], close_raises=True)
        m = self.mgr(lambda t, s: ch)
        sid, text = m.spawn("explore", "x", wait=True)
        m.wait_all(10)
        self.assertEqual(text, "ok")
        self.assertTrue(os.path.exists(C.result_path(sid, self.runs)))

    def test_channel_without_close_is_fine(self):
        m = self.mgr(lambda t, s: FakeChannel([summary_reply("ok")]))
        _, text = m.spawn("explore", "x", wait=True)
        self.assertEqual(text, "ok")

    def test_factory_failure_is_a_failed_run(self):
        def boom(t, s):
            raise RuntimeError("no chrome")
        m = self.mgr(boom)
        sid, _ = m.spawn("explore", "x", wait=True)
        m.wait_all(10)
        self.assertTrue(os.path.exists(C.result_path(sid, self.runs)))


class FakeSlot:
    instances = []

    def __init__(self, src, profile, work_root, name, **kw):
        self.kw = kw
        self.closed = 0
        self.fail_start = False
        FakeSlot.instances.append(self)

    def start(self):
        if self.fail_start:
            raise RuntimeError("chrome did not start")
        return "http://127.0.0.1:9999"

    def close(self):
        self.closed += 1


class FakeCtl:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []
        self.closed = 0
        self.connected_to = None

        class P:
            url = "https://chat.example.invalid/c/123"
        self.page = P()

    def connect(self, timeout=30, target_url=None):
        self.connected_to = target_url
        return True

    def send_prompt_and_get_response(self, prompt, marker, auto_submit=True):
        self.prompts.append((prompt, marker))
        r = self.replies.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r(marker) if callable(r) else r

    def close(self):
        self.closed += 1


class TestWebChannel(unittest.TestCase):
    def setUp(self):
        FakeSlot.instances = []

    def channel(self, replies, **kw):
        self.ctl = FakeCtl(replies)
        return WebChannel("anyprovider", "/src", "Profile 1", "/work", "a1",
                          slot_factory=FakeSlot, controller_factory=lambda cdp, prov: self.ctl,
                          start_url="https://chat.example.invalid/new", **kw)

    def test_lazy_start(self):
        ch = self.channel(["hello"])
        self.assertEqual(FakeSlot.instances, [])
        ch.send("p")
        self.assertEqual(len(FakeSlot.instances), 1)

    def test_reply_returned_and_chat_url_recorded(self):
        ch = self.channel(["the reply"])
        self.assertEqual(ch.send("p"), "the reply")
        self.assertEqual(ch.chat_url, "https://chat.example.invalid/c/123")
        self.assertEqual(self.ctl.connected_to, "https://chat.example.invalid/new")

    def test_second_send_reuses_slot(self):
        ch = self.channel(["a", "b"])
        ch.send("1")
        ch.send("2")
        self.assertEqual(len(FakeSlot.instances), 1)

    def test_marker_stripped(self):
        ch = self.channel([lambda m: m + "\nbody"])
        self.assertEqual(ch.send("p"), "body")

    def test_rate_limit(self):
        ch = self.channel(["RATE_LIMIT_DETECTED", "RATE_LIMIT_DETECTED"], rate_wait=0)
        with self.assertRaises(RateLimited):
            ch.send("p")

    def test_paste_failure(self):
        ch = self.channel(["FAILED_TO_PASTE"])
        with self.assertRaises(ChannelError):
            ch.send("p")

    def test_empty_reply(self):
        for empty in ("", "   \n", None):
            ch = self.channel([empty])
            with self.assertRaises(ChannelError):
                ch.send("p")

    def test_controller_exception_becomes_channel_error(self):
        ch = self.channel([RuntimeError("page gone")])
        with self.assertRaises(ChannelError):
            ch.send("p")

    def test_start_failure_closes_slot(self):
        ch = WebChannel("anyprovider", "/src", "Profile 1", "/work", "a1",
                        slot_factory=FakeSlot, controller_factory=lambda c, p: FakeCtl([]),
                        start_url="x")
        orig_init = FakeSlot.__init__

        def init(self, *a, **k):
            orig_init(self, *a, **k)
            self.fail_start = True
        FakeSlot.__init__ = init
        try:
            with self.assertRaises(ChannelError):
                ch.send("p")
        finally:
            FakeSlot.__init__ = orig_init
        self.assertEqual(FakeSlot.instances[0].closed, 1)

    def test_close_closes_controller_and_slot_once_and_is_idempotent(self):
        ch = self.channel(["a"])
        ch.send("p")
        ch.close()
        ch.close()
        self.assertEqual(self.ctl.closed, 1)
        self.assertEqual(FakeSlot.instances[0].closed, 1)

    def test_send_after_close_refused(self):
        ch = self.channel(["a"])
        ch.close()
        with self.assertRaises(ChannelError):
            ch.send("p")

    def test_resume_url_used_as_start_target(self):
        ch = self.channel(["a"], resume_url="https://chat.example.invalid/c/old")
        ch.send("p")
        self.assertEqual(self.ctl.connected_to, "https://chat.example.invalid/c/old")


if __name__ == "__main__":
    unittest.main()
