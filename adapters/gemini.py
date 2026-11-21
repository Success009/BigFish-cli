# adapters/gemini.py - Google AI Studio Adapter for BigFish CLI
import os
import re
import json
import time
import subprocess
from typing import Optional, List, Tuple
from adapters.base import BaseWebAdapter
import debuglog as dlog
from parser import strip_model_thoughts, is_pure_thought_text

def extract_all_json_groups(text):
    """Robust JSON string parser using balanced braces to catch deeply nested or escaped arrays/objects."""
    results = []
    if not text:
        return results
    length = len(text)
    i = 0
    while i < length:
        ch = text[i]
        if ch in ('{', '['):
            stack = [ch]
            start = i
            i += 1
            in_string = False
            escape = False
            while i < length and stack:
                c = text[i]
                if escape:
                    escape = False
                elif c == '\\':
                    escape = True
                elif c == '"':
                    in_string = not in_string
                elif not in_string:
                    if c in ('{', '['):
                        stack.append(c)
                    elif c == '}' and stack[-1] == '{':
                        stack.pop()
                    elif c == ']' and stack[-1] == '[':
                        stack.pop()
                i += 1
            if not stack:
                candidate = text[start:i]
                try:
                    obj = json.loads(candidate)
                    results.append(obj)
                except Exception:
                    pass
            continue
        i += 1
    return results

def recursive_extract_makersuite_text(node):
    """
    Recursively navigates Google AI Studio's internal protobuf-like array payloads.
    Targets model-generated response candidates while avoiding reflection of user prompts.
    """
    text_chunks = []
    if isinstance(node, str):
        return [node]
    elif isinstance(node, list):
        if len(node) >= 2 and isinstance(node[0], str) and isinstance(node[1], list):
            if node[0] == "model":
                for child in node[1:]:
                    text_chunks.extend(recursive_extract_makersuite_text(child))
                return text_chunks
        for item in node:
            text_chunks.extend(recursive_extract_makersuite_text(item))
    elif isinstance(node, dict):
        for k, v in node.items():
            text_chunks.extend(recursive_extract_makersuite_text(v))
    return text_chunks

def extract_text_from_any_payload(data):
    """
    Directly unpacks standard and RPC JSON objects emitted by the AI Studio frontend.
    Handles generateContent responses: { candidates: [ { content: { parts: [ { text: "..." } ] } } ] }
    """
    extracted = []
    if isinstance(data, dict):
        if "candidates" in data and isinstance(data["candidates"], list):
            for cand in data["candidates"]:
                if isinstance(cand, dict) and "content" in cand:
                    content = cand["content"]
                    if isinstance(content, dict) and "parts" in content:
                        for part in content.get("parts", []):
                            if isinstance(part, dict) and "text" in part:
                                extracted.append(part["text"])
        for k, v in data.items():
            if k != "candidates":
                extracted.extend(extract_text_from_any_payload(v))
    elif isinstance(data, list):
        for item in data:
            extracted.extend(extract_text_from_any_payload(item))
    return extracted

def sanitize_and_extract_strings(raw_payload):
    """Aggressive fallback for RPC payloads with Google prepended garbage prefixes."""
    if not raw_payload:
        return []
    cleaned = raw_payload.lstrip(")]}'\n ")
    extracted = []
    try:
        data = json.loads(cleaned)
        extracted = extract_text_from_any_payload(data)
        if not extracted:
            extracted = recursive_extract_makersuite_text(data)
        if extracted:
            return extracted
    except Exception:
        pass

    try:
        groups = extract_all_json_groups(cleaned)
        for g in groups:
            res = extract_text_from_any_payload(g)
            if not res:
                res = recursive_extract_makersuite_text(g)
            if res:
                extracted.extend(res)
        if extracted:
            return extracted
    except Exception:
        pass

    matches = re.findall(r'"text"\s*:\s*"((?:\\.|[^"\\])*)"', cleaned)
    for m in matches:
        try:
            val = json.loads(f'"{m}"')
            extracted.append(val)
        except Exception:
            extracted.append(m)
    return extracted

