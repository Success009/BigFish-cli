# Unit tests for parser.py module
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import parser

class TestParserModule(unittest.TestCase):


    def test_parse_plain_text_fallback(self):
        raw_output = "Hello this is a plain text response."
        parsed = parser.smart_parse_ai_output(raw_output)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["message"], "Hello this is a plain text response.")



    def test_strip_model_thoughts_bold_sections(self):
        raw_thought = """**Analyzing Missing Tool Output**

I'm now focusing on the prompt "do you responce again." My current thinking zeroes in on scenarios where the tool's output is absent.

**Pinpointing Desired Response**

I'm now zeroing in on the user's implicit demand for a solution.

**Deducing Tool Behavior**

I'm now back at the problem, and focusing on the user's feedback."""
        cleaned = parser.strip_model_thoughts(raw_thought)
        self.assertEqual(cleaned, "")

    def test_strip_model_thoughts_xml_tags(self):
        raw = "<thought>Thinking about this problem...</thought><message>Final result</message>"
        cleaned = parser.strip_model_thoughts(raw)
        self.assertEqual(cleaned, "<message>Final result</message>")






    def test_is_pure_thought_text(self):
        self.assertTrue(parser.is_pure_thought_text("<message>\n**Assessing Server Compatibility**"))
        self.assertTrue(parser.is_pure_thought_text("**Pinpointing Desired Response**\n\nI'm now zeroing in on the user's implicit demand."))
        self.assertTrue(parser.is_pure_thought_text("The harness might have failed to intercept the <execute_tool> call."))
        self.assertFalse(parser.is_pure_thought_text("I have examined the issue and updated the delay in DevToolsClient.java."))
        self.assertFalse(parser.is_pure_thought_text("<message>Here is the fix.</message><execute_tool><tool_name>read_file</tool_name></execute_tool>"))


    def test_clean_response_payload_filters_thoughts_and_preserves_valid(self):
        from browser import clean_response_payload
        # Thought only blocks should clean to empty
        self.assertEqual(clean_response_payload("<message>\n**Assessing Server Compatibility**"), "")
        self.assertEqual(clean_response_payload("The harness might have failed to intercept the <execute_tool> call."), "")
        
        # Valid blocks with thought preamble should strip preamble and preserve tags
        valid_input = """**Analyzing Missing Tool Output**

I will now read the file.

<message>Reading the file.</message>
<execute_tool>
  <tool_name>read_file</tool_name>
  <path>test.py</path>
</execute_tool>
__DONE__"""
        cleaned = clean_response_payload(valid_input)
        self.assertIn("<message>Reading the file.</message>", cleaned)
        self.assertIn("<execute_tool>", cleaned)
    def test_quoted_thought_tags_and_plain_done_preserved(self):
        from browser import clean_response_payload
        stream_payload = ("more_vert Model 9:28 AM Thoughts\nAnalyzing the Request\n"
                          "thinking about how to reply.\n\n"
                          "Collapse to hide model thoughts chevron_right\n\n"
                          "\x3csay\x3eBased on modern anti-cheat architectures, rapid combat is detected.\n\n"
                          "1. Packet Frequency.\x3c/say\x3e\n__DONE__")
        cleaned = clean_response_payload(stream_payload)
        self.assertNotIn("Analyzing the Request", cleaned)
        self.assertIn("Packet Frequency", cleaned)

        parsed = parser.smart_parse_ai_output(cleaned)
        self.assertEqual(parsed[0]["status"], "complete")
        self.assertIn("Based on modern anti-cheat", parsed[0]["message"])

    def test_internal_done_in_message_preserved(self):
        raw_output = "<message>Step 1.\nDone with installation.\nStep 2.</message>\n__DONE__"
        parsed = parser.smart_parse_ai_output(raw_output)
        self.assertEqual(len(parsed), 1)
        self.assertIn("Done with installation.", parsed[0]["message"])
        self.assertIn("Step 2.", parsed[0]["message"])


    def test_clean_response_payload_internal_done(self):
        from browser import clean_response_payload
        raw_output = "\x3csay\x3eStatus report:\nDone with phase 1.\nStarting phase 2.\x3c/say\x3e\n__DONE__"
        cleaned = clean_response_payload(raw_output)
        self.assertIn("Done with phase 1.", cleaned)
        self.assertIn("Starting phase 2.", cleaned)
        self.assertNotIn("__DONE__", cleaned)


