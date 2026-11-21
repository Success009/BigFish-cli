# adapters/deepseek.py - DeepSeek Chat Web Adapter for BigFish CLI
import re
import json
import time
from typing import Optional, List, Tuple
from adapters.base import BaseWebAdapter
import debuglog as dlog

def parse_deepseek_sse_payload(payload_text: str) -> Tuple[str, str]:
    """
    Parses Server-Sent Events (SSE) stream chunks emitted by chat.deepseek.com.
    Returns a tuple of (content_text, reasoning_text).
    """
    if not payload_text:
        return "", ""
    
    content_pieces = []
    reasoning_pieces = []

    lines = payload_text.splitlines()
    for line in lines:
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if data_str == "[DONE]":
            continue
        try:
            chunk = json.loads(data_str)
            choices = chunk.get("choices", [])
            for c in choices:
                delta = c.get("delta", {})
                if "content" in delta and delta["content"]:
                    content_pieces.append(delta["content"])
                if "reasoning_content" in delta and delta["reasoning_content"]:
                    reasoning_pieces.append(delta["reasoning_content"])
        except Exception:
            # Fallback regex for non-standard / partial JSON chunks
            c_matches = re.findall(r'"content"\s*:\s*"((?:\\.|[^"\\])*)"', data_str)
            for m in c_matches:
                try: content_pieces.append(json.loads(f'"{m}"'))
                except Exception: content_pieces.append(m)
            r_matches = re.findall(r'"reasoning_content"\s*:\s*"((?:\\.|[^"\\])*)"', data_str)
            for m in r_matches:
                try: reasoning_pieces.append(json.loads(f'"{m}"'))
                except Exception: reasoning_pieces.append(m)

    return "".join(content_pieces), "".join(reasoning_pieces)