def parse_counttokens_body(raw):
    """AI Studio CountTokens bodies look like [328,[],[[[327],1]],null,null,[[1,328]]]:
    element 0 is the total prompt token count. Returns an int or None for any other shape."""
    try:
        b = raw.lstrip()
        if b.startswith(")]}'"):
            b = b[4:]
        d = json.loads(b)
        n = d[0]
        if isinstance(n, int) and not isinstance(n, bool) and n >= 0:
            return n
    except Exception:
        pass
    return None


def extract_reply_chunks(payload_text):
    """Structure-aware extraction for AI Studio GenerateContent bodies.
    Each chunk is data[0][i][0][0][0][0][0]; slot 1 holds text and slot 12 == 1 marks thinking.
    Metadata (model marker, message id, encrypted blob, timestamp) lives outside slot 1.
    Returns the reply text (may be empty), or None when the body is not in this shape,
    so the caller falls back to the generic string scan."""
    try:
        b = payload_text.lstrip()
        if b.startswith(")]}'"):
            b = b[4:]
        data = json.loads(b)
        chunks = data[0]
        if not isinstance(chunks, list):
            return None
    except Exception:
        return None
    parts, recognized = [], False
    for entry in chunks:
        try:
            slots = entry[0][0][0][0][0]
            if not isinstance(slots, list) or len(slots) < 2 or not isinstance(slots[1], str):
                continue
        except Exception:
            continue
        recognized = True
        thought = len(slots) > 12 and bool(slots[12])
        if not thought and slots[1]:
            parts.append(slots[1])
    return "".join(parts) if recognized else None

def parse_gemini_stream_payload(payload_text):
    """High-performance parser for raw chunks intercepted from AI Studio network streams."""
    if not payload_text:
        return ""
    structured = extract_reply_chunks(payload_text)
    if structured is not None:
        return structured
    candidates = sanitize_and_extract_strings(payload_text)
    if not candidates:
        return ""
    meaningful = [c for c in candidates if c and not c.isspace()]
    if not meaningful:
        return ""
    return "".join(meaningful)

def find_genuine_response_start(text):
    """
    Finds the character offset where the genuine response or tool execution starts,
    skipping any simulated/quoted tags mentioned inside thought sections.
    """
    candidates = []
    # 1. Genuine tool call (must include a tool_name tag)
    for m in re.finditer(r'<(?:execute_tool|call_to_tool|tool_call|call)\b[^>]*>([\s\S]*?)(?:</(?:execute_tool|call_to_tool|tool_call|call)>|$)', text, re.IGNORECASE):
        if re.search(r'<(?:tool_name|name|tool_call_name)\b', m.group(1), re.IGNORECASE):
            candidates.append(m.start())
            break
            
    # 2. Genuine message call (must have substantive, non-thought content)
    for m in re.finditer(r'<(?:message|talk_to_user|msg|explanation)\b[^>]*>([\s\S]*?)(?:</(?:message|talk_to_user|msg|explanation)>|$)', text, re.IGNORECASE):
        content = m.group(1).strip()
        if not is_pure_thought_text(content):
            candidates.append(m.start())
            break
            
    if candidates:
        return min(candidates)
    return None

_NEW_TAG_NAMES = ("say", "sh", "write", "edit", "read", "ls", "cd", "rm", "mv", "grep",
                  "web", "bg_start", "bg_list", "bg_stop", "confirm")


def _cut_to_first_tag(text):
    """Drop a UI preamble (thought summaries and similar) in front of the first line that
    starts with a new-format tag. No such line: return the text unchanged."""
    lt = chr(60)
    pat = re.compile("^" + lt + "(" + "|".join(_NEW_TAG_NAMES) + r")(?::[A-Za-z0-9_]+)?(?=[ \t/" + chr(62) + "])", re.MULTILINE)
    m = pat.search(text)
    if m and m.start() > 0:
        return text[m.start():]
    return text