class TestDsmlStripping(unittest.TestCase):
    """Regression tests for _strip_dsml — DeepSeek-native DSML tag removal."""

    def test_dsml_pipe_tags_removed(self):
        """<｜｜DSML｜｜_processing> and close variants must be stripped."""
        raw = (
            "<｜｜DSML｜｜_processing>\n"
            "<message>Hello</message>\n"
            "</｜｜DSML｜｜>\n"
            "__DONE__"
        )
        result = parser._strip_dsml(raw)
        self.assertNotIn("DSML", result)
        self.assertIn("<message>Hello</message>", result)

    def test_parameter_spam_removed(self):
        """Trailing </parameter> spam must be stripped without touching real content."""
        raw = (
            "<command>\n"
            "  <tool_name>run_command</tool_name>\n"
            "  <command>echo hi</command>\n"
            "</command>\n"
            "</parameter></parameter></parameter>\n"
            "__DONE__"
        )
        result = parser._strip_dsml(raw)
        self.assertNotIn("</parameter>", result)
        self.assertIn("<command>echo hi</command>", result)


    def test_deepseek_special_tokens_removed(self):
        """<｜tool▁call▁begin｜> style tokens must be stripped."""
        raw = "<｜tool▁call▁begin｜><message>Hi</message><｜tool▁outputs▁begin｜>__DONE__"
        result = parser._strip_dsml(raw)
        self.assertNotIn("｜tool", result)
        self.assertIn("<message>Hi</message>", result)


if __name__ == "__main__":
    unittest.main()



class TestMessageWithInnerCdataSnippets(unittest.TestCase):
    """Regression: When a <message> contains <![CDATA[ and ]]> snippets inside prose
    (e.g. discussing CDATA bugs), the parser must NOT discard everything outside
    that first CDATA match."""

    def test_message_with_inner_cdata_not_truncated(self):
        raw = (
            "<message>Top of message.\n"
            "Here is some text: <![CDATA[inner snippet]]> and more text.\n"
            "Bottom of message.</message>\n"
            "__DONE__"
        )
        result = parser.smart_parse_ai_output(raw)
        self.assertNotEqual(result, "PARSE_ERROR")
        msg = result[0]["message"]
        self.assertIn("Top of message.", msg)
        self.assertIn("inner snippet", msg)
        self.assertIn("Bottom of message.", msg)


class TestNestedCdataAndSameLineTerminator(unittest.TestCase):
    """Regression: Bug A (opener + terminator on same line) and
    Bug B (nested <![CDATA[ inside trap stack) must not truncate content."""




    def test_message_containing_inner_message_confirm_tag_not_cut_off(self):
        raw = (
            "<message>\n"
            "# Full Report\n"
            "B4 — Elevation gate loop.\n"
            "The gate says \"send ONLY <message>CONFIRM</message>,\" but that message is parsed as a user turn.\n"
            "B5 — Protection is asymmetric.\n"
            "Section C. Lean prompt.\n"
            "</message>\n"
            "DONE"
        )
        parsed = parser.smart_parse_ai_output(raw)
        self.assertNotEqual(parsed, "PARSE_ERROR")
        msg = parsed[0]["message"]
        self.assertIn("Full Report", msg)
        self.assertIn("Elevation gate loop", msg)
        self.assertIn("CONFIRM", msg)
        self.assertIn("Protection is asymmetric", msg)
        self.assertIn("Section C. Lean prompt.", msg)



