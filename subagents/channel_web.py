"""subagents.channel_web - the real browser channel for one sub-agent (plan item A2).

A channel is anything with send(prompt) -> reply text. This one owns a private BrowserSlot
(own Chrome profile copy and debug port) and a controller connected to it.
The provider is a parameter; the start URL comes from the adapter registry.
Browser modules are imported lazily so importing this file loads no Playwright and no UI.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from subagents.browserpool import BrowserSlot, PoolError
from subagents.runner import ChannelError, RateLimited


def _default_controller(cdp_url: str, provider: str):
    try:
        import paths  # noqa: F401  (adds web/, core/, adapters path entries when run from the repo root)
    except Exception:
        pass
    from web.browser import WebAgentController
    return WebAgentController(cdp_url=cdp_url, provider=provider)


def _default_start_url(provider: str) -> str:
    try:
        import paths  # noqa: F401
    except Exception:
        pass
    import adapters
    return adapters.get_adapter(provider).default_url


import threading as _th
from collections import deque as _dq


def _dlog(tag, msg):
    try:
        import debuglog as _dl
        _dl.log(tag, msg)
    except Exception:
        pass


class _MinuteLimiter:
    """Shared by every Gemini sub in this process: at most `limit` sends per `window` seconds."""

    def __init__(self, limit=10, window=60.0):
        self.limit = limit
        self.window = window
        self._lock = _th.Lock()
        self._stamps = _dq()

    def wait(self):
        waited = 0.0
        while True:
            with self._lock:
                now = time.time()
                while self._stamps and now - self._stamps[0] >= self.window:
                    self._stamps.popleft()
                if len(self._stamps) < self.limit:
                    self._stamps.append(now)
                    return waited
                delay = self.window - (now - self._stamps[0]) + 0.2
            time.sleep(delay)
            waited += delay


_GEMINI_LIMITER = _MinuteLimiter()


class WebChannel:
    RELOAD_RETRIES = 2   # resend the same prompt after a page reload that dropped it
    RATE_WAIT = 65       # seconds to wait before the one retry after a rate limit

    def __init__(self, provider: str, src_dir: str, profile: str, work_root: str, name: str, *,
                 resume_url: Optional[str] = None,
                 slot_factory: Optional[Callable] = None,
                 controller_factory: Optional[Callable] = None,
                 start_url: Optional[str] = None,
                 port_taken=(), ready_timeout: float = 40.0,
                 on_turn: Optional[Callable] = None,
                 start_gate=None, start_pause: float = 0.0, on_rate_limit=None,
                 rate_wait: Optional[float] = None, limiter=None):
        self.provider = provider
        self._limiter = _GEMINI_LIMITER if limiter is None else (limiter or None)
        if rate_wait is not None:
            self.RATE_WAIT = rate_wait
        self._on_rate_limit = on_rate_limit   # on_rate_limit(profile, provider)
        self._start_gate = start_gate      # shared lock: one browser starts at a time
        self._start_pause = start_pause    # seconds to wait (gate held) after a successful start
        self.profile = profile
        self.chat_url = resume_url or ""
        self._src_dir = src_dir
        self._work_root = work_root
        self._name = name
        self._slot_factory = slot_factory or BrowserSlot
        self._controller_factory = controller_factory or _default_controller
        self._start_url = start_url
        self._port_taken = port_taken
        self._ready_timeout = ready_timeout
        self._on_turn = on_turn   # on_turn(profile, provider, prompt_tokens, response_tokens)
        self.opened_url = ""   # the page address right after connect (shows which chat was opened)
        self._slot = None
        self._ctl = None
        self._closed = False

    def _start(self) -> None:
        gate = self._start_gate
        if gate is None:
            self._start_inner()
            return
        with gate:
            self._start_inner()
            if self._start_pause > 0:
                time.sleep(self._start_pause)

    def _start_inner(self) -> None:
        url = self.chat_url or self._start_url or _default_start_url(self.provider)
        try:
            self._slot = self._slot_factory(self._src_dir, self.profile, self._work_root, self._name,
                                            start_url=url, port_taken=self._port_taken,
                                            ready_timeout=self._ready_timeout)
            cdp = self._slot.start()
            self._ctl = self._controller_factory(cdp, self.provider)
            self._ctl.connect(timeout=20, target_url=url)
            self._attach_limit_hook()
            try:
                self.opened_url = str(getattr(self._ctl.page, "url", "") or "")
            except Exception:
                self.opened_url = ""
        except Exception as e:
            self.close()
            raise ChannelError("browser start failed: %s: %s" % (type(e).__name__, e))

    GEN_URL_HINTS = ("generatecontent", "streamgenerate")

    def _attach_limit_hook(self) -> None:
        """Sub-only: record an HTTP error on a generation request so the limit signal can fire.

        The Gemini adapter never sets rate_status or live_payloads, so a quota 429 would be missed.
        This also saves the first real limit response (no headers) for later study. Never raises.
        """
        try:
            page = getattr(self._ctl, "page", None)
            if page is None or not hasattr(page, "on"):
                return
            ctl, provider = self._ctl, self.provider
            hints = self.GEN_URL_HINTS

            def handler(response):
                try:
                    if response.request.method != "POST":
                        return
                    url = response.url or ""
                    if not any(h in url.lower() for h in hints):
                        return
                    st = int(response.status)
                    if st < 400:
                        return
                    ctl.rate_status = st
                    body = ""
                    try:
                        body = response.text() or ""
                    except Exception:
                        body = ""
                    if body:
                        if not hasattr(ctl, "live_payloads"):
                            ctl.live_payloads = []
                        ctl.live_payloads.append(body)
                    try:
                        from cdp_stream import save_capture
                        save_capture(url.split("?")[0], st, {}, body, False,
                                     {"provider": provider, "source": "subagent_limit_hook"})
                    except Exception:
                        pass
                except Exception:
                    pass

            page.on("response", handler)
        except Exception:
            pass

    def send(self, prompt: str) -> str:
        if self._closed:
            raise ChannelError("channel is closed")
        if self._ctl is None:
            self._start()
        raw = None
        marker = ""
        for attempt in range(self.RELOAD_RETRIES + 1):
            marker = "[SYS_MARKER_%d]" % int(time.time() * 1000)
            if self.provider == "gemini" and self._limiter is not None:
                _w = self._limiter.wait()
                if _w > 0:
                    _dlog("SUB-LIMITER", "%s waited %.1fs before send" % (self._name, _w))
            try:
                raw = self._ctl.send_prompt_and_get_response(prompt, marker, auto_submit=True)
            except Exception as e:
                raise ChannelError("send failed: %s: %s" % (type(e).__name__, e))
            if raw != "PERMISSION_DENIED_RELOADED":
                break
        if raw == "PERMISSION_DENIED_RELOADED":
            raise ChannelError("session reloaded %d times without a reply" % (self.RELOAD_RETRIES + 1))
        try:
            self.chat_url = self._ctl.page.url or self.chat_url
        except Exception:
            pass
        if raw == "RATE_LIMIT_DETECTED":
            _body = ""
            try:
                _lp = getattr(self._ctl, "live_payloads", None) or []
                _body = str(_lp[-1])[:300] if _lp else ""
            except Exception:
                _body = ""
            _dlog("SUB-RATE-LIMIT", "%s first hit status=%s body=%s; waiting %ds then retrying once" % (
                self._name, getattr(self._ctl, "rate_status", None), _body, self.RATE_WAIT))
            time.sleep(self.RATE_WAIT)
            try:
                self._ctl.rate_status = None
                self._ctl.live_payloads = []
            except Exception:
                pass
            if self.provider == "gemini" and self._limiter is not None:
                self._limiter.wait()
            marker = "[SYS_MARKER_%d]" % int(time.time() * 1000)
            try:
                raw = self._ctl.send_prompt_and_get_response(prompt, marker, auto_submit=True)
            except Exception as e:
                raise ChannelError("send failed on retry: %s: %s" % (type(e).__name__, e))
            _dlog("SUB-RATE-LIMIT", "%s retry result: %s" % (self._name, "limited again" if raw == "RATE_LIMIT_DETECTED" else "ok"))
        if raw == "RATE_LIMIT_DETECTED":
            if self._on_rate_limit is not None:
                try:
                    self._on_rate_limit(self.profile, self.provider)
                except Exception:
                    pass
            raise RateLimited("%s rate limit (after one retry) body=%s" % (self.provider, _body))
        if raw == "FAILED_TO_PASTE":
            raise ChannelError("paste failed")
        if not raw or not str(raw).strip():
            raise ChannelError("empty reply")
        raw = str(raw)
        if raw.startswith(marker):
            raw = raw[len(marker):].lstrip("\n")
        self._report_turn(prompt, raw)
        return raw

    def _report_turn(self, prompt: str, reply: str) -> None:
        """Hand this turn's token use to the accounting hook. Never raises."""
        if self._on_turn is None:
            return
        try:
            sniffed = None
            getter = getattr(self._ctl, "get_last_prompt_tokens", None)
            if callable(getter):
                sniffed = getter()
            p_tok = sniffed if (isinstance(sniffed, int) and sniffed > 0) else int(len(prompt) / 3.8)
            r_tok = int(len(reply) / 3.8)
            self._on_turn(self.profile, self.provider, p_tok, r_tok)
        except Exception:
            pass

    def close(self) -> None:
        self._closed = True
        ctl, self._ctl = self._ctl, None
        if ctl is not None:
            try:
                ctl.close()
            except Exception:
                pass
        slot, self._slot = self._slot, None
        if slot is not None:
            try:
                slot.close()
            except Exception:
                pass
