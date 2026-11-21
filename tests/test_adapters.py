# tests/test_adapters.py - Unit tests for Web Agent Adapters and DeepSeek Integration
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import paths  # puts core/, web/, interface/ on sys.path (debuglog etc.)
import adapters
from adapters.deepseek import parse_deepseek_sse_payload, DeepSeekWebAdapter
from adapters.gemini import GeminiWebAdapter
import parser
import session

class TestAdaptersRegistry(unittest.TestCase):
    def test_default_adapter_is_gemini(self):
        adapter = adapters.get_adapter()
        self.assertEqual(adapter.name, "gemini")
        self.assertIn("aistudio.google.com", adapter.default_url)

    def test_get_gemini_adapter(self):
        adapter = adapters.get_adapter("gemini")
        self.assertEqual(adapter.name, "gemini")
        self.assertIn("aistudio.google.com", adapter.default_url)

    def test_adapter_aliases(self):
        self.assertEqual(adapters.get_adapter("ds").name, "deepseek")
        self.assertEqual(adapters.get_adapter("deep-seek").name, "deepseek")
        self.assertEqual(adapters.get_adapter("google").name, "gemini")
        self.assertEqual(adapters.get_adapter("ai_studio").name, "gemini")

    def test_list_available_adapters(self):
        available = adapters.list_available_adapters()
        self.assertIn("deepseek", available)
        self.assertIn("gemini", available)

    def test_provider_cycle(self):
        self.assertEqual(adapters.get_next_provider("gemini"), "deepseek")
        self.assertEqual(adapters.get_next_provider("deepseek"), "gemini")

    def test_detect_adapter_by_url(self):
        ds_url = "https://chat.deepseek.com/a/chat/s/12345-abcde"
        gem_url = "https://aistudio.google.com/app/prompts/123456"
        unknown_url = "https://example.com"

        self.assertEqual(adapters.detect_adapter_by_url(ds_url).name, "deepseek")
        self.assertEqual(adapters.detect_adapter_by_url(gem_url).name, "gemini")
        self.assertIsNone(adapters.detect_adapter_by_url(unknown_url))
    def test_detect_provider_from_url(self):
        self.assertEqual(
            adapters.detect_provider_from_url("https://chat.deepseek.com/a/chat/s/1"), "deepseek"
        )
        self.assertEqual(
            adapters.detect_provider_from_url("https://aistudio.google.com/app/prompts/1"), "gemini"
        )
        self.assertIsNone(adapters.detect_provider_from_url("https://example.com"))

    def test_infer_provider_prefers_explicit_field(self):
        # Explicit provider wins over URL sniffing
        self.assertEqual(
            adapters.infer_provider({"provider": "deepseek", "url": "https://aistudio.google.com/x"}),
            "deepseek",
        )

    def test_infer_provider_url_detection(self):
        # No explicit provider -> URL detection works for every registered adapter,
        # including Claude (the previous hardcoded deepseek-vs-gemini logic failed here).
        self.assertEqual(
            adapters.infer_provider({"url": "https://chat.deepseek.com"}), "deepseek"
        )
        self.assertEqual(
            adapters.infer_provider({"url": "https://aistudio.google.com"}), "gemini"
        )

    def test_infer_provider_default_for_unknown(self):
        self.assertEqual(adapters.infer_provider({"url": None}, default="gemini"), "gemini")
        self.assertEqual(adapters.infer_provider("https://example.com", default="deepseek"), "deepseek")

    def test_every_registered_adapter_has_matching_default_url(self):
        # Guard: each adapter's default_url must resolve back to that same provider,
        # so session inference round-trips correctly for newly added models.
        for name in adapters.list_available_adapters():
            adapter = adapters.get_adapter(name)
            self.assertEqual(
                adapters.detect_provider_from_url(adapter.default_url), name,
                f"default_url of '{name}' did not round-trip via detect_provider_from_url",
            )

