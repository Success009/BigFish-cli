import os
import sys
import tempfile
import unittest
from unittest import mock

from subagents.channel_web import WebChannel
from subagents.tests.test_manager_close import FakeSlot, FakeCtl


class FakePage:
    def __init__(self):
        self.handlers = []

    def on(self, event, fn):
        if event == "response":
            self.handlers.append(fn)


class PageCtl(FakeCtl):
    """FakeCtl with a page, like the real controller."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.page = FakePage()


class Req:
    def __init__(self, method="POST"):
        self.method = method


class Resp:
    def __init__(self, url, status, body="", method="POST", text_raises=False):
        self.url = url
        self.status = status
        self.request = Req(method)
        self._body = body
        self._raises = text_raises

    def text(self):
        if self._raises:
            raise RuntimeError("no data")
        return self._body


GEN = "https://alkalimakersuite-pa.clients6.google.com/$rpc/x/GenerateContent"
QUOTA = '{"error":{"code":429,"message":"You exceeded your current quota","status":"RESOURCE_EXHAUSTED"}}'


class TestLimitHook(unittest.TestCase):
    def setUp(self):
        self.cap = tempfile.mkdtemp()
        # cdp_stream is imported lazily by the hook; make sure it is importable and capture goes to temp
        try:
            import paths  # noqa: F401
        except Exception:
            pass
        import cdp_stream
        self._old = cdp_stream.CAPTURE_DIR
        cdp_stream.CAPTURE_DIR = self.cap
        self.cdp_stream = cdp_stream

    def tearDown(self):
        self.cdp_stream.CAPTURE_DIR = self._old

    def channel(self, ctl_cls=PageCtl):
        ctl_holder = {}

        def cf(cdp, provider):
            c = ctl_cls(["ok"]) if ctl_cls is not None else None
            ctl_holder["c"] = c
            return c

        ch = WebChannel("gemini", "/src", "Profile 1", tempfile.mkdtemp(), "t1",
                        slot_factory=FakeSlot, controller_factory=cf, limiter=False)
        ch._start()
        return ch, ctl_holder["c"]

    def fire(self, ctl, resp):
        for h in ctl.page.handlers:
            h(resp)

    def captures(self):
        return [f for f in os.listdir(self.cap) if f.endswith(".json")]

    def test_hook_attached_once_after_connect(self):
        ch, ctl = self.channel()
        self.assertEqual(len(ctl.page.handlers), 1)

    def test_429_sets_status_body_and_capture(self):
        ch, ctl = self.channel()
        self.fire(ctl, Resp(GEN, 429, QUOTA))
        self.assertEqual(ctl.rate_status, 429)
        self.assertEqual(ctl.live_payloads, [QUOTA])
        self.assertEqual(len(self.captures()), 1)

    def test_200_ignored(self):
        ch, ctl = self.channel()
        self.fire(ctl, Resp(GEN, 200, "fine"))
        self.assertFalse(hasattr(ctl, "rate_status"))
        self.assertEqual(self.captures(), [])

    def test_get_request_ignored(self):
        ch, ctl = self.channel()
        self.fire(ctl, Resp(GEN, 429, QUOTA, method="GET"))
        self.assertFalse(hasattr(ctl, "rate_status"))

    def test_non_generation_url_ignored(self):
        ch, ctl = self.channel()
        self.fire(ctl, Resp("https://aistudio.google.com/api/other", 429, QUOTA))
        self.assertFalse(hasattr(ctl, "rate_status"))

    def test_unreadable_body_still_sets_status(self):
        ch, ctl = self.channel()
        self.fire(ctl, Resp(GEN, 429, text_raises=True))
        self.assertEqual(ctl.rate_status, 429)
        self.assertFalse(getattr(ctl, "live_payloads", []))

    def test_capture_failure_never_raises(self):
        ch, ctl = self.channel()
        with mock.patch.object(self.cdp_stream, "save_capture", side_effect=RuntimeError("disk")):
            self.fire(ctl, Resp(GEN, 429, QUOTA))
        self.assertEqual(ctl.rate_status, 429)

    def test_query_string_not_stored(self):
        ch, ctl = self.channel()
        self.fire(ctl, Resp(GEN + "?key=SECRET", 429, QUOTA))
        import json
        with open(os.path.join(self.cap, self.captures()[0]), encoding="utf-8") as fh:
            d = json.load(fh)
        self.assertNotIn("SECRET", d["url"])
        self.assertEqual(d["headers"], {})

    def test_page_without_on_is_skipped(self):
        # FakeCtl.page has only a url attribute and no on(): the hook must skip it and send must still work
        ch, ctl = self.channel(ctl_cls=FakeCtl)
        self.assertFalse(hasattr(ctl.page, "on"))
        self.assertEqual(ch.send("hi"), "ok")

    def test_controller_without_page_attribute_is_skipped(self):
        class NoPage(FakeCtl):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                del self.page
        ch, ctl = self.channel(ctl_cls=NoPage)
        self.assertFalse(hasattr(ctl, "page"))
        self.assertEqual(ch.send("hi"), "ok")

    def test_hook_output_drives_real_limit_signal(self):
        # End to end offline: hook output -> monitor_active_response -> RATE_LIMIT_DETECTED
        from tests.test_limit_signal_gemini import make_controller, run, FakeAdapter
        ch, ctl = self.channel()
        self.fire(ctl, Resp(GEN, 429, QUOTA))
        real = make_controller(FakeAdapter("gemini"), list(ctl.live_payloads), ctl.rate_status)
        self.assertEqual(run(real), "RATE_LIMIT_DETECTED")


if __name__ == "__main__":
    unittest.main()