class DeepSeekWebAdapter(BaseWebAdapter):
    """
    Adapter for DeepSeek Chat (https://chat.deepseek.com).
    Supports DeepSeek-V3 and DeepSeek-R1 (DeepThink) models via web UI.
    """
    name = "deepseek"
    display_name = "DeepSeek"
    default_url = "https://chat.deepseek.com"
    domain_keywords = ("chat.deepseek.com", "deepseek.com")

    def extract_prompt_id(self, url: str) -> Optional[str]:
        if url and '/a/chat/s/' in url:
            parts = url.split('/a/chat/s/')[-1].split('?')[0].split('/')
            return parts[0] if parts and parts[0] else None
        return None

    def wait_for_chat_ready(self, page, timeout: int = 60000) -> bool:
        """Waits for DeepSeek's main chat container and input textarea to be ready."""
        start = time.time()
        timeout_sec = timeout / 1000.0 if timeout > 1000 else timeout
        js_ready_check = """() => {
            const ta = document.querySelector(
                '#chat-input, ' +
                'textarea[placeholder*="Ask anything" i], ' +
                'textarea[placeholder*="DeepSeek" i], ' +
                'textarea[placeholder*="message" i], ' +
                'textarea.chat-input, ' +
                'div[contenteditable="true"]'
            );
            if (!ta) return false;
            if (ta.disabled || ta.readOnly) return false;
            return true;
        }"""
        while (time.time() - start) < timeout_sec:
            try:
                if page and not page.is_closed() and page.evaluate(js_ready_check):
                    return True
                
                # Check for login / auth redirect requirement
                js_check_login = """() => {
                    const text = (document.body && document.body.innerText) || '';
                    return text.includes('Log in to DeepSeek') || text.includes('Sign up to DeepSeek');
                }"""
                if page and not page.is_closed() and page.evaluate(js_check_login):
                    print("[DeepSeek] ⚠️ DeepSeek session requires login. Please log in to your account.")
                    return False
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def get_chat_box(self, page):
        """Locates the primary prompt input textarea or contenteditable element in DeepSeek."""
        selectors = [
            '#chat-input',
            'textarea[placeholder*="Ask anything" i]',
            'textarea[placeholder*="DeepSeek" i]',
            'textarea[placeholder*="message" i]',
            'textarea.chat-input',
            'div[contenteditable="true"]',
            'textarea'
        ]
        for sel in selectors:
            loc = page.locator(sel).last
            try:
                if loc.count() > 0:
                    return loc
            except Exception:
                pass
        return page.locator('textarea').last

    def scroll_chat_to_bottom(self, page) -> None:
        try:
            page.evaluate("""() => {
                const scrollContainers = document.querySelectorAll(
                    '.chat-scroll-container, .chat-container, [class*="scroll"], main'
                );
                for (const c of scrollContainers) {
                    c.scrollTop = c.scrollHeight;
                }
                window.scrollTo(0, document.body.scrollHeight);
            }""")
        except Exception:
            pass

    def paste_prompt(self, page, prompt_text: str, turn_marker: Optional[str] = None) -> bool:
        """Atomically injects prompt into DeepSeek chat input via React-compatible JS dispatch.
        No clipboard used — avoids double-paste from clipboard+fill fallback race condition."""

        # Scroll to bottom so the chat input is in view and React is mounted
        try:
            page.evaluate("() => { window.scrollTo(0, document.body.scrollHeight); }")
            time.sleep(0.1)
        except Exception:
            pass

        # Step 1: Clear any existing content, then atomically set value via React nativeInputValueSetter
        js_inject = """(payload) => {
            // Find the textarea
            const selectors = [
                'textarea[placeholder*="Message DeepSeek" i]',
                '#chat-input',
                'textarea[placeholder*="Ask anything" i]',
                'textarea[placeholder*="message" i]',
                'textarea'
            ];
            let el = null;
            for (const sel of selectors) {
                el = document.querySelector(sel);
                if (el && !el.disabled && !el.readOnly) break;
            }
            if (!el) return false;

            el.focus({ preventScroll: true });

            // React uses a custom setter — must use it to trigger onChange
            const nativeSetter = Object.getOwnPropertyDescriptor(
                window.HTMLTextAreaElement.prototype, 'value'
            );
            if (nativeSetter && nativeSetter.set) {
                nativeSetter.set.call(el, payload);
            } else {
                el.value = payload;
            }

            // Fire React synthetic input event
            el.dispatchEvent(new Event('input', { bubbles: true, cancelable: true }));
            el.dispatchEvent(new Event('change', { bubbles: true, cancelable: true }));

            return el.value.length > 0;
        }"""
        try:
            success = page.evaluate(js_inject, prompt_text)
        except Exception:
            success = False

        if not success:
            # Fallback: Playwright fill() which also fires input events
            try:
                box = self.get_chat_box(page)
                box.click(force=True)
                time.sleep(0.1)
                box.fill(prompt_text)
                success = True
            except Exception:
                pass

        time.sleep(0.15)
        return bool(success)

    def trigger_submit(self, page) -> bool:
        """Submits the DeepSeek prompt.
        
        Strategy order:
        1. JS click on the primary send circle button (waiting up to 1.5s for enabled state)
        2. Press Enter on the focused textarea
        3. Global keyboard Enter fallback
        """
        # Strategy 1: JS click the enabled send button (poll briefly for React render)
        js_click_send = """() => {
            const sendBtn = document.querySelector(
                'div[role="button"].ds-button--primary.ds-button--circle:not(.ds-button--disabled), ' +
                'div[role="button"].ds-button--filled.ds-button--circle:not(.ds-button--disabled), ' +
                'div[role="button"].ds-button--primary:not(.ds-button--disabled), ' +
                'div[role="button"].ds-button--filled:not(.ds-button--disabled)'
            );
            if (sendBtn && sendBtn.offsetParent !== null && sendBtn.getAttribute('aria-disabled') !== 'true') {
                sendBtn.click();
                return 'send_circle_clicked';
            }
            
            // Fallback candidates
            const candidates = [
                document.querySelector('button[aria-label*="Send" i]:not([disabled])'),
                document.querySelector('[data-testid="send-button"]:not([disabled])'),
                ...Array.from(document.querySelectorAll('div[role="button"]:not(.ds-button--disabled):not([disabled]), button:not([disabled])')).filter(b => {
                    const hasSvg = b.querySelector('svg');
                    const hasRect = b.querySelector('svg rect');
                    const isFloating = b.className && b.className.includes('floating');
                    const visible = b.offsetParent !== null;
                    const nearTextarea = document.querySelector('textarea') && 
                        b.getBoundingClientRect().bottom > (document.querySelector('textarea').getBoundingClientRect().top - 20);
                    return hasSvg && !hasRect && !isFloating && visible && nearTextarea;
                }),
            ];
            for (const btn of candidates) {
                if (btn && btn.offsetParent !== null && !btn.classList.contains('ds-button--disabled')) {
                    btn.click();
                    return btn.className || btn.tagName || 'clicked';
                }
            }
            return null;
        }"""
        for _ in range(15):
            try:
                result = page.evaluate(js_click_send)
                if result:
                    dlog.log("SUBMIT", f"JS click send button: {str(result)[:40]}")
                    return True
            except Exception as e:
                dlog.log("SUBMIT", f"JS click send failed: {e}")
            time.sleep(0.1)

        # Strategy 2: Focus and Press Enter on the textarea
        try:
            selectors = [
                'textarea[placeholder*="Message DeepSeek" i]',
                '#chat-input',
                'textarea[placeholder*="Ask anything" i]',
                'textarea[placeholder*="message" i]',
                'textarea',
            ]
            for sel in selectors:
                loc = page.locator(sel)
                if loc.count() > 0:
                    loc.last.focus()
                    time.sleep(0.05)
                    loc.last.press("Enter")
                    dlog.log("SUBMIT", f"Enter pressed on {sel}")
                    return True
        except Exception as e:
            dlog.log("SUBMIT", f"textarea.press Enter failed: {e}")

        # Strategy 3: Global keyboard Enter
        try:
            page.keyboard.press("Enter")
            dlog.log("SUBMIT", "global keyboard Enter")
            return True
        except Exception:
            return False

    def is_generating(self, page) -> bool:
        """Detects whether DeepSeek is actively generating a response.
        
        DeepSeek send/stop button is a div[role="button"] (not <button>).
        During generation, the send arrow is replaced by a square stop icon (svg rect or viewBox 36 36).
        """
        js_generating_check = """() => {
            // Stop button = square icon = svg rect in any button-like element (div[role="button"], button, .ds-button)
            const stopByRect = document.querySelector(
                'div[role="button"]:has(svg rect), ' +
                '.ds-button:has(svg rect), ' +
                'button:has(svg rect), ' +
                '[role="button"]:has(svg rect), ' +
                '[role="button"]:has(svg [viewBox*="36 36"]), ' +
                '.ds-button--circle:has(svg rect)'
            );
            if (stopByRect && stopByRect.offsetParent !== null) return true;

            // Aria label / data-testid fallback
            const stopByLabel = document.querySelector(
                '[aria-label*="Stop" i], [title*="Stop" i], [data-testid="stop-button"]'
            );
            if (stopByLabel && stopByLabel.offsetParent !== null) return true;

            // Spinner / loading elements
            const loading = document.querySelector('.ds-loading, .ds-spinner');
            if (loading && loading.offsetParent !== null) return true;

            // Textarea disabled during generation
            const ta = document.querySelector('textarea');
            if (ta && ta.disabled) return true;

            return false;
        }"""
        try:
            return bool(page.evaluate(js_generating_check))
        except Exception:
            return False

    def stop_generation(self, page) -> bool:
        """Clicks DeepSeek's stop button (square svg rect icon) if active."""
        js_stop = """() => {
            const candidates = [
                document.querySelector('div[role="button"]:has(svg rect)'),
                document.querySelector('.ds-button:has(svg rect)'),
                document.querySelector('button:has(svg rect)'),
                document.querySelector('[role="button"]:has(svg rect)'),
                document.querySelector('[role="button"]:has(svg [viewBox*="36 36"])'),
                document.querySelector('[aria-label*="Stop" i]'),
                document.querySelector('[data-testid="stop-button"]'),
                document.querySelector('.ds-stop-button'),
            ];
            for (const btn of candidates) {
                if (btn && btn.offsetParent !== null) {
                    btn.click();
                    return true;
                }
            }
            return false;
        }"""
        try:
            return bool(page.evaluate(js_stop))
        except Exception:
            return False

    def register_stream_listener(self, page, controller) -> None:
        """Attaches network listeners targeting DeepSeek's chat completion SSE stream endpoint."""
        controller.captured_responses = []
        controller.stream_finished = False

        # DeepSeek web chat hits: https://chat.deepseek.com/api/v0/chat/completion
        # We match on the path fragment "chat/completion" to avoid false positives
        DEEPSEEK_STREAM_PATHS = (
            "api/v0/chat/completion",
            "api/chat/completion",
            "chat/completion",
            "v1/chat/completions",  # may also hit this on some configs
        )

        def _is_generation_url(url: str) -> bool:
            url_lower = url.lower()
            # Must contain at least one of these path fragments
            return any(path in url_lower for path in DEEPSEEK_STREAM_PATHS)

        def response_handler(response):
            try:
                if response.request.method not in ("POST", "GET"):
                    return
                if not _is_generation_url(response.url):
                    return
                # Only capture if status indicates a streaming or successful response
                if response.status not in (200, 206):
                    return
                controller.captured_responses.append(response)
                controller.stream_finished = False
                dlog.log("STREAM", f"captured response url={response.url[:60]} status={response.status}")
            except Exception:
                pass

        def request_finished_handler(request):
            try:
                if request.method not in ("POST", "GET"):
                    return
                if not _is_generation_url(request.url):
                    return
                controller.stream_finished = True
                dlog.log("STREAM", f"request finished url={request.url[:60]}")
            except Exception:
                pass

        def request_failed_handler(request):
            try:
                if not _is_generation_url(request.url):
                    return
                controller.stream_finished = True
                dlog.log("STREAM", f"request FAILED url={request.url[:60]}")
            except Exception:
                pass

        page.on("response", response_handler)
        page.on("requestfinished", request_finished_handler)
        page.on("requestfailed", request_failed_handler)

    def parse_stream_payload(self, raw_payload: str) -> str:
        content, _ = parse_deepseek_sse_payload(raw_payload)
        return content

    def clean_payload(self, text: str) -> str:
        """New-format-safe: never trims by old tag names. Drops NULs and a trailing done marker."""
        if not text:
            return ""
        text = text.replace(chr(0), "")
        text = re.sub(r"\s*__DONE__\s*$", "", text, flags=re.IGNORECASE)
        return text.strip()

    def _legacy_clean_payload(self, text: str) -> str:
        """Cleans DeepSeek response payload, preserving XML tags up to __DONE__."""
        if not text:
            return ""
        pos = self.find_response_start(text)
        if pos is not None:
            trimmed = text[pos:]
            _TOK = chr(95) * 2 + "DONE" + chr(95) * 2
            try:
                from parser import _mask_cdata, _mask_content_bodies
                _masked = _mask_content_bodies(_mask_cdata(trimmed))
            except Exception:
                _masked = trimmed
            done_matches = list(re.finditer(re.escape(_TOK), _masked, re.IGNORECASE))
            if done_matches:
                return trimmed[:done_matches[-1].end()].strip()
            done_alt = re.search(r'</(?:message|execute_tool|call_to_tool|tool_call|call)>\s*done\b', trimmed, re.IGNORECASE)
            if done_alt:
                return trimmed[:done_alt.end()].strip()
            return trimmed.strip()
        return text.strip()

    def find_response_start(self, text: str) -> Optional[int]:
        return 0 if text else None

    def _legacy_find_response_start(self, text: str) -> Optional[int]:
        if not text:
            return None
        markers = [
            r'<\s*message\b',
            r'<\s*talk_to_user\b',
            r'<\s*msg\b',
            r'<\s*explanation\b',
            r'<\s*execute_tool\b',
            r'<\s*call_to_tool\b',
            r'<\s*tool_call\b',
            r'<\s*call\b'
        ]
        earliest_pos = None
        for pattern in markers:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                pos = match.start()
                if earliest_pos is None or pos < earliest_pos:
                    earliest_pos = pos
        return earliest_pos

    def is_pure_thought(self, text: str) -> bool:
        """New format: only an empty reply counts as a thought. Everything else goes to the parser."""
        return not (text and text.strip())

    def _legacy_is_pure_thought(self, text: str) -> bool:
        """Returns True only if text is provably nothing but internal AI thinking.
        
        The SSE stream payload is raw Markdown — it will never contain <message> tags.
        The UI/Fiber extraction gives us the full original text including <message> tags.
        So: check for XML tags first; if found, use tag presence. If no tags at all,
        treat as non-thought (raw markdown from SSE stream) so it goes to the parser.
        """
        if not text:
            return True
        # If XML structure tags are present, check for user-facing ones
        has_any_xml_tags = bool(re.search(r'<[a-zA-Z_]', text))
        if has_any_xml_tags:
            has_message_or_tool = bool(re.search(
                r'<(?:message|execute_tool|call_to_tool|tool_call|call|talk_to_user|msg)\b', 
                text, 
                re.IGNORECASE
            ))
            return not has_message_or_tool
        # No XML tags at all = raw Markdown from SSE stream — treat as valid content
        return False

    def check_page_errors(self, page) -> Optional[str]:
        """DeepSeek does not use automated transfer. Returns None to prevent false triggers and keep session intact."""
        return None

    def get_current_profile_identity(self, page) -> str:
        js_identity = """() => {
            const profileEl = document.querySelector(
                '.ds-user-avatar, [aria-label*="Account" i], .user-profile, [class*="avatar"]'
            );
            if (profileEl) {
                const label = profileEl.getAttribute('aria-label') || profileEl.innerText || '';
                if (label.trim()) return label.trim();
            }
            return "DeepSeek User";
        }"""
        try:
            return page.evaluate(js_identity) or "DeepSeek User"
        except Exception:
            return "DeepSeek User"

    def start_new_chat(self, page) -> bool:
        """Starts a fresh DeepSeek chat session by clicking the New Chat button or navigating."""
        try:
            js_new_chat = """() => {
                const allElements = Array.from(document.querySelectorAll('button, div[role="button"], a, div'));
                for (const el of allElements) {
                    const txt = (el.innerText || '').trim().toLowerCase();
                    const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                    if (txt === 'new chat' || aria === 'new chat' || txt === '+ new chat') {
                        el.click();
                        return true;
                    }
                }
                const plusBtn = document.querySelector('.ds-icon-plus, [class*="new-chat"], [class*="newChat"]');
                if (plusBtn) {
                    plusBtn.click();
                    return true;
                }
                return false;
            }"""
            clicked = page.evaluate(js_new_chat)
            if clicked:
                time.sleep(0.5)
                return True
            page.goto(self.default_url, wait_until="domcontentloaded", timeout=30000)
            return True
        except Exception:
            return False

    def extract_ui_response(self, page, turn_marker: str) -> str:
        """Extracts the latest assistant response from DeepSeek UI DOM.
        Tier 1: Pure raw Markdown directly from React Fiber props (preserves 100% of tables, headers, code, bullets).
        Tier 2: Semantic DOM-to-Markdown tree walker (reconstructs Markdown from HTML tags).
        Tier 3: Live connected element innerText (with temporary hiding of any thinking blocks)."""
        js_extract_deepseek = """() => {
            const assistantContainers = document.querySelectorAll(
                '.ds-assistant-message-main-content, ' +
                '.ds-markdown.ds-assistant-message-main-content, ' +
                '[class*="assistant-message-main-content"], ' +
                '.ds-message [class*="main-content"]'
            );

            // Tier 1: Pure Raw Markdown from React Fiber props
            for (let i = assistantContainers.length - 1; i >= 0; i--) {
                const el = assistantContainers[i];
                if (el.closest('[class*="user"], [data-role="user"]')) continue;
                
                let fiber = null;
                for (const k of Object.keys(el)) {
                    if (k.startsWith('__reactFiber$')) { fiber = el[k]; break; }
                }
                let curr = fiber;
                while (curr) {
                    const p = curr.memoizedProps;
                    if (p && typeof p.markdown === 'string' && p.markdown.length > 0) {
                        return p.markdown.trim();
                    }
                    if (p && typeof p.content === 'string' && p.content.length > 0 && !curr.stateNode?.classList?.contains('ds-think-content')) {
                        return p.content.trim();
                    }
                    curr = curr.return;
                }
            }

            // Tier 2: Semantic DOM-to-Markdown tree walker
            const domToMarkdown = (node) => {
                if (!node) return "";
                if (node.nodeType === Node.TEXT_NODE) return node.textContent;
                if (node.nodeType !== Node.ELEMENT_NODE) return "";
                
                const cls = String(node.className || "");
                const tag = node.tagName.toLowerCase();
                if (tag === "details" || cls.includes("think") || cls.includes("reasoning")) return "";
                
                const inner = Array.from(node.childNodes).map(domToMarkdown).join("");
                
                if (tag === "h1" || tag === "h2") return "\\n\\n## " + inner.trim() + "\\n\\n";
                if (tag === "h3" || tag === "h4") return "\\n\\n### " + inner.trim() + "\\n\\n";
                if (tag === "p") return "\\n\\n" + inner.trim() + "\\n\\n";
                if (tag === "li") return "\\n- " + inner.trim();
                if (tag === "ul" || tag === "ol") return "\\n" + inner.trim() + "\\n";
                if (tag === "code" && node.parentElement?.tagName.toLowerCase() !== "pre") return "`" + inner.trim() + "`";
                if (tag === "pre") return "\\n```\\n" + inner.trim() + "\\n```\\n";
                if (tag === "strong" || tag === "b") return "**" + inner.trim() + "**";
                if (tag === "em" || tag === "i") return "*" + inner.trim() + "*";
                if (tag === "tr") {
                    const cells = Array.from(node.querySelectorAll("th, td")).map(c => domToMarkdown(c).trim());
                    return "| " + cells.join(" | ") + " |\\n";
                }
                if (tag === "table") {
                    return "\\n\\n" + inner.trim() + "\\n\\n";
                }
                return inner;
            };

            for (let i = assistantContainers.length - 1; i >= 0; i--) {
                const el = assistantContainers[i];
                if (el.closest('[class*="user"], [data-role="user"]')) continue;
                const converted = domToMarkdown(el).replace(/\\n{3,}/g, "\\n\\n").trim();
                if (converted.length > 0) return converted;
            }

            // Tier 3: Live connected element innerText
            const cleanElementText = (el) => {
                if (!el) return '';
                try {
                    const thinkingEls = el.querySelectorAll(
                        'details, [class*="think" i], [class*="reasoning" i], [aria-label*="thinking" i], [class*="deepthink" i]'
                    );
                    const saved = [];
                    for (const th of thinkingEls) {
                        saved.push({ node: th, orig: th.style.display });
                        th.style.display = 'none';
                    }
                    const text = el.innerText || '';
                    for (const s of saved) {
                        s.node.style.display = s.orig;
                    }
                    return text.trim();
                } catch (e) {
                    return (el.innerText || el.textContent || '').trim();
                }
            };

            for (let i = assistantContainers.length - 1; i >= 0; i--) {
                const el = assistantContainers[i];
                if (el.closest('[class*="user"], [data-role="user"]')) continue;
                const text = cleanElementText(el);
                if (text.length > 0) return text;
            }

            // Strategy 2: Assistant .ds-message blocks
            const msgContainers = document.querySelectorAll('.ds-message, [data-role="assistant"], [class*="model-turn"]');
            for (let i = msgContainers.length - 1; i >= 0; i--) {
                const el = msgContainers[i];
                if (el.closest('[class*="user"], [data-role="user"]')) continue;
                const markdown = el.querySelector('.ds-markdown, [class*="markdown"]');
                const target = markdown || el;
                const text = cleanElementText(target);
                if (text.length > 0) return text;
            }

            return '';
        }"""
        try:
            return page.evaluate(js_extract_deepseek) or ""
        except Exception:
            return ""