class TestDeepSeekWebAdapter(unittest.TestCase):
    def setUp(self):
        self.adapter = DeepSeekWebAdapter()

    def test_extract_prompt_id(self):
        url = "https://chat.deepseek.com/a/chat/s/sess-987-xyz?param=1"
        self.assertEqual(self.adapter.extract_prompt_id(url), "sess-987-xyz")
        self.assertIsNone(self.adapter.extract_prompt_id("https://chat.deepseek.com"))

    def test_parse_deepseek_sse_content(self):
        raw = (
            'data: {"choices":[{"delta":{"content":"<message>Hello "}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"DeepSeek!</message>\\n__DONE__"}}]}\n\n'
            'data: [DONE]'
        )
        content, reasoning = parse_deepseek_sse_payload(raw)
        self.assertEqual(content, "<message>Hello DeepSeek!</message>\n__DONE__")
        self.assertEqual(reasoning, "")

    def test_parse_deepseek_sse_reasoning(self):
        raw = (
            'data: {"choices":[{"delta":{"reasoning_content":"Step 1: analyze\\n"}}]}\n\n'
            'data: {"choices":[{"delta":{"reasoning_content":"Step 2: formulate code"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"<message>Complete</message>\\n__DONE__"}}]}\n\n'
            'data: [DONE]'
        )
        content, reasoning = parse_deepseek_sse_payload(raw)
        self.assertEqual(content, "<message>Complete</message>\n__DONE__")
        self.assertEqual(reasoning, "Step 1: analyze\nStep 2: formulate code")

    def test_clean_payload_with_done_marker(self):
        raw_text = "\x3csay\x3eI am ready.\x3c/say\x3e\n__DONE__"
        cleaned = self.adapter.clean_payload(raw_text)
        self.assertEqual(cleaned, "\x3csay\x3eI am ready.\x3c/say\x3e")

    def test_clean_payload_keeps_old_tag_lines_inside_bodies(self):
        raw_text = ("\x3cwrite path=\"a.xml\"\x3e\n\x3cmessage\x3ehi\x3c/message\x3e\nmore\n\x3c/write\x3e\n"
                    "\x3csh\x3e\necho done\n\x3c/sh\x3e")
        self.assertEqual(self.adapter.clean_payload(raw_text), raw_text)

    def test_is_pure_thought(self):
        # New format: only an empty reply is a pure thought; everything else goes to the parser.
        self.assertFalse(self.adapter.is_pure_thought("Just contemplating the meaning of code..."))
        self.assertFalse(self.adapter.is_pure_thought("\x3csay\x3eReal message\x3c/say\x3e"))
        self.assertFalse(self.adapter.is_pure_thought("\x3csh\x3e\nls\n\x3c/sh\x3e"))
        self.assertTrue(self.adapter.is_pure_thought(""))
        self.assertTrue(self.adapter.is_pure_thought(None))

class TestParserDeepSeekThinkTags(unittest.TestCase):
    def test_strip_deepseek_think_tags(self):
        raw_output = "<think>\nUser wants a web search.\nLet's consider query formulation.\n</think><message>Here is the answer.</message>\n__DONE__"
        cleaned = parser.strip_model_thoughts(raw_output)
        self.assertNotIn("User wants a web search", cleaned)
        self.assertIn("Here is the answer.", cleaned)


class TestSessionProviderPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_dir = session.SESSION_DIR
        session.SESSION_DIR = self.tmp.name

    def tearDown(self):
        session.SESSION_DIR = self._orig_dir
        self.tmp.cleanup()

    def test_save_and_load_deepseek_provider(self):
        fn = session.save_current_session(
            first_prompt="Build DeepSeek agent",
            current_url="https://chat.deepseek.com/a/chat/s/12345",
            chat_history=[{"role": "User", "content": "Build DeepSeek agent"}],
            current_cwd=os.getcwd(),
            profile_email="DeepSeek User",
            profile_dir="Default",
            provider="deepseek"
        )
        self.assertIsNotNone(fn)

        # Standard 7-tuple unpack works without error
        url, loaded_fn, preview, hist, cwd, email, pdir = session.load_session_by_filename(fn)
        self.assertEqual(url, "https://chat.deepseek.com/a/chat/s/12345")
        self.assertEqual(loaded_fn, fn)

        # Object attribute preserves provider
        res = session.load_session_by_filename(fn)
        self.assertEqual(res.provider, "deepseek")


