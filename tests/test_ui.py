# Unit tests for ui.py and audio.py formatting and speech sanitizers
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import ui
import audio

class TestUIAndAudioFormatting(unittest.TestCase):
    def test_latex_and_markdown_display_sanitization(self):
        sample = r"Peak draw $\approx 170\text{mA} - 200\text{mA}$. $4 \times 72\text{mA} \approx 288\text{mA}$."
        cleaned = ui.sanitize_formatting_for_display(sample)
        self.assertIn("≈", cleaned)
        self.assertIn("×", cleaned)
        self.assertNotIn(r"\approx", cleaned)
        self.assertNotIn(r"\times", cleaned)
        self.assertNotIn(r"\text", cleaned)
        self.assertNotIn("$", cleaned)

    def test_bold_latex_display_sanitization(self):
        sample = r"Total Combined Maximum Draw: $\approx \mathbf{500\text{mA}}$ ($\mathbf{0.5\text{A}}$)."
        cleaned = ui.sanitize_formatting_for_display(sample)
        self.assertIn("**500mA**", cleaned)
        self.assertIn("**0.5A**", cleaned)

    def test_xml_tag_leak_sanitization(self):
        # New format: done marker is removed, tags written inside say text stay visible.
        sample = "Operation succeeded.\n__DONE__"
        self.assertEqual(ui.sanitize_formatting_for_display(sample), "Operation succeeded.")
        shown = ui.sanitize_formatting_for_display("Use \x3csh\x3e to run commands.")
        self.assertIn("\x3csh\x3e", shown)

    def test_audio_speech_filtering(self):
        sample = """Running build now.
```python
def broken_code():
    pass
```
Peak draw $\\approx 100\\text{mA}$."""
        spoken = audio.filter_text_for_speech(sample)
        self.assertIn("Running build now", spoken)
        self.assertNotIn("def broken_code", spoken)
        self.assertIn("approximately", spoken)
        self.assertIn("100mA", spoken)

    def test_keybindings_registered(self):
        registered_keys = set()
        for b in ui.bindings.bindings:
            for k in b.keys:
                registered_keys.add(getattr(k, 'value', str(k)))
        self.assertTrue('c-i' in registered_keys or 'tab' in registered_keys, "Tab key (c-i) must be bound")
        self.assertTrue('c-m' in registered_keys or 'enter' in registered_keys, "Enter key (c-m) must be bound")
        self.assertIn('home', registered_keys, "Home key must be bound")
        self.assertIn('c-p', registered_keys, "Ctrl+P key must be bound")
        self.assertIn('c-j', registered_keys, "c-j must be bound (Kitty sends \\n for Enter)")

    def test_cj_and_cm_both_submit(self):
        """c-j (\\n) and c-m (\\r) must call validate_and_handle, not insert text.

        Kitty terminal sends \\n (ControlJ) for plain Enter when the KKP is not
        fully active, so both keys MUST trigger submission.
        """
        from prompt_toolkit.buffer import Buffer
        submitted = []

        class FakeBuffer(Buffer):
            def validate_and_handle(self):
                submitted.append(True)
            def apply_completion(self, c):
                pass

        class FakeEvent:
            def __init__(self):
                self.current_buffer = FakeBuffer()

        # Find c-m and c-j handlers in bindings
        cm_handler = None
        cj_handler = None
        for b in ui.bindings.bindings:
            keys = tuple(getattr(k, 'value', str(k)) for k in b.keys)
            if keys == ('c-m',):
                cm_handler = b.handler
            if keys == ('c-j',):
                cj_handler = b.handler

        self.assertIsNotNone(cm_handler, "c-m binding must exist")
        self.assertIsNotNone(cj_handler, "c-j binding must exist")

        cm_handler(FakeEvent())
        self.assertEqual(len(submitted), 1, "c-m must call validate_and_handle")

        cj_handler(FakeEvent())
        self.assertEqual(len(submitted), 2, "c-j must also call validate_and_handle (not insert newline)")

    def test_ansi_sequences_enter_variants_mapped(self):
        """Kitty KKP plain-Enter sequences must map to ControlM (submit), not ControlJ."""
        from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
        from prompt_toolkit.keys import Keys

        # Plain Enter sequences must trigger submit (ControlM)
        for seq in ['\x1b[13u', '\x1b[13;1u', '\x1b[27;1;13~']:
            self.assertEqual(
                ANSI_SEQUENCES.get(seq), Keys.ControlM,
                f"Kitty plain-Enter sequence {repr(seq)} must map to ControlM"
            )

        # Shift/Ctrl+Enter sequences must NOT map to ControlM (they should insert newline)
        for seq in ['\x1b[13;2u', '\x1b[13;5u', '\x1b[27;2;13~', '\x1b[27;5;13~']:
            mapped = ANSI_SEQUENCES.get(seq)
            self.assertNotEqual(
                mapped, Keys.ControlM,
                f"Modified Enter {repr(seq)} must NOT submit (maps to {mapped})"
            )

    def test_tab_manager_auto_complete_and_cycle(self):
        from prompt_toolkit.buffer import Buffer
        b = Buffer()
        b.insert_text('/co')
        class FakeEvent:
            current_buffer = b
        ui.tab_manager.handle_tab(FakeEvent())
        self.assertEqual(b.text, '/continue')

        # Test non-slash auto-completion
        b = Buffer()
        b.insert_text('cont')
        FakeEvent.current_buffer = b
        ui.tab_manager.handle_tab(FakeEvent())
        self.assertEqual(b.text, '/continue')

        # Test cycling on ambiguous command
        b = Buffer()
        b.insert_text('/n')
        FakeEvent.current_buffer = b
        ui.tab_manager.handle_tab(FakeEvent())
        first = b.text
        self.assertIn(first, ['/new', '/next', '/name'])
        ui.tab_manager.handle_tab(FakeEvent())
        second = b.text
        self.assertIn(second, ['/new', '/next', '/name'])
        self.assertNotEqual(first, second)

    def test_audio_sentence_splitting(self):
        sample = "Hello Dr. Smith! Here is v1.2 of the engine, e.g. for testing. Wait... does it work? Yes! 3.14 is pi."
        sentences = audio.split_into_sentences(sample)
        self.assertEqual(len(sentences), 5)
        self.assertEqual(sentences[0], "Hello Dr. Smith!")
        self.assertEqual(sentences[1], "Here is v1.2 of the engine, e.g. for testing.")
        self.assertEqual(sentences[2], "Wait... does it work?")
        self.assertEqual(sentences[3], "Yes!")
        self.assertEqual(sentences[4], "3.14 is pi.")

    def test_audio_numbered_list_splitting(self):
        sample = "1. First step.\n2. Second step.\n3. Third step."
        sentences = audio.split_into_sentences(sample)
        self.assertEqual(len(sentences), 3)
        self.assertEqual(sentences[0], "1. First step.")
        self.assertEqual(sentences[1], "2. Second step.")
        self.assertEqual(sentences[2], "3. Third step.")

    def test_audio_queue_reset_and_epoch_increment(self):
        audio.stop_audio()
        epoch_before = audio.SPEECH_EPOCH
        audio.speak("First sentence. Second sentence.")
        self.assertGreater(audio.SPEECH_EPOCH, epoch_before)
        # Calling stop_audio clears the queues and increments epoch again
        audio.stop_audio()
        self.assertTrue(audio.TEXT_QUEUE.empty())
        self.assertTrue(audio.AUDIO_QUEUE.empty())
        self.assertFalse(audio.is_speaking())

if __name__ == "__main__":
    unittest.main()
