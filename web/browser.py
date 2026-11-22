# browser.py - Multi-Agent Pluggable Web Browser Bridge
import os
import re
import json
import time
import subprocess
import pyperclip
from playwright.sync_api import sync_playwright
import debuglog as dlog
import turnlog
import adapters
from adapters.gemini import (
    extract_all_json_groups,
    recursive_extract_makersuite_text,
    extract_text_from_any_payload,
    sanitize_and_extract_strings,
    parse_gemini_stream_payload,
    find_genuine_response_start,
    clean_response_payload,
    is_pure_thought_text
)

class BrowserClosedError(Exception): pass

def get_terminal_window_id():
    """Gets the X11 window ID of the active terminal session."""
    try:
        if os.name != 'nt':
            return subprocess.check_output("xdotool getactivewindow", shell=True, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        pass
    return None

def restore_terminal_focus(win_id):
    """Restores OS focus back to the recorded terminal window ID."""
    try:
        if os.name != 'nt' and win_id:
            subprocess.run(f"xdotool windowactivate {win_id}", shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


class WebAgentController:
    """
    Pluggable CDP Controller for Web AI Applications (Google AI Studio, DeepSeek).
    Centralizes browser lifecycle, remote debugging connection, and tab management,
    while delegating site-specific DOM parsing, packet streaming, and error detection to adapters.
    """
    def __init__(self, cdp_url="http://127.0.0.1:9222", provider="gemini"):
        self.cdp_url = cdp_url
        self.provider = provider
        self.adapter = adapters.get_adapter(provider)
        self.playwright = sync_playwright().start()
        self.browser = None
        self.page = None
        self.captured_responses = []
        self.stream_finished = False
        self.last_sniffed_prompt_tokens = None
        self.initial_model_turns_count = 0
        # Opt-in auto profile switch on rate notice. Default OFF:
        # keep polling the current turn rather than aborting it.
        self.auto_transfer_on_rate_limit = True

    def switch_adapter(self, provider_name):
        """Switches the active web adapter on the fly."""
        self.provider = provider_name
        self.adapter = adapters.get_adapter(provider_name)
        dlog.log("ADAPTER", f"switched to {self.adapter.display_name}")

    def is_connected(self):
        """Quickly checks if controller has an active, open browser and page context."""
        try:
            return bool(
                hasattr(self, 'page') and self.page and not self.page.is_closed()
                and self.browser and self.browser.is_connected()
            )
        except Exception:
            return False

    def bring_to_front(self):
        """Centralized window focus logic: brings page to front strictly when pasting/submitting payload."""
        if getattr(self, "no_focus", False):
            return   # background search browsers must never take focus
        if os.environ.get("BF_RAISE_BROWSER", "") != "1":
            return   # default: never raise the Chrome window
        try:
            if self.page and not self.page.is_closed():
                self.page.bring_to_front()
        except Exception:
            pass

    def connect(self, timeout=30, target_url=None):
        try:
            target_prompt_id = self.adapter.extract_prompt_id(target_url) if target_url else None

            # Fast-path: return immediately if page is already connected and active
            if self.is_connected():
                try:
                    self.register_network_listener()   # no-op when this page already has it
                except Exception as _le:
                    dlog.log("LISTENER", f"fast-path register failed: {_le}")
                if target_url:
                    # If current page is already on this exact URL or prompt ID
                    if target_url in self.page.url or (target_prompt_id and target_prompt_id in self.page.url):
                        return True
                    # Check if another open page has target_url
                    if self.browser:
                        for context in self.browser.contexts:
                            for pg in context.pages:
                                if target_url in pg.url or (target_prompt_id and target_prompt_id in pg.url):
                                    self.page = pg
                                    self.register_network_listener()
                                    try: self.bring_to_front()
                                    except Exception: pass
                                    return True
                    # If no open page has it, navigate the current active page to target_url
                    try:
                        print(f"[Browser] Navigating tab to session URL: {target_url} ...")
                        self.page.goto(target_url, wait_until="domcontentloaded", timeout=40000)
                        self.wait_for_chat_ready(timeout=20000)
                    except Exception as e:
                        print(f"[Browser] Tab navigation warning: {e}")
                return True

            start_t = time.time()
            if not self.browser or not self.browser.is_connected():
                while time.time() - start_t < timeout:
                    try:
                        self.browser = self.playwright.chromium.connect_over_cdp(self.cdp_url, timeout=3000)
                        break
                    except Exception:
                        time.sleep(0.2)
                if not self.browser or not self.browser.is_connected():
                    raise BrowserClosedError("Could not connect to Chrome debugging port.")

            # Give Chrome up to 3s to expose an already-opened tab matching target_url
            find_tab_deadline = time.time() + min(timeout, 3)
            while time.time() < find_tab_deadline:
                # Priority 1: Find a tab already on target_url or matching prompt id
                if target_url:
                    for context in self.browser.contexts:
                        for pg in context.pages:
                            if target_url in pg.url or (target_prompt_id and target_prompt_id in pg.url):
                                self.page = pg
                                self.register_network_listener()
                                try: self.bring_to_front()
                                except Exception: pass
                                return True
                time.sleep(0.2)

            # Priority 2: If no tab specifically matched target_url, find any tab matching adapter domain
            for context in self.browser.contexts:
                for pg in context.pages:
                    if self.adapter.is_matching_url(pg.url):
                        self.page = pg
                        self.register_network_listener()
                        if target_url and not (target_url in self.page.url or (target_prompt_id and target_prompt_id in self.page.url)):
                            try:
                                print(f"[Browser] Navigating tab to session URL: {target_url} ...")
                                self.page.goto(target_url, wait_until="domcontentloaded", timeout=40000)
                                self.wait_for_chat_ready(timeout=20000)
                            except Exception as e:
                                print(f"[Browser] Tab navigation warning: {e}")
                        return True

            # Priority 3: If no matching tab is open at all, automatically navigate the first tab or open a new one
            dest_url = target_url or self.adapter.default_url
            if self.browser.contexts:
                ctx = self.browser.contexts[0]
                target_pg = ctx.pages[0] if ctx.pages else ctx.new_page()
                try:
                    target_pg.goto(dest_url, wait_until="domcontentloaded", timeout=40000)
                    self.page = target_pg
                    self.register_network_listener()
                    if target_url:
                        self.wait_for_chat_ready(timeout=20000)
                    return True
                except Exception:
                    pass
            raise BrowserClosedError(f"Could not find or open the {self.adapter.display_name} tab.")
        except Exception as e:
            raise BrowserClosedError(f"Connection failed: {e}")

    def get_chat_box(self):
        """Locates primary prompt input textarea using active adapter."""
        return self.adapter.get_chat_box(self.page)

    def scroll_chat_to_bottom(self):
        self.adapter.scroll_chat_to_bottom(self.page)

    def wait_for_chat_ready(self, timeout=60000):
        if not hasattr(self, 'page') or not self.page or self.page.is_closed():
            self.connect()
        return self.adapter.wait_for_chat_ready(self.page, timeout=timeout)

    def register_network_listener(self):
        """Attaches response sniffers strictly targeting generation streams.
        Re-registers when adapter changes (e.g., Gemini -> DeepSeek switch)."""
        registered_for = getattr(self, "_network_listener_registered_for", None)
        registered_page = getattr(self, "_network_listener_page", None)
        if registered_for != self.adapter.name or registered_page is not self.page:
            # New adapter OR a different page object: this page has no listener yet
            self.captured_responses = []
            self.stream_finished = False
            self.adapter.register_stream_listener(self.page, self)
            self._network_listener_registered_for = self.adapter.name
            self._network_listener_page = self.page
            try:
                _pu = (self.page.url or "")[:70]
            except Exception:
                _pu = "?"
            dlog.log("LISTENER", f"registered stream listener for {self.adapter.name} page={_pu}")

    def configure_tools_for_prompt(self, enable_url_context=False):
        pass

    def load_chat(self, url):
        # Auto-switch adapter if URL belongs to a different provider
        detected = adapters.detect_adapter_by_url(url)
        if detected and detected.name != self.adapter.name:
            self.switch_adapter(detected.name)
            
        self.connect(target_url=url)
        try:
            target_prompt_id = self.adapter.extract_prompt_id(url)
            if hasattr(self, 'page') and self.page and not self.page.is_closed():
                # If page is already on this exact prompt URL, don't re-navigate and wipe DOM!
                if self.page.url == url or (url and url in self.page.url) or (target_prompt_id and target_prompt_id in self.page.url):
                    if self.wait_for_chat_ready(timeout=5000):
                        return
            print(f"[Browser] Loading chat URL: {url} ...")
            self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            print(f"[Browser] Waiting for {self.adapter.display_name} session and chat interface to be ready...")
            if not self.wait_for_chat_ready(timeout=60000):
                print("[Browser] Warning: Chat box not ready after 60s, proceeding anyway...")
        except Exception as e:
            print(f"[Browser] Failed to load chat URL: {e}")

    def start_new_chat(self):
        self.connect()
        try:
            if hasattr(self, 'page') and self.page and not self.page.is_closed():
                if hasattr(self.adapter, 'start_new_chat'):
                    if self.adapter.start_new_chat(self.page):
                        self.wait_for_chat_ready(timeout=15000)
                        return
                target_url = self.adapter.default_url
                self.page.goto(target_url, wait_until="domcontentloaded", timeout=40000)
                self.wait_for_chat_ready(timeout=20000)
        except Exception as e:
            dlog.log("BROWSER", f"start_new_chat warning: {e}")

    def get_current_url(self):
        try:
            if hasattr(self, 'page') and self.page and not self.page.is_closed():
                return self.page.url
            self.connect()
            return self.page.url if self.page else None
        except Exception:
            return None

    def get_current_profile(self, profile_dir=None):
        try:
            if not hasattr(self, 'page') or not self.page or self.page.is_closed():
                self.connect()
            ident = self.adapter.get_current_profile_identity(self.page)
            if ident and ident not in ("Unknown Profile", "DeepSeek User"):
                return ident
        except Exception:
            pass
        if profile_dir:
            try:
                import tokens
                res_email = tokens.get_profile_email_from_local_state(profile_dir)
                if res_email:
                    return res_email
            except Exception:
                pass
        return "Unknown Profile"

    def paste_from_clipboard(self):
        self.connect()
        try:
            self.bring_to_front()
            chat_box = self.get_chat_box()
            chat_box.click(force=True)
            time.sleep(0.2)
            self.page.keyboard.press("Control+v")
        except Exception as e:
            print(f"[Browser] Paste shortcut failed: {e}")

    def is_page_generating(self):
        try:
            return self.adapter.is_generating(self.page)
        except Exception:
            return False

    def stop_generation(self):
        try:
            return self.adapter.stop_generation(self.page)
        except Exception:
            return False

    def paste_prompt_to_chat(self, base_prompt, turn_marker):
        """Prepares chat box and delivers prompt payload using the active adapter."""
        self.connect()
        try:
            dlog.log("PASTE", f"marker={turn_marker[:30]}")
            
            # Ensure chat input is mounted and ready
            if not self.wait_for_chat_ready(timeout=35000):
                dlog.log("PASTE", "FAIL: chat_box not ready")
                print("\n" + "="*50)
                print(f"🚨 {self.adapter.display_name.upper()} ACTION REQUIRED 🚨")
                print("Could not find the chat box! Please check Chrome, sign in, and close any popups.")
                print("="*50 + "\n")
                return False
            
            # Halt any lingering generation from prior turns
            if self.is_page_generating():
                dlog.log("PASTE", "halting prior generation")
                self.stop_generation()
                for _ in range(15):
                    time.sleep(0.2)
                    if not self.is_page_generating():
                        break

            # Clean transaction buffers for the active turn
            self._rate_notice_logged = False
            if hasattr(self, "captured_responses"):
                self.captured_responses.clear()
            self.live_payloads = []
            self.rate_status = None
            self.cdp_captured = False
            self.stream_finished = False
            self.last_sniffed_prompt_tokens = None
            
            full_payload = f"{base_prompt}\n\n{turn_marker}"
            ok = self.adapter.paste_prompt(self.page, full_payload, turn_marker)
            pending = getattr(self, "pending_attachments", None)
            if ok and pending:
                self.attach_files(pending)
                self.pending_attachments = []
            return ok
        except Exception as e:
            dlog.log("PASTE", f"error: {e}")
            return False

    def attach_files(self, paths):
        """Attaches local files through the page's file input (provider-independent)."""
        paths = [p for p in paths if os.path.isfile(p)]
        if not paths:
            return True
        try:
            inputs = self.page.query_selector_all('input[type="file"]')
        except Exception as e:
            inputs = []
            dlog.log("ATTACH", f"query failed: {e}")
        for el in inputs:
            try:
                el.set_input_files(paths)
                time.sleep(1.5)
                dlog.log("ATTACH", f"attached {len(paths)} file(s) together")
                return True
            except Exception:
                continue
        done = 0
        for p in paths:
            for el in inputs:
                try:
                    el.set_input_files(p)
                    time.sleep(1.0)
                    done += 1
                    break
                except Exception:
                    continue
        if done:
            dlog.log("ATTACH", f"attached {done}/{len(paths)} file(s) one by one")
            return done == len(paths)
        print("[Browser] Could not attach files: no file input accepted them. Sending text only.")
        dlog.log("ATTACH", "FAIL: no file input accepted files")
        return False

    def trigger_submit_if_needed(self):
        """Submits prompt via active adapter if not already generating."""
        self.connect()
        if self.is_page_generating():
            return True
        try:
            return self.adapter.trigger_submit(self.page)
        except Exception as e:
            dlog.log("SUBMIT", f"error: {e}")
            return False

    def _confirm_rate_limit(self):
        # True only when the out-of-messages notice is verified, never on one DOM hit.
        if not getattr(self, "auto_transfer_on_rate_limit", False):
            return False
        if getattr(self.adapter, "name", "") == "deepseek":
            return False
        try:
            if self.adapter.check_page_errors(self.page) != "RATE_LIMIT" or self.is_page_generating():
                return False
        except Exception:
            return False
        status = getattr(self, "rate_status", None)
        # Hard network signal (HTTP 429) needs only a brief confirmation; a
        # banner-only notice gets a short verification window, not 8-20s.
        grace = 1.0 if status == 429 else 4.0
        dlog.log("RATE", "notice seen; verifying for " + str(grace) + "s (http status " + str(status) + ")")
        t0 = time.time()
        while time.time() - t0 < grace:
            time.sleep(1.0)
            try:
                if self.adapter.check_page_errors(self.page) != "RATE_LIMIT" or self.is_page_generating():
                    dlog.log("RATE", "notice vanished; not confirmed")
                    return False
            except Exception:
                return False
        dlog.log("RATE", "notice confirmed")
        return True

    UI_FALLBACK_LOG = os.path.expanduser("~/gemini_local_agent/ui_fallback.log")

    def _log_ui_fallback(self, turn_marker, ui_text):
        """Detailed record of a page-scrape turn, in its own file, for debugging capture."""
        try:
            try:
                page_url = (self.page.url or "")[:120]
            except Exception:
                page_url = "?"
            recent = []
            try:
                with open(dlog.path(), "r", encoding="utf-8", errors="replace") as f:
                    recent = f.read().splitlines()[-15:]
            except Exception:
                pass
            txt = ui_text or ""
            rec = [
                "=== " + time.strftime("%Y-%m-%d %H:%M:%S") + " UI FALLBACK ===",
                "turn=" + str(turn_marker)[:40],
                "provider=" + str(getattr(getattr(self, "adapter", None), "name", "")),
                "profile=" + str(self._profile_tag()),
                "page=" + page_url,
                "reason=" + str(getattr(self, "_last_ui_reason", "") or "?"),
                "stream_finished=" + str(getattr(self, "stream_finished", "?")),
                "cdp_captured=" + str(getattr(self, "cdp_captured", "?")),
                "n_live=" + str(len(getattr(self, "live_payloads", []) or [])),
                "n_captured_responses=" + str(len(getattr(self, "captured_responses", []) or [])),
                "http_status=" + str(getattr(self, "rate_status", None)),
                "listener_adapter=" + str(getattr(self, "_network_listener_registered_for", None)),
                "listener_on_this_page=" + str(getattr(self, "_network_listener_page", None) is self.page),
                "ui_text_len=" + str(len(txt)),
                "ui_head=" + repr(txt[:200]),
                "ui_tail=" + repr(txt[-200:]),
                "recent_debug_log:",
            ] + ["  " + x[:200] for x in recent]
            path = self.UI_FALLBACK_LOG
            os.makedirs(os.path.dirname(path), exist_ok=True)
            try:
                if os.path.getsize(path) > 300000:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        data = f.read()
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(data[-150000:])
            except OSError:
                pass
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n".join(rec) + "\n\n")
            dlog.log("UIFALLBACK", "details saved to " + path)
        except Exception:
            pass

    def _profile_tag(self):
        """Chrome profile folder running on the debug port, cached for 60 seconds."""
        try:
            now = time.time()
            cached = getattr(self, "_profile_tag_cache", None)
            if cached and now - cached[0] < 60:
                return cached[1]
            from profiles import get_running_chrome_profile_dir
            import profile_ids
            folder = get_running_chrome_profile_dir() or ""
            if folder:
                lab = profile_ids.label(folder)
                prof = lab if lab == folder else lab.replace(" ", "") + "(" + folder.replace(" ", "") + ")"
            else:
                prof = "?"
            self._profile_tag_cache = (now, prof)
            return prof
        except Exception:
            return "?"

    CAPTURE_HISTORY_LOG = os.path.expanduser("~/gemini_local_agent/capture_history.log")
    CAPTURE_HISTORY_MAX = 500

    def _log_capture_compact(self, source, prov, prof, cdp, n_live, reason=""):
        """One short line per turn, kept across restarts, trimmed to the last 500 lines."""
        try:
            line = " ".join([time.strftime("%Y-%m-%d %H:%M:%S"), "src=" + str(source), "prov=" + str(prov),
                             "prof=" + str(prof), "cdp=" + str(cdp), "live=" + str(n_live)])
            if reason:
                line += " reason=" + str(reason)[:40]
            path = self.CAPTURE_HISTORY_LOG
            os.makedirs(os.path.dirname(path), exist_ok=True)
            lines = []
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.read().splitlines()
            except OSError:
                pass
            lines.append(line)
            lines = lines[-self.CAPTURE_HISTORY_MAX:]
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception:
            pass

    def _tl(self, turn_marker, **kw):
        try:
            prof = self._profile_tag()
            prov = str(getattr(getattr(self, "adapter", None), "name", ""))
            kw.setdefault("profile", prof)
            if kw.get("source") in ("ui_pending", "none"):
                self._last_ui_reason = str(kw.get("reason", ""))
            dlog.log("CAPTURE", "source=" + str(kw.get("source", "?")) + " provider=" + prov + " profile=" + str(prof)
                     + " cdp=" + str(kw.get("cdp", "?")) + " n_live=" + str(kw.get("n_live", "?")))
            self._log_capture_compact(kw.get("source", "?"), prov, prof, kw.get("cdp", "?"), kw.get("n_live", "?"), kw.get("reason", ""))
        except Exception:
            pass
        try:
            turnlog.record(turn=str(turn_marker)[:40], provider=str(getattr(getattr(self, "adapter", None), "name", "")), **kw)
        except Exception:
            pass

    def monitor_active_response(self, turn_marker):
        """Monitors network stream and UI fallback to capture response.
        
        SSE streams (DeepSeek, etc): resp.text() BLOCKS until request ends.
        So during generation we poll the UI for progress, then read network payload after done.
        """
        self.connect()
        import sys
        try:
            dlog.log("MONITOR", f"waiting for prompt in {self.adapter.display_name}")
            
            prompt_registered = False
            for _ in range(25):
                time.sleep(0.2)
                if self.is_page_generating():
                    prompt_registered = True
                    break

            if not prompt_registered:
                # Retry submit trigger if generation hasn't started
                dlog.log("SEND", "prompt not registered, retrying submit")
                try:
                    self.adapter.trigger_submit(self.page)
                    time.sleep(0.5)
                    if self.is_page_generating():
                        prompt_registered = True
                except Exception:
                    pass

            dlog.log("SEND", f"prompt_registered={prompt_registered}")

            # ----------------------------------------------------
            # 1. WAIT FOR GENERATION TO FINISH (In-place dynamic spinner)
            # ----------------------------------------------------
            has_seen_busy = prompt_registered
            start_poll_time = time.time()
            final_text = ""
            
            _poll_interval = float(getattr(self.adapter, "poll_interval", 0.5) or 0.5)
            while (time.time() - start_poll_time) < 240:
                time.sleep(_poll_interval)
                
                try:
                    err_status = self.adapter.check_page_errors(self.page)
                    if err_status == "RATE_LIMIT":
                        if getattr(self.adapter, 'name', '') == "deepseek":
                            pass  # Automated transfer voided for DeepSeek
                        elif not getattr(self, "auto_transfer_on_rate_limit", False):
                            # Auto-transfer is OFF: a rate notice (often a false
                            # positive) must never abort the turn or switch
                            # profiles. Keep polling; the 240s cap still applies.
                            if not getattr(self, "_rate_notice_logged", False):
                                dlog.log("RATE", "notice observed; auto-transfer OFF - continuing to wait")
                                self._rate_notice_logged = True
                            pass
                        else:
                            if not getattr(self, "_rate_notice_logged", False):
                                dlog.log("RATE", "notice observed; awaiting confirmation")
                                self._rate_notice_logged = True
                    elif err_status == "PERMISSION_DENIED":
                        pass
                        sys.stdout.flush()
                        print(f"\n[Browser] 🚨 {self.adapter.display_name} Session Error detected! Initiating session reload...")
                        dlog.log("RELOAD", "dynamic reload for permission state")
                        self.page.reload(wait_until="domcontentloaded", timeout=60000)
                        self.wait_for_chat_ready(timeout=60000)
                        return "PERMISSION_DENIED_RELOADED"
                except Exception:
                    pass
                
                is_busy = self.is_page_generating()
                elapsed = time.time() - start_poll_time

                # Progress is rendered by the caller's Rich spinner (main.py).
                # Do NOT write raw \r status lines here: they collide with Rich's
                # live display and repeat the "generating..." line down the chat.
                if is_busy:
                    has_seen_busy = True
                    continue

                # Grace period: if generation hasn't been observed yet and stream is not finished,
                # wait up to 3 seconds for the model to start responding or the stop button to mount
                if not has_seen_busy and not self.stream_finished and elapsed < 3.0:
                    continue

                # Generation stopped — read network payload (SSE body now available)
                if self.stream_finished or has_seen_busy or (elapsed >= 3.0 and not is_busy):
                    dlog.log("POLL", f"generation stopped, stream_finished={self.stream_finished}, trying network payload")
                    
                    candidate_text_pieces = []
                    for _lp in list(getattr(self, "live_payloads", []) or []):
                        try:
                            _parsed = self.adapter.parse_stream_payload(_lp)
                            if _parsed and (not candidate_text_pieces or len(_parsed) > len(candidate_text_pieces[0])):
                                candidate_text_pieces = [_parsed]
                        except Exception as e:
                            dlog.log("POLL", f"live payload parse error: {e}")
                    if not candidate_text_pieces and hasattr(self, "captured_responses") and self.captured_responses:
                        for resp in self.captured_responses:
                            try:
                                raw_payload = resp.text()
                                if raw_payload:
                                    parsed = self.adapter.parse_stream_payload(raw_payload)
                                    if parsed:
                                        candidate_text_pieces.append(parsed)
                            except Exception as e:
                                dlog.log("POLL", f"resp.text() error: {e}")
                                pass
                    
                    candidate_text = "".join(candidate_text_pieces).strip()

                    # Limit signal: an HTTP 429, or a short body with limit wording, means the
                    # account is out of messages. Checked on the RAW bodies, before the
                    # payload branches below would treat an error body as a normal reply.
                    try:
                        if (getattr(self, "auto_transfer_on_rate_limit", False)
                                and getattr(self.adapter, "name", "") != "deepseek"
                                and not self.is_page_generating()):
                            from cdp_stream import LIMIT_BODY_RE as _LBR
                            _rs = getattr(self, "rate_status", None)
                            _rs_i = int(_rs) if _rs is not None else None
                            _raws = [str(_x) for _x in (getattr(self, "live_payloads", []) or [])]
                            _short_hit = any(len(_r) < 600 and _LBR.search(_r) for _r in _raws)
                            _all_short = bool(_raws) and sum(len(_r) for _r in _raws) < 600
                            _limit_why = ""
                            if _rs_i == 429:
                                _limit_why = "http 429"
                            elif _rs_i is not None and _rs_i >= 400 and _short_hit:
                                _limit_why = "http " + str(_rs_i) + " with limit wording"
                            elif _rs_i is None and _all_short and _short_hit:
                                _limit_why = "short body with limit wording"
                            if _limit_why:
                                dlog.log("RATE", "limit signal: " + _limit_why)
                                self._tl(turn_marker, source="limit_signal", reason=_limit_why, stream_finished=self.stream_finished)
                                return "RATE_LIMIT_DETECTED"
                    except Exception as _le:
                        dlog.log("RATE", "limit signal check error: " + str(_le))
                    
                    if candidate_text:
                        cleaned_payload = self.adapter.clean_payload(candidate_text)
                        is_thought_only = self.adapter.is_pure_thought(cleaned_payload) if cleaned_payload else True
                        
                        if cleaned_payload and not is_thought_only:
                            dlog.log("POLL", f"network payload ok ({len(cleaned_payload)} chars)")
                            self._tl(turn_marker, source="network", net_len=len(candidate_text), final_len=len(cleaned_payload), stream_finished=self.stream_finished, cdp=getattr(self, "cdp_captured", False), n_live=len(getattr(self, "live_payloads", []) or []), elapsed=round(elapsed, 1))
                            final_text = cleaned_payload
                            break
                        else:
                            dlog.log("POLL", "network payload thought-only or empty, fallback to UI")
                            self._tl(turn_marker, source="ui_pending", reason="thought_only_or_empty", net_len=len(candidate_text), cleaned_len=len(cleaned_payload or ""), stream_finished=self.stream_finished, cdp=getattr(self, "cdp_captured", False), elapsed=round(elapsed, 1))
                            break
                    else:
                        if self._confirm_rate_limit():
                            return "RATE_LIMIT_DETECTED"
                        dlog.log("POLL", "no network payload captured, fallback to UI")
                        self._tl(turn_marker, source="ui_pending", reason="no_payload", stream_finished=self.stream_finished, cdp=getattr(self, "cdp_captured", False), n_live=len(getattr(self, "live_payloads", []) or []), elapsed=round(elapsed, 1))
                        break
                
                # Timeout fallback
                if elapsed > 15 and not is_busy:
                    pass
                    sys.stdout.flush()
                    dlog.log("POLL", f"15s idle timeout, fallback to UI")
                    self._tl(turn_marker, source="ui_pending", reason="idle_timeout_15s", stream_finished=self.stream_finished, cdp=getattr(self, "cdp_captured", False), elapsed=round(elapsed, 1))
                    break
                        
            # If network stream provided a valid cleaned payload, return immediately
            pass
            sys.stdout.flush()  # clear spinner line
            if final_text and not self.adapter.is_pure_thought(final_text):
                self.last_source = "network"
                self._last_ui_reason = ""
                return f"{turn_marker}\n{final_text}"

            # Guard: if the prompt never registered and nothing was captured,
            # the UI scrape can only return the previous reply. Return empty so
            # the caller retries instead of re-running an old reply.
            if (not prompt_registered and not has_seen_busy
                    and not self.stream_finished
                    and not list(getattr(self, "live_payloads", []) or [])):
                dlog.log("SEND", "unregistered and no stream captured; refusing stale UI scrape")
                self._tl(turn_marker, source="none", reason="unregistered_no_stream", final_len=0)
                return ""

            # ----------------------------------------------------
            # 2. HIGH-FIDELITY FALLBACK (UI EXTRACTION)
            # ----------------------------------------------------
            dlog.log("UI", f"extracting high-fidelity response via {self.adapter.display_name} UI")
            
            # Ensure generation has completed in UI (fast path: short cadence,
            # exits after two consecutive idle checks instead of a fixed 0.5s sleep).
            done_streak = 0
            for _ in range(60):
                if not self.is_page_generating():
                    done_streak += 1
                    if done_streak >= 2:
                        break
                else:
                    done_streak = 0
                time.sleep(0.15)

            self.scroll_chat_to_bottom()
            ui_text = self.adapter.extract_ui_response(self.page, turn_marker)
            self.last_source = "ui"
            self._log_ui_fallback(turn_marker, ui_text)
            if ui_text:
                cleaned_ui = self.adapter.clean_payload(ui_text) or ui_text.strip()
                if cleaned_ui:
                    dlog.log("UI", f"extracted {len(cleaned_ui)} chars from UI")
                    self._tl(turn_marker, source="ui", final_len=len(cleaned_ui), ui_raw_len=len(ui_text))
                    return f"{turn_marker}\n{cleaned_ui}"
            
            # If still nothing, return the best captured text or empty string
            self._tl(turn_marker, source="none", reason="ui_empty", final_len=len(final_text or ""))
            return final_text if final_text else ""
        except Exception as e:
            print(f"[Browser] Response monitoring exception: {e}")
            return ""

    def send_prompt_and_get_response(self, base_prompt, turn_marker, auto_submit=True):
        """Sends the prompt and monitors the stream."""
        if not self.paste_prompt_to_chat(base_prompt, turn_marker):
            return "FAILED_TO_PASTE"
        if auto_submit:
            time.sleep(0.2)
            self.trigger_submit_if_needed()
        return self.monitor_active_response(turn_marker)

    def wait_for_response(self, turn_marker):
        return self.monitor_active_response(turn_marker)

    def get_last_prompt_tokens(self):
        return self.last_sniffed_prompt_tokens

    def close(self):
        try:
            if self.browser:
                self.browser.close()
        except: pass
        try:
            if self.playwright:
                self.playwright.stop()
        except: pass


class AIStudioController(WebAgentController):
    """
    Backward-compatible subclass for Google AI Studio controller.
    Preserves 100% of the historical API surface for existing tests and scripts.
    """
    def __init__(self, cdp_url="http://127.0.0.1:9222"):
        super().__init__(cdp_url=cdp_url, provider="gemini")