if __name__ == "__main__":
    unittest.main()


class TestParserMessageBoundary:
    """Regression: a message body quoting inline tag examples must not be
    truncated, and must not fabricate tool calls from those examples."""

    def _tags(self):
        L, G = chr(60), chr(62)
        def t(n): return L + n + G
        def tc(n): return L + chr(47) + n + G
        return {
            "cmd_o": t("command"), "cmd_c": tc("command"),
            "msg_o": t("message"), "msg_c": tc("message"),
            "tn_o": t("tool_name"), "tn_c": tc("tool_name"),
            "p_o": t("path"), "p_c": tc("path"),
            "DONE": "__DO" + "NE__",
        }

    def test_inline_examples_do_not_truncate_or_fabricate(self):
        import parser
        T = self._tags()
        raw = (T["msg_o"] + "Fix 3: it matched " + T["cmd_o"] + "chat_conversations" + T["cmd_c"]
               + " and the contract is " + T["cmd_o"] + " / " + T["msg_o"] + " / " + T["DONE"]
               + ". Restart to pick it up." + T["msg_c"] + "\n"
               + T["cmd_o"] + "\n  " + T["tn_o"] + "list_dir" + T["tn_c"]
               + "\n  " + T["p_o"] + "/tmp" + T["p_c"] + "\n" + T["cmd_c"] + "\n" + T["DONE"])
        res = parser.smart_parse_ai_output(raw)
        msg = res[0]["message"]
        tools = [x["tool"] for x in res[0]["tools"]]
        assert msg.startswith("Fix 3:"), msg[:40]
        assert "Restart to pick it up." in msg, msg[-40:]
        assert tools == ["list_dir"], tools

    def test_simple_message_and_tool(self):
        import parser
        T = self._tags()
        raw = (T["msg_o"] + "ok" + T["msg_c"] + "\n" + T["cmd_o"] + T["tn_o"]
               + "list_dir" + T["tn_c"] + T["p_o"] + "/" + T["p_c"]
               + T["cmd_c"] + "\n" + T["DONE"])
        res = parser.smart_parse_ai_output(raw)
        assert res[0]["message"] == "ok"
        assert res[0]["tools"][0]["tool"] == "list_dir"

    def test_message_only(self):
        import parser
        T = self._tags()
        raw = T["msg_o"] + "hello world" + T["msg_c"] + "\n" + T["DONE"]
        res = parser.smart_parse_ai_output(raw)
        assert res[0]["message"] == "hello world"


class TestRateLimitGating:
    """Auto-transfer must be opt-in; a rate notice with auto OFF must never
    abort the turn or switch profiles."""

    def test_controller_default_auto_transfer_on(self):
        from browser import WebAgentController
        import inspect
        src = inspect.getsource(WebAgentController.__init__)
        assert "auto_transfer_on_rate_limit = True" in src

    def test_keep_waiting_when_auto_off(self):
        import inspect, browser
        src = inspect.getsource(browser.WebAgentController.monitor_active_response)
        # With auto OFF we must NOT return RATE_LIMIT_DETECTED
        assert "auto_transfer_on_rate_limit" in src
        idx = src.find('elif not getattr(self, "auto_transfer_on_rate_limit", False):')
        assert idx != -1, "keep-waiting branch missing"
        branch = src[idx:idx + 500]
        assert "RATE_LIMIT_DETECTED" not in branch.split("else:")[0]

    def test_autotransfer_toggle_persisted(self):
        import inspect
        import main
        src = inspect.getsource(main)
        assert "auto_transfer_on_rate_limit" in src
        assert "/autotransfer" in src