def clean_response_payload(text):
    """New-format-safe cleaner. Never cuts text based on old tag names.
    Drops NULs, removes explicit thought blocks, strips a trailing done marker."""
    if not text:
        return ""
    text = text.replace(chr(0), "")
    lt = chr(60)
    thought_re = (lt + r"(thought|thinking|think|reasoning|reflection|internal|antthinking|thought_process)(?:\s[^" + chr(62) + r"]*)?" + chr(62)
                  + r"[\s\S]*?" + lt + r"/\1\s*" + chr(62))
    text = re.sub(thought_re, "", text, flags=re.IGNORECASE)
    text = _cut_to_first_tag(text)
    text = re.sub(r"\s*__DONE__\s*$", "", text, flags=re.IGNORECASE)
    cleaned = text.strip()
    if is_pure_thought_text(cleaned):
        return ""
    return cleaned


def _legacy_clean_response_payload(text):
    """
    Cleans raw model response text from stream or UI.
    If valid XML tags (<message>, <execute_tool>, etc.) are present, slices from the
    first tag to immediately discard all preceding thought preamble.
    Strips trailing __DONE__ and any thoughts outside tags.
    Ensures internal thoughts are stripped and pure thoughts return empty string.
    """
    if not text:
        return ""
    text = text.replace('\x00', '')
    
    # Strip explicit thought XML blocks first
    text = re.sub(r'<(?:thought|thinking|think|reasoning|reflection|internal|antthinking|thought_process)(?:\s+[^>]*)?>[\s\S]*?</(?:thought|thinking|think|reasoning|reflection|internal|antthinking|thought_process)>', '', text, flags=re.IGNORECASE)

    start_pos = find_genuine_response_start(text)
    if start_pos is not None:
        text = text[start_pos:]
    else:
        text = strip_model_thoughts(text)
        
    closing_tags = list(re.finditer(r'</(?:message|talk_to_user|msg|explanation|execute_tool|call_to_tool|tool_call|call|command|tool_tool|tool)>', text, re.IGNORECASE))
    if closing_tags:
        last_close = closing_tags[-1]
        trailing_part = text[last_close.end():]
        if re.search(r'\b(?:__done__|done)\b', trailing_part, re.IGNORECASE) or len(trailing_part.strip()) > 0:
            text = text[:last_close.end()].strip()
    else:
        if '__done__' in text.lower():
            done_idx = text.lower().rfind('__done__')
            text = text[:done_idx].strip()
        elif re.search(r'\n\s*done\s*$', text, re.IGNORECASE):
            text = re.sub(r'\n\s*done\s*$', '', text, flags=re.IGNORECASE).strip()
        
    cleaned = text.strip()
    if is_pure_thought_text(cleaned):
        return ""
    return cleaned


