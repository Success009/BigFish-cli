#!/usr/bin/env python3
# tests/test_cdp_stream.py - unit tests for cdp_stream.CdpStreamCapture
import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cdp_stream import CdpStreamCapture

GEN_URL = "https://claude.ai/api/organizations/o/chat_conversations/c/completion"


def b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class FakeSession:
    def __init__(self, buffered=None, fail_stream=False):
        self.handlers = {}
        self.sent = []
        self.buffered = buffered
        self.fail_stream = fail_stream
        self.detached = False

    def on(self, event, fn):
        self.handlers[event] = fn

    def send(self, method, params=None):
        self.sent.append(method)
        if method == "Network.streamResourceContent":
            if self.fail_stream:
                raise RuntimeError("boom")
            if self.buffered is not None:
                return {"bufferedData": b64(self.buffered)}
            return {}
        return {}

    def detach(self):
        self.detached = True

    def fire(self, event, params):
        self.handlers[event](params)


class FakeContext:
    def __init__(self, session):
        self.session = session

    def new_cdp_session(self, page):
        return self.session


class FakePage:
    def __init__(self, session):
        self.context = FakeContext(session)


def matcher(url):
    return "/chat_conversations/" in url and "/completion" in url


def make(buffered=None, with_status=False, fail_stream=False):
    session = FakeSession(buffered=buffered, fail_stream=fail_stream)
    got = []
    statuses = []
    cb = statuses.append if with_status else None
    cap = CdpStreamCapture(
        FakePage(session), matcher, lambda text, ok: got.append((text, ok)), cb
    )
    assert cap.attach() is True
    return session, cap, got, statuses


def response(session, rid, url=GEN_URL, status=200):
    session.fire(
        "Network.responseReceived",
        {"requestId": rid, "response": {"url": url, "status": status}},
    )


def data(session, rid, text):
    session.fire("Network.dataReceived", {"requestId": rid, "data": b64(text)})


class TestCdpStreamCapture(unittest.TestCase):
    def test_chunks_are_assembled_in_order(self):
        s, cap, got, _ = make()
        response(s, "1")
        data(s, "1", "hello ")
        data(s, "1", "world")
        s.fire("Network.loadingFinished", {"requestId": "1"})
        self.assertEqual(got, [("hello world", True)])

    def test_buffered_head_is_prepended(self):
        s, cap, got, _ = make(buffered="HEAD-")
        response(s, "1")
        data(s, "1", "tail")
        s.fire("Network.loadingFinished", {"requestId": "1"})
        self.assertEqual(got, [("HEAD-tail", True)])

    def test_failed_request_still_delivers_body(self):
        s, cap, got, _ = make()
        response(s, "1")
        data(s, "1", "partial but complete")
        s.fire("Network.loadingFailed", {"requestId": "1"})
        self.assertEqual(got, [("partial but complete", False)])

    def test_unrelated_url_is_ignored(self):
        s, cap, got, _ = make()
        response(s, "9", url="https://claude.ai/api/chat_messages/x")
        data(s, "9", "nope")
        s.fire("Network.loadingFinished", {"requestId": "9"})
        self.assertEqual(got, [])
        self.assertNotIn("Network.streamResourceContent", s.sent)

    def test_empty_stream_delivers_nothing(self):
        s, cap, got, _ = make()
        response(s, "1")
        s.fire("Network.loadingFinished", {"requestId": "1"})
        self.assertEqual(got, [])

    def test_multibyte_text_roundtrips(self):
        s, cap, got, _ = make()
        text = "caf" + chr(233) + " " + chr(0x4E2D)
        response(s, "1")
        data(s, "1", text)
        s.fire("Network.loadingFinished", {"requestId": "1"})
        self.assertEqual(got, [(text, True)])

    def test_two_streams_do_not_mix(self):
        s, cap, got, _ = make()
        response(s, "1")
        response(s, "2")
        data(s, "1", "A")
        data(s, "2", "B")
        s.fire("Network.loadingFinished", {"requestId": "2"})
        s.fire("Network.loadingFinished", {"requestId": "1"})
        self.assertEqual(sorted(got), [("A", True), ("B", True)])

    def test_status_callback_reports_error_status(self):
        s, cap, got, statuses = make(with_status=True)
        response(s, "1", status=429)
        self.assertEqual(statuses, [429])

    def test_error_status_body_still_delivered(self):
        s, cap, got, _ = make()
        response(s, "1", status=429)
        data(s, "1", "limit body")
        s.fire("Network.loadingFinished", {"requestId": "1"})
        self.assertEqual(got, [("limit body", True)])

    def test_stream_start_failure_does_not_raise(self):
        s, cap, got, _ = make(fail_stream=True)
        response(s, "1")
        data(s, "1", "x")
        s.fire("Network.loadingFinished", {"requestId": "1"})
        self.assertEqual(got, [("x", True)])

    def test_attach_skipped_without_context(self):
        class Bare:
            pass

        cap = CdpStreamCapture(Bare(), matcher, lambda t, o: None)
        self.assertFalse(cap.attach())

    def test_detach_closes_session(self):
        s, cap, got, _ = make()
        cap.detach()
        self.assertTrue(s.detached)
        self.assertIsNone(cap.session)


if __name__ == "__main__":
    unittest.main()
