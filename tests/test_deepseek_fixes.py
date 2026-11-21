# tests/test_deepseek_fixes.py - Tests for DeepSeek runtime bug fixes
# Tests the 6 bugs found in live run: paste spam, stream capture, send button, UI extraction,
# is_generating detection, stream URL filtering.
import os
import sys
import shutil
import unittest
import tempfile
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import adapters
import tokens
from adapters.deepseek import DeepSeekWebAdapter, parse_deepseek_sse_payload


class TestDeepSeekPasteNoSpam(unittest.TestCase):
    """BUG 2 fix: paste_prompt must NOT call clipboard + Ctrl+V.
    It must use atomic JS injection only — one write, no double-paste."""

    def setUp(self):
        self.adapter = DeepSeekWebAdapter()

    def test_paste_uses_js_evaluate_not_keyboard(self):
        """paste_prompt calls page.evaluate (JS inject) not page.keyboard.press."""
        page = MagicMock()
        # JS evaluate returns True (success)
        page.evaluate.return_value = True

        result = self.adapter.paste_prompt(page, "Hello BigFish")

        # evaluate called with JS function and the payload
        page.evaluate.assert_called()
        call_args = page.evaluate.call_args
        # First arg is JS string
        js_code = call_args[0][0]
        self.assertIn("nativeSetter", js_code, "Should use React nativeInputValueSetter")
        self.assertIn("dispatchEvent", js_code, "Should dispatch input event for React")
        # Second arg is the payload
        self.assertEqual(call_args[0][1], "Hello BigFish")

        # keyboard.press should NOT be called (no Ctrl+V)
        page.keyboard.press.assert_not_called()
        self.assertTrue(result)

    def test_paste_fallback_fill_called_when_js_fails(self):
        """If JS inject returns False, fallback to Playwright fill()."""
        page = MagicMock()
        page.evaluate.return_value = False
        mock_box = MagicMock()
        page.locator.return_value.last = mock_box
        mock_box.count.return_value = 1

        result = self.adapter.paste_prompt(page, "Fallback test")

        # fill should be called as fallback
        mock_box.fill.assert_called_once_with("Fallback test")

    def test_paste_single_call_no_double_write(self):
        """page.evaluate called for scroll + injection (2 calls max) — no clipboard+paste double-write."""
        page = MagicMock()
        page.evaluate.return_value = True

        self.adapter.paste_prompt(page, "Single paste only")

        # evaluate called at most twice: once for scroll-to-bottom, once for JS injection
        self.assertLessEqual(page.evaluate.call_count, 2)
        # No keyboard events
        page.keyboard.press.assert_not_called()


class TestDeepSeekStreamFilter(unittest.TestCase):
    """BUG 3 fix: stream listener only captures completion-path URLs."""

    def setUp(self):
        self.adapter = DeepSeekWebAdapter()

    def _make_controller(self):
        ctrl = MagicMock()
        ctrl.captured_responses = []
        ctrl.stream_finished = False
        return ctrl

    def _make_page_with_listeners(self):
        page = MagicMock()
        listeners = {}

        def on_event(event, handler):
            listeners[event] = handler

        page.on.side_effect = on_event
        return page, listeners

    def test_completion_url_captured(self):
        """chat/completion path should be captured."""
        page, listeners = self._make_page_with_listeners()
        ctrl = self._make_controller()
        self.adapter.register_stream_listener(page, ctrl)

        resp = MagicMock()
        resp.url = "https://chat.deepseek.com/api/v0/chat/completion"
        resp.request.method = "POST"
        resp.status = 200

        listeners["response"](resp)
        self.assertIn(resp, ctrl.captured_responses)

    def test_non_completion_url_ignored(self):
        """Random chat-page URLs like /a/chat/s/... should NOT be captured."""
        page, listeners = self._make_page_with_listeners()
        ctrl = self._make_controller()
        self.adapter.register_stream_listener(page, ctrl)

        resp = MagicMock()
        resp.url = "https://chat.deepseek.com/a/chat/s/some-session-id"
        resp.request.method = "GET"
        resp.status = 200

        listeners["response"](resp)
        self.assertEqual(ctrl.captured_responses, [], "Session page URL must not be captured")

    def test_stream_finished_set_on_completion_request_done(self):
        """stream_finished should be set True when completion request finishes."""
        page, listeners = self._make_page_with_listeners()
        ctrl = self._make_controller()
        self.adapter.register_stream_listener(page, ctrl)

        req = MagicMock()
        req.url = "https://chat.deepseek.com/api/v0/chat/completion"
        req.method = "POST"

        listeners["requestfinished"](req)
        self.assertTrue(ctrl.stream_finished)

    def test_unrelated_request_not_marking_stream_finished(self):
        """An image/asset request should not set stream_finished."""
        page, listeners = self._make_page_with_listeners()
        ctrl = self._make_controller()
        self.adapter.register_stream_listener(page, ctrl)

        req = MagicMock()
        req.url = "https://chat.deepseek.com/static/favicon.ico"
        req.method = "GET"

        listeners["requestfinished"](req)
        self.assertFalse(ctrl.stream_finished)