class GeminiWebAdapter(BaseWebAdapter):
    """
    Full implementation of the Google AI Studio (`aistudio.google.com`) adapter.
    """
    name = "gemini"
    display_name = "Gemini"
    default_url = "https://aistudio.google.com/app/prompts/new_chat"
    domain_keywords = ("aistudio.google.com",)

    def extract_prompt_id(self, url: str) -> Optional[str]:
        if url and '/prompts/' in url:
            parts = url.split('/prompts/')[-1].split('?')[0].split('/')
            return parts[0] if parts and parts[0] != "new_chat" else None
        return None

    def wait_for_chat_ready(self, page, timeout: int = 60000) -> bool:
        start = time.time()
        timeout_sec = timeout / 1000.0 if timeout > 1000 else timeout
        js_ready_check = """() => {
            const ta = document.querySelector('ms-prompt-box textarea, textarea.cdk-textarea-autosize, textarea[aria-label*="prompt" i], textarea');
            if (!ta) return false;
            if (ta.disabled || ta.readOnly) return false;
            return true;
        }"""
        while (time.time() - start) < timeout_sec:
            try:
                if page and not page.is_closed() and page.evaluate(js_ready_check):
                    return True

                js_check_inaccessible = """() => {
                    const text = (document.body && document.body.innerText) || '';
                    return (
                        text.includes('Prompt cannot be accessed') ||
                        text.includes('The requested prompt does not exist or you do not have access')
                    );
                }"""
                if page and not page.is_closed() and page.evaluate(js_check_inaccessible):
                    print("[Gemini] ⚠️ Active profile cannot access this prompt URL. Redirecting to new chat...")
                    page.goto(self.default_url, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(1)
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def get_chat_box(self, page):
        prompt_box = page.locator('textarea[aria-label*="prompt" i], textarea.cdk-textarea-autosize, textarea[placeholder*="prompt" i]').last
        try:
            if prompt_box.count() > 0:
                return prompt_box
        except Exception:
            pass
        return page.locator('ms-prompt-box textarea, textarea').last

    def scroll_chat_to_bottom(self, page) -> None:
        try:
            page.evaluate("""() => {
                const autoscroll = document.querySelector('ms-autoscroll-container');
                if (autoscroll) autoscroll.scrollTop = autoscroll.scrollHeight;
                const scrollbar = document.querySelector('ms-items-scrollbar');
                if (scrollbar) scrollbar.scrollTop = scrollbar.scrollHeight;
            }""")
        except Exception:
            pass

    def paste_prompt(self, page, prompt_text: str, turn_marker: Optional[str] = None) -> bool:
        """Transfers the prompt into AI Studio's textarea. Tries fill() first (no clipboard, no OS focus needed); falls back to clipboard paste."""
        chat_box = self.get_chat_box(page)
        chat_box.click(force=True)
        time.sleep(0.1)

        # Preferred path: write straight into the textarea. Needs no window focus.
        try:
            chat_box.fill(prompt_text)
            time.sleep(0.1)
            _got = chat_box.input_value() or ""
            if len(_got) >= min(len(prompt_text), 5) and len(_got) >= len(prompt_text) // 2:
                try:
                    import debuglog as _dl
                    _dl.log("PASTE-PATH", "fill ok chars=%d" % len(_got))
                except Exception:
                    pass
                return True
        except Exception:
            pass
        try:
            import debuglog as _dl
            _dl.log("PASTE-PATH", "fill failed, using clipboard")
        except Exception:
            pass

        # Copy prompt text to clipboard
        if os.name == 'nt':
            import win32clipboard
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(prompt_text, win32clipboard.CF_UNICODETEXT)
            win32clipboard.CloseClipboard()
            time.sleep(0.1)
            page.keyboard.press("Control+v")
        else:
            try:
                p = subprocess.Popen(['xclip', '-selection', 'clipboard'], stdin=subprocess.PIPE)
                p.communicate(prompt_text.encode('utf-8'))
            except FileNotFoundError:
                try:
                    p = subprocess.Popen(['xsel', '--clipboard', '--input'], stdin=subprocess.PIPE)
                    p.communicate(prompt_text.encode('utf-8'))
                except FileNotFoundError:
                    pass
            time.sleep(0.15)
            page.keyboard.press("Control+v")
            time.sleep(0.2)

        # Verification
        chat_box_text = chat_box.input_value()
        if not chat_box_text or len(chat_box_text) < 5:
            # Fallback: direct set via Playwright locator fill
            chat_box.fill(prompt_text)
            time.sleep(0.2)
        return True

    def trigger_submit(self, page) -> bool:
        """Submits prompt using Run button or Ctrl+Enter in AI Studio."""
        js_click_run = """() => {
            const btn = document.querySelector('button.ctrl-enter-submits, button.run-button');
            if (btn && !btn.disabled && btn.offsetParent !== null) {
                btn.click();
                return true;
            }
            return false;
        }"""
        try:
            clicked = page.evaluate(js_click_run)
            if clicked:
                return True
        except Exception:
            pass
        # Fallback: Ctrl+Enter
        try:
            page.keyboard.press("Control+Enter")
            return True
        except Exception:
            return False

    def is_generating(self, page) -> bool:
        js_generating_check = """() => {
            const stopBtn = document.querySelector('button.stop-button, button[aria-label*="Stop" i], mat-icon[data-mat-icon-name="stop"], button .mat-icon[fonticon="stop"]');
            if (stopBtn && stopBtn.offsetParent !== null) return true;
            
            const runBtn = document.querySelector('button.ctrl-enter-submits, button.run-button');
            if (runBtn) {
                // AI Studio keeps Run enabled while generating and swaps its label; a disabled Run is just an empty box.
                const lbl = ((runBtn.innerText || '') + ' ' + (runBtn.getAttribute('aria-label') || '')).toLowerCase();
                if (lbl.includes('progress_activity') || lbl.includes('stop')) return true;
            }

            const cancelBtn = document.querySelector('button[aria-label*="Cancel" i]');
            if (cancelBtn && cancelBtn.offsetParent !== null) return true;

            const progressSpinners = document.querySelectorAll('mat-spinner, mat-progress-bar, .mat-mdc-progress-spinner');
            for (const sp of progressSpinners) {
                if (sp.offsetParent !== null) return true;
            }
            return false;
        }"""
        try:
            return bool(page.evaluate(js_generating_check))
        except Exception:
            return False

    def stop_generation(self, page) -> bool:
        js_stop = """() => {
            const stopBtn = document.querySelector('button.stop-button, button[aria-label*="Stop" i], button .mat-icon[fonticon="stop"]');
            if (stopBtn && stopBtn.offsetParent !== null) {
                stopBtn.click();
                return true;
            }
            const runBtn = document.querySelector('button.ctrl-enter-submits, button.run-button');
            if (runBtn && (runBtn.innerText.includes('Stop') || runBtn.getAttribute('aria-label') === 'Stop')) {
                runBtn.click();
                return true;
            }
            return false;
        }"""
        try:
            return bool(page.evaluate(js_stop))
        except Exception:
            return False

    def register_stream_listener(self, page, controller) -> None:
        controller.captured_responses = []
        controller.stream_finished = False

        def response_handler(response):
            try:
                if response.request.method != "POST":
                    return
            except Exception:
                return
            url_lower = response.url.lower()
            if "counttokens" in url_lower:
                try:
                    raw_t = response.text()
                    if raw_t:
                        m = re.search(r'"totalTokens"\s*:\s*(\d+)', raw_t)
                        if m:
                            controller.last_sniffed_prompt_tokens = int(m.group(1))
                        else:
                            _n = parse_counttokens_body(raw_t)
                            if _n is not None:
                                controller.last_sniffed_prompt_tokens = _n
                except Exception:
                    pass
            if "generatecontent" in url_lower or "streamgenerate" in url_lower:
                controller.captured_responses.append(response)
                controller.stream_finished = False

        def request_finished_handler(request):
            try:
                if request.method != "POST":
                    return
            except Exception:
                return
            url_lower = request.url.lower()
            if "generatecontent" in url_lower or "streamgenerate" in url_lower:
                controller.stream_finished = True

        def request_failed_handler(request):
            try:
                if request.method != "POST":
                    return
            except Exception:
                return
            url_lower = request.url.lower()
            if "generatecontent" in url_lower or "streamgenerate" in url_lower:
                controller.stream_finished = True

        page.on("response", response_handler)
        page.on("requestfinished", request_finished_handler)
        page.on("requestfailed", request_failed_handler)

    def parse_stream_payload(self, raw_payload: str) -> str:
        return parse_gemini_stream_payload(raw_payload)

    def clean_payload(self, text: str) -> str:
        return clean_response_payload(text)

    def find_response_start(self, text: str) -> Optional[int]:
        return find_genuine_response_start(text)

    def is_pure_thought(self, text: str) -> bool:
        return is_pure_thought_text(text)

    def check_page_errors(self, page) -> Optional[str]:
        js_check_error = """() => {
            const errorContainers = document.querySelectorAll(
                'mat-snack-bar-container, .mat-mdc-snack-bar-container, snack-bar-container, ' +
                '[role="alert"], ms-banner, .turn-error, .chat-turn-error, ms-error-notice, ' +
                '.error-container, .error-message'
            );
            for (const el of errorContainers) {
                if (el.closest('textarea, [contenteditable="true"], .chat-turn-container.user, pre, code')) continue;
                const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                if (
                    text.includes("you've reached your rate limit") ||
                    text.includes("rate limit exceeded") ||
                    text.includes("resource has been exhausted") ||
                    text.includes("quota exceeded") ||
                    text.includes("too many requests")
                ) {
                    return "RATE_LIMIT";
                }
                if (
                    text.includes("permission denied. please try again") ||
                    text.includes("an internal error has occurred") ||
                    text.includes("error generating response")
                ) {
                    return "PERMISSION_DENIED";
                }
            }
            const modelTurns = document.querySelectorAll('ms-chat-turn .chat-turn-container.model');
            if (modelTurns.length > 0) {
                const lastTurn = modelTurns[modelTurns.length - 1];
                const errorBadge = lastTurn.querySelector('.error, [class*="error"], mat-icon[color="warn"]');
                if (errorBadge) {
                    const errTxt = (lastTurn.innerText || '').toLowerCase();
                    if (errTxt.includes("rate limit") || errTxt.includes("quota") || errTxt.includes("resource has been exhausted")) {
                        return "RATE_LIMIT";
                    }
                    if (errTxt.includes("permission denied") || errTxt.includes("internal error") || errTxt.includes("error generating")) {
                        return "PERMISSION_DENIED";
                    }
                }
            }
            return null;
        }"""
        try:
            return page.evaluate(js_check_error)
        except Exception:
            return None

    def get_current_profile_identity(self, page) -> str:
        js_email = """() => {
            const btn = document.querySelector('[aria-label^="Google Account"]');
            if (btn) {
                const label = btn.getAttribute('aria-label');
                const emailMatch = label.match(/\\(([^)]+@[a-zA-Z0-9.-]+)\\)/);
                if (emailMatch) return emailMatch[1];
                return label.replace('Google Account:', '').trim().split('\\n')[0];
            }
            return "Unknown Profile";
        }"""
        try:
            return page.evaluate(js_email)
        except Exception:
            return "Unknown Profile"

    def extract_ui_response(self, page, turn_marker: str) -> str:
        js_get_last_turn_text = """(minTurns) => {
            const isInputElement = (el) => {
                if (!el || el.nodeType !== Node.ELEMENT_NODE) return false;
                const tag = el.tagName ? el.tagName.toLowerCase() : '';
                return (tag === 'textarea' || tag === 'input' || el.getAttribute('contenteditable') === 'true');
            };

            const traverseCleanText = (node) => {
                if (!node) return '';
                if (isInputElement(node)) return '';
                
                const tag = node.tagName ? node.tagName.toLowerCase() : '';
                const classes = node.className && typeof node.className === 'string' ? node.className.toLowerCase() : '';
                if (
                    tag === 'ms-implicit-thought' ||
                    tag === 'implicit-thought' ||
                    tag === 'ms-thought-chunk' ||
                    classes.includes('thought-container') ||
                    classes.includes('thought-panel') ||
                    classes.includes('thought-activity-host') ||
                    classes.includes('model-thought') ||
                    classes.includes('mat-expansion-panel')
                ) {
                    return '';
                }

                if (node.nodeType === Node.TEXT_NODE) {
                    return node.nodeValue;
                }

                let text = '';
                for (let child of node.childNodes) {
                    text += traverseCleanText(child);
                }
                return text;
            };

            const modelTurns = document.querySelectorAll('ms-chat-turn .chat-turn-container.model');
            if (!modelTurns || modelTurns.length === 0) return '';
            
            for (let i = modelTurns.length - 1; i >= 0; i--) {
                const candidate = modelTurns[i];
                if (candidate.closest('.thought-activity-host')) continue;
                const raw = traverseCleanText(candidate).trim();
                if (raw.length > 0) return raw;
            }
            return '';
        }"""
        try:
            return page.evaluate(js_get_last_turn_text) or ""
        except Exception:
            return ""

    def start_new_chat(self, page) -> bool:
        """Starts a fresh Google AI Studio chat session by navigating to new chat URL."""
        try:
            page.goto(self.default_url, wait_until="domcontentloaded", timeout=40000)
            return True
        except Exception:
            return False
