# tests/test_gemini_parser.py - structure-aware Gemini (AI Studio) stream parsing and busy-check guard.
# Bodies are synthetic but follow the live GenerateContent shape observed on 2026-11-11:
#   data[0][i][0][0][0][0][0] = slots; slots[1] = text; slots[12] truthy = thinking.
import inspect
import json
import unittest

import adapters.gemini as g

LT = "<"
PREFIX = ")]}'\n"


def slots(text, thought=False, blob=None):
    s = [None] * 15
    s[1] = text
    if thought:
        s[12] = 1
    if blob is not None:
        s[14] = blob
    return s


def entry(sl):
    # five nested lists, so that entry[0][0][0][0][0] is the slots list
    return [[[[[sl]]]]]


def body(chunks, prefix=True):
    data = [[entry(c) for c in chunks]]
    return (PREFIX if prefix else "") + json.dumps(data, ensure_ascii=False)


class TestExtractReplyChunks(unittest.TestCase):
    def test_reply_only(self):
        b = body([slots("hello "), slots("world")])
        self.assertEqual(g.extract_reply_chunks(b), "hello world")

    def test_thinking_skipped(self):
        b = body([slots("**Plan**", thought=True), slots("answer")])
        self.assertEqual(g.extract_reply_chunks(b), "answer")

    def test_blob_chunk_with_empty_text_skipped(self):
        b = body([slots("ok"), slots("", blob="Eus" + "A" * 300)])
        self.assertEqual(g.extract_reply_chunks(b), "ok")

    def test_more_than_five_chunks(self):
        chunks = [slots("t%d " % i, thought=(i % 2 == 0)) for i in range(9)]
        out = g.extract_reply_chunks(body(chunks))
        self.assertEqual(out, "t1 t3 t5 t7 ")

    def test_unicode_dollar_backslash_survive(self):
        text = "caf\u00e9 \u4f60\u597d \U0001F41F \u2192 $5 and $10 back\\slash {x}"
        out = g.extract_reply_chunks(body([slots(text)]))
        self.assertEqual(out, text)

    def test_tool_block_reply_kept_whole(self):
        blk = (LT + "command>\n" + LT + "tool_name>read_file" + LT + "/tool_name>\n"
               + LT + "path>a.py" + LT + "/path>\n" + LT + "/command>")
        parts = [blk[:20], blk[20:]]
        out = g.extract_reply_chunks(body([slots(parts[0]), slots(parts[1])]))
        self.assertEqual(out, blk)

    def test_thinking_only_returns_empty_string_not_none(self):
        out = g.extract_reply_chunks(body([slots("only thoughts", thought=True)]))
        self.assertEqual(out, "")

    def test_works_without_prefix(self):
        self.assertEqual(g.extract_reply_chunks(body([slots("x")], prefix=False)), "x")

    def test_partial_body_returns_none(self):
        b = body([slots("hello"), slots("world")])
        self.assertIsNone(g.extract_reply_chunks(b[:len(b) // 2]))

    def test_non_json_and_empty_return_none(self):
        self.assertIsNone(g.extract_reply_chunks("not json at all"))
        self.assertIsNone(g.extract_reply_chunks(""))

    def test_other_shapes_return_none(self):
        self.assertIsNone(g.extract_reply_chunks(json.dumps({"candidates": []})))
        self.assertIsNone(g.extract_reply_chunks(json.dumps([[["x"]]])))
        self.assertIsNone(g.extract_reply_chunks(json.dumps([[]])))

    def test_slot_with_non_string_text_ignored(self):
        s = [None, 123] + [None] * 13
        self.assertIsNone(g.extract_reply_chunks(body([s])))


class TestParseGeminiStreamPayload(unittest.TestCase):
    def test_uses_structure_and_drops_metadata(self):
        chunks = [slots("**Thinking**", thought=True), slots("final text"), slots("", blob="B" * 400)]
        self.assertEqual(g.parse_gemini_stream_payload(body(chunks)), "final text")

    def test_empty_input(self):
        self.assertEqual(g.parse_gemini_stream_payload(""), "")
        self.assertEqual(g.parse_gemini_stream_payload(None), "")

    def test_old_shape_still_parsed_by_fallback(self):
        old = json.dumps({"candidates": [{"content": {"parts": [{"text": "legacy reply"}]}}]})
        self.assertIn("legacy reply", g.parse_gemini_stream_payload(old))


class TestBusyCheckSource(unittest.TestCase):
    """The JS runs in Chrome, so only guard its source: AI Studio keeps Run enabled while generating
    and disables it when the box is empty, so a disabled Run button must never count as busy."""

    def setUp(self):
        self.src = inspect.getsource(g.GeminiWebAdapter.is_generating)

    def test_disabled_run_button_is_not_busy(self):
        self.assertNotIn("runBtn.disabled", self.src)
        self.assertNotIn("aria-disabled", self.src)

    def test_run_label_swap_counts_as_busy(self):
        self.assertIn("progress_activity", self.src)


class TestCountTokensBody(unittest.TestCase):
    def test_live_shape(self):
        self.assertEqual(g.parse_counttokens_body("[328,[],[[[327],1]],null,null,[[1,328]]]"), 328)

    def test_prefix_and_zero(self):
        self.assertEqual(g.parse_counttokens_body(PREFIX + "[0,[]]"), 0)

    def test_bad_shapes_return_none(self):
        for bad in ("", "nope", "{}", "[]", "[\"x\"]", "[true]", "[-1]", None):
            self.assertIsNone(g.parse_counttokens_body(bad))


if __name__ == "__main__":
    unittest.main()