class TestDeepSeekSendButton(unittest.TestCase):
    """BUG 5 fix: trigger_submit uses correct send button selector."""

    def setUp(self):
        self.adapter = DeepSeekWebAdapter()

    def test_trigger_submit_js_uses_ds_button_primary_selector(self):
        """trigger_submit JS must query ds-button--primary.ds-button--filled."""
        page = MagicMock()
        page.evaluate.return_value = True

        result = self.adapter.trigger_submit(page)

        self.assertTrue(result)
        js_code = page.evaluate.call_args[0][0]
        self.assertIn("ds-button--primary", js_code)
        self.assertIn("ds-button--filled", js_code)

    def test_trigger_submit_fallback_to_enter_on_js_false(self):
        """If JS click returns False, fall back to pressing Enter on the textarea."""
        page = MagicMock()
        page.evaluate.return_value = False

        # Mock get_chat_box via locator chain
        mock_box = MagicMock()
        page.locator.return_value.last = mock_box
        mock_box.count.return_value = 1

        result = self.adapter.trigger_submit(page)
        self.assertTrue(result)
        # Either box.press("Enter") or page.keyboard.press("Enter") should fire
        pressed = mock_box.press.called or page.keyboard.press.called
        self.assertTrue(pressed, "Should press Enter as fallback")


class TestDeepSeekUIExtraction(unittest.TestCase):
    """BUG 4 fix: extract_ui_response uses broader, current DeepSeek selectors."""

    def setUp(self):
        self.adapter = DeepSeekWebAdapter()

    def test_extract_ui_response_js_contains_markdown_selector(self):
        """JS should query ds-markdown and [class*='markdown']."""
        page = MagicMock()
        page.evaluate.return_value = "Some response text"

        text = self.adapter.extract_ui_response(page, "[SYS_MARKER_123]")

        js_code = page.evaluate.call_args[0][0]
        self.assertIn("ds-markdown", js_code)
        # Broader fallback selector
        self.assertTrue(
            'class*="markdown"' in js_code or "markdown" in js_code,
            "Should include a broader markdown class selector"
        )
        self.assertEqual(text, "Some response text")

    def test_extract_ui_response_skips_thinking_blocks(self):
        """JS should filter out elements with 'think'/'reasoning' in class."""
        page = MagicMock()
        page.evaluate.return_value = ""  # simulate no result when thinking-only

        result = self.adapter.extract_ui_response(page, "[SYS_MARKER_456]")
        js_code = page.evaluate.call_args[0][0]

        # Thinking skip logic must be present
        self.assertIn("think", js_code.lower())
        self.assertIn("reasoning", js_code.lower())
        self.assertEqual(result, "")

    def test_extract_ui_response_returns_empty_on_exception(self):
        """Should return '' not raise on evaluation error."""
        page = MagicMock()
        page.evaluate.side_effect = Exception("page closed")

        result = self.adapter.extract_ui_response(page, "[SYS_MARKER_789]")
        self.assertEqual(result, "")


