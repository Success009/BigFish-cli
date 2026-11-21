# cdp_stream.py - raw network capture of a streaming completion via CDP.
#
# Why: Playwright's Response.text() asks Chrome for the body AFTER the request
# is over. For SSE streams Chrome often has already discarded it ("No data
# found for resource"), and when the request is flagged failed we never get
# it at all. Network.streamResourceContent makes Chrome push every chunk to us
# while it arrives, so the full text is in our hands before teardown.
#
# Persistent capture: streams that look like a rate/usage limit (HTTP >= 400, or
# limit wording inside the body) are written to CAPTURE_DIR as JSON. The debug
# log is wiped on every startup (and a transfer restarts the CLI), so the log
# alone can never preserve the real limit packet.
import base64
import json
import os
import re
import threading
import time

import debuglog as dlog

CAPTURE_DIR = os.path.expanduser("~/gemini_local_agent/rate_limit_captures")
MAX_CAPTURE_FILES = 40
MAX_BODY_CHARS = 200000

LIMIT_BODY_RE = re.compile(
    r"rate[_ ]?limit|exceeded[_ ]?limit|message[_ ]?limit|usage[_ ]?limit|"
    r"too many requests|out of (free )?messages|reached your .{0,30}limit|"
    r"resets? at|quota",
    re.IGNORECASE,
)


def looks_like_limit(status, text):
    """True for HTTP >= 400, or a body containing limit wording."""
    try:
        if status is not None and int(status) >= 400:
            return True
    except Exception:
        pass
    # A real limit notice is small; long replies that merely discuss limits are not.
    return bool(text and len(text) < 3000 and LIMIT_BODY_RE.search(text))


def save_capture(url, status, headers, text, ok, meta=None, capture_dir=None):
    """Writes one capture JSON file; prunes old ones. Returns the path or None."""
    try:
        d = capture_dir or CAPTURE_DIR
        os.makedirs(d, exist_ok=True)
        now = time.time()
        name = time.strftime("%Y%m%d_%H%M%S", time.localtime(now)) + "_%03d.json" % int((now % 1) * 1000)
        path = os.path.join(d, name)
        payload = {
            "captured_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "url": url,
            "status": status,
            "ok": ok,
            "headers": headers or {},
            "body_chars": len(text or ""),
            "body_truncated": len(text or "") > MAX_BODY_CHARS,
            "body": (text or "")[:MAX_BODY_CHARS],
            "meta": meta or {},
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        files = sorted(x for x in os.listdir(d) if x.endswith(".json"))
        for old in files[:-MAX_CAPTURE_FILES]:
            try:
                os.remove(os.path.join(d, old))
            except Exception:
                pass
        return path
    except Exception as e:
        dlog.log("CDP", "save_capture failed: " + str(e))
        return None


class CdpStreamCapture:
    def __init__(self, page, url_matcher, on_payload, on_status=None, meta=None):
        """
        page        - Playwright page (needs page.context.new_cdp_session)
        url_matcher - callable(url) -> bool, True for the generation stream
        on_payload  - callable(text, ok) invoked once per finished stream
        on_status   - optional callable(status) on every matched response
        meta        - optional callable() -> dict (provider, profile...) stored in captures
        """
        self.page = page
        self.url_matcher = url_matcher
        self.on_payload = on_payload
        self.on_status = on_status
        self.meta = meta
        self.session = None
        self._lock = threading.Lock()
        self._active = {}   # requestId -> list of decoded text chunks
        self._status = {}   # requestId -> http status
        self._info = {}     # requestId -> {"url":..., "headers":...}
        self.last_capture_path = None

    def attach(self):
        ctx = getattr(self.page, "context", None)
        if ctx is None or not hasattr(ctx, "new_cdp_session"):
            return False
        self.session = ctx.new_cdp_session(self.page)
        self.session.on("Network.responseReceived", self._on_response)
        self.session.on("Network.dataReceived", self._on_data)
        self.session.on("Network.loadingFinished", self._on_finished)
        self.session.on("Network.loadingFailed", self._on_failed)
        self.session.send("Network.enable")
        dlog.log("CDP", "stream capture attached")
        return True

    def detach(self):
        try:
            if self.session is not None:
                self.session.detach()
        except Exception:
            pass
        self.session = None

    def _on_response(self, params):
        try:
            resp = params.get("response", {})
            if not self.url_matcher(resp.get("url", "")):
                return
            rid = params.get("requestId")
            if not rid:
                return
            if self.on_status is not None:
                try:
                    self.on_status(resp.get("status"))
                except Exception:
                    pass
            with self._lock:
                self._active[rid] = []
                self._status[rid] = resp.get("status")
                self._info[rid] = {"url": resp.get("url", ""), "headers": resp.get("headers", {})}
            try:
                res = self.session.send("Network.streamResourceContent", {"requestId": rid})
                buffered = (res or {}).get("bufferedData") if isinstance(res, dict) else None
                if buffered:
                    head = base64.b64decode(buffered).decode("utf-8", errors="replace")
                    with self._lock:
                        bucket = self._active.get(rid)
                        if bucket is not None:
                            bucket.insert(0, head)
                    dlog.log("CDP", "buffered head " + str(len(head)) + " chars")
                dlog.log("CDP", "streaming started")
            except Exception as e:
                dlog.log("CDP", "streamResourceContent failed: " + str(e))
        except Exception as e:
            dlog.log("CDP", "responseReceived error: " + str(e))

    def _on_data(self, params):
        try:
            rid = params.get("requestId")
            raw = params.get("data")
            if not raw:
                return
            with self._lock:
                bucket = self._active.get(rid)
                if bucket is None:
                    return
                bucket.append(base64.b64decode(raw).decode("utf-8", errors="replace"))
        except Exception as e:
            dlog.log("CDP", "dataReceived error: " + str(e))

    def _finish(self, rid, ok):
        with self._lock:
            chunks = self._active.pop(rid, None)
            st = self._status.pop(rid, None)
            info = self._info.pop(rid, {})
        if chunks is None:
            return
        text = "".join(chunks)
        try:
            if st is not None and int(st) >= 400:
                dlog.log("CDP", "error body status=" + str(st) + " head=" + repr(text[:300]))
        except Exception:
            pass
        dlog.log("CDP", "stream done ok=" + str(ok) + " chars=" + str(len(text)))

        if looks_like_limit(st, text):
            meta = {}
            if self.meta is not None:
                try:
                    meta = self.meta() or {}
                except Exception:
                    meta = {}
            path = save_capture(info.get("url", ""), st, info.get("headers", {}), text, ok, meta)
            if path:
                self.last_capture_path = path
                dlog.log("CDP", "limit-like stream saved to " + path)

        if text:
            try:
                self.on_payload(text, ok)
            except Exception as e:
                dlog.log("CDP", "on_payload error: " + str(e))

    def _on_finished(self, params):
        self._finish(params.get("requestId"), True)

    def _on_failed(self, params):
        self._finish(params.get("requestId"), False)