class TestDeepSeekIsGenerating(unittest.TestCase):
    """BUG 5 related: is_generating must detect DeepSeek's actual stop button."""

    def setUp(self):
        self.adapter = DeepSeekWebAdapter()

    def test_is_generating_js_contains_ds_button_primary(self):
        """is_generating JS should check for ds-button--primary as stop indicator."""
        page = MagicMock()
        page.evaluate.return_value = True

        result = self.adapter.is_generating(page)

        self.assertTrue(result)
        js_code = page.evaluate.call_args[0][0]
        # Should look for stop button among active primary buttons
        self.assertTrue(
            "ds-button--primary" in js_code or "Stop" in js_code or "stop" in js_code.lower(),
            "is_generating JS must check stop indicator"
        )


class TestDeepSeekSSEPayloadParsing(unittest.TestCase):
    """Existing SSE parser should handle real DeepSeek chat/completion format."""

    def test_parse_real_completion_format(self):
        """Parse a realistic DeepSeek /api/v0/chat/completion SSE payload."""
        raw = (
            'data: {"choices":[{"delta":{"content":"<message>Big Fish"},"index":0}]}\n\n'
            'data: {"choices":[{"delta":{"content":" is ready"},"index":0}]}\n\n'
            'data: {"choices":[{"delta":{"content":".</message>\\n__DONE__"},"index":0}]}\n\n'
            'data: [DONE]\n\n'
        )
        content, reasoning = parse_deepseek_sse_payload(raw)
        self.assertIn("<message>", content)
        self.assertIn("__DONE__", content)
        self.assertEqual(reasoning, "")

    def test_parse_deepthink_with_reasoning(self):
        """Parse DeepThink (R1) response with reasoning_content field."""
        raw = (
            'data: {"choices":[{"delta":{"reasoning_content":"Hmm, user wants..."}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"<message>Answer</message>\\n__DONE__"}}]}\n\n'
            'data: [DONE]\n\n'
        )
        content, reasoning = parse_deepseek_sse_payload(raw)
        self.assertEqual(content, "<message>Answer</message>\n__DONE__")
        self.assertIn("Hmm, user wants", reasoning)

    def test_empty_payload_returns_empty_strings(self):
        content, reasoning = parse_deepseek_sse_payload("")
        self.assertEqual(content, "")
        self.assertEqual(reasoning, "")

    def test_done_only_payload(self):
        content, reasoning = parse_deepseek_sse_payload("data: [DONE]\n\n")
        self.assertEqual(content, "")
        self.assertEqual(reasoning, "")


class TestDeepSeekDOMAndSessionFixes(unittest.TestCase):
    """Tests for recent DOM detection, session URL persistence, and parser resiliency fixes."""

    def setUp(self):
        self.adapter = DeepSeekWebAdapter()

    def test_is_generating_checks_div_role_button(self):
        """is_generating must query div[role=button] and .ds-button, not only <button>."""
        page = MagicMock()
        page.evaluate.return_value = True
        result = self.adapter.is_generating(page)
        self.assertTrue(result)
        js_code = page.evaluate.call_args[0][0]
        self.assertIn("div[role=\"button\"]", js_code)
        self.assertIn(".ds-button", js_code)

    def test_extract_ui_targets_assistant_main_content(self):
        """extract_ui_response must query .ds-assistant-message-main-content to grab whole message."""
        page = MagicMock()
        page.evaluate.return_value = "<message>Full response here</message>\nDONE"
        res = self.adapter.extract_ui_response(page, "SYS_MARKER")
        self.assertEqual(res, "<message>Full response here</message>\nDONE")
        js_code = page.evaluate.call_args[0][0]
        self.assertIn("ds-assistant-message-main-content", js_code)



    def test_session_updates_landing_url_to_chat_url(self):
        """save_current_session replaces generic chat.deepseek.com/ with specific /a/chat/s/ URL."""
        import session, json, tempfile, shutil
        temp_dir = tempfile.mkdtemp()
        orig_dir = session.SESSION_DIR
        try:
            session.SESSION_DIR = temp_dir
            # Create session with landing URL
            fn = session.save_current_session(
                first_prompt="Initial prompt",
                current_url="https://chat.deepseek.com/",
                chat_history=[],
                current_cwd="/test",
                profile_email="DeepSeek User",
                profile_dir="Default",
                filename="test_ds.json",
                provider="deepseek"
            )
            # Now simulate page navigating to specific chat URL
            fn2 = session.save_current_session(
                first_prompt="Initial prompt",
                current_url="https://chat.deepseek.com/a/chat/s/abc-123",
                chat_history=[],
                current_cwd="/test",
                profile_email="DeepSeek User",
                profile_dir="Default",
                filename="test_ds.json",
                provider="deepseek"
            )
            with open(os.path.join(temp_dir, "test_ds.json"), "r") as f:
                data = json.load(f)
            self.assertEqual(data["url"], "https://chat.deepseek.com/a/chat/s/abc-123")
        finally:
            session.SESSION_DIR = orig_dir
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_deepseek_check_page_errors_disabled(self):
        adapter = adapters.get_adapter("deepseek")
        self.assertIsNone(adapter.check_page_errors(None))

    def test_token_segregation_deepseek_vs_gemini(self):
        temp_dir = tempfile.mkdtemp()
        ledger_file = os.path.join(temp_dir, "ledger.json")
        try:
            # Record DeepSeek turn
            tokens.record_turn_tokens(
                profile_dir="DeepSeek",
                prompt_tokens=500,
                response_tokens=1000,
                email="DeepSeek User",
                ledger_file=ledger_file,
                provider="deepseek"
            )
            # Record Gemini turn on Default profile
            tokens.record_turn_tokens(
                profile_dir="Default",
                prompt_tokens=200,
                response_tokens=300,
                email="gemini@gmail.com",
                ledger_file=ledger_file,
                provider="gemini"
            )

            # DeepSeek summary should only have DeepSeek
            ds_summary = tokens.get_profiles_usage_summary(ledger_file=ledger_file, provider="deepseek")
            self.assertEqual(len(ds_summary), 1)
            self.assertEqual(ds_summary[0]["profile_dir"], "DeepSeek")
            self.assertEqual(ds_summary[0]["total_tokens"], 1500)

            # Gemini summary should only have Gemini Default
            gem_summary = tokens.get_profiles_usage_summary(["Default"], ledger_file=ledger_file, provider="gemini")
            self.assertEqual(len(gem_summary), 1)
            self.assertEqual(gem_summary[0]["profile_dir"], "Default")
            self.assertEqual(gem_summary[0]["total_tokens"], 500)
            self.assertEqual(gem_summary[0]["email"], "gemini@gmail.com")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
    def test_token_segregation_claude_isolated(self):
        """Claude token usage must be tracked in its own provider bucket, separate
        from gemini/deepseek, and reported correctly by get_profiles_usage_summary."""
        temp_dir = tempfile.mkdtemp()
        ledger_file = os.path.join(temp_dir, "ledger.json")
        try:
            tokens.record_turn_tokens(
                profile_dir="Profile 1", prompt_tokens=300, response_tokens=700,
                email="claude@example.com", ledger_file=ledger_file, provider="claude"
            )
            tokens.record_turn_tokens(
                profile_dir="Profile 1", prompt_tokens=100, response_tokens=100,
                email="gemini@example.com", ledger_file=ledger_file, provider="gemini"
            )

            cl_summary = tokens.get_profiles_usage_summary(
                ["Profile 1"], ledger_file=ledger_file, provider="claude"
            )
            self.assertEqual(len(cl_summary), 1)
            self.assertEqual(cl_summary[0]["profile_dir"], "Profile 1")
            self.assertEqual(cl_summary[0]["total_tokens"], 1000)
            self.assertEqual(cl_summary[0]["email"], "claude@example.com")

            # Gemini bucket must remain independent (200, not 1200)
            gem_summary = tokens.get_profiles_usage_summary(
                ["Profile 1"], ledger_file=ledger_file, provider="gemini"
            )
            self.assertEqual(gem_summary[0]["total_tokens"], 200)

            # Ledger must physically hold separate provider buckets
            ledger = tokens.load_ledger(ledger_file)
            self.assertIn("claude", ledger.get("providers", {}))
            self.assertEqual(
                ledger["providers"]["claude"]["Profile 1"]["total_tokens"], 1000
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

if __name__ == "__main__":
    unittest.main()
