# Unit tests for main.py module
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main

class TestMainModule(unittest.TestCase):
    def test_imports_and_functions_exist(self):
        self.assertTrue(callable(main.compile_handover_packet))
        self.assertTrue(callable(main.restart_browser))
        self.assertTrue(callable(main.get_all_available_profiles))
        self.assertTrue(hasattr(main, 'tokens'))
        self.assertTrue(callable(main.estimate_tokens))

    def test_compile_handover_packet(self):
        history = [
            {"role": "User", "content": "Build feature X"},
            {"role": "AI", "content": "I am building feature X"},
            {"role": "System", "content": "Ran 'write_file'. Result:\nSuccessfully wrote 50 characters to main.py."}
        ]
        packet = main.compile_handover_packet(history, ["Default"], "Default")
        self.assertIn("MODEL CONTEXT TRANSFER & HANDOVER BLUEPRINT", packet)
        self.assertIn("main.py", packet)
        self.assertIn("Build feature X", packet)

    def test_compile_handover_packet_anti_hallucination_directives(self):
        history = [
            {"role": "User", "content": "Fix bug in backend"},
            {"role": "AI", "content": "I will examine backend.py"},
            {"role": "System", "content": "Ran 'read_file' on 'backend.py'. Result:\ndef broken(): pass"}
        ]
        packet = main.compile_handover_packet(history, ["Default"], "Default")
        # Verify strict separation of agent vs harness
        self.assertIn("YOU ARE THE AI AGENT, NOT THE SYSTEM HARNESS", packet)
        # Verify syntax enforcement and the stop-after-last-command rule
        self.assertIn("OUTPUT SYNTAX (ONE TAG PER TOOL)", packet)
        self.assertIn("Stop after your last tag", packet)
        # Verify code inspection grounding directive
        self.assertIn("INSPECT RELEVANT CODE BEFORE CONTINUING", packet)
        # Verify clean action record formatting without raw stdout dump
        self.assertIn("[Action Executed by Previous Model]", packet)
        self.assertIn("- Tool: `read_file`", packet)

    def test_compile_handover_packet_cumulative_history_backfill(self):
        # Current chat history is very short (e.g. immediately after a transfer)
        chat_history = [
            {"role": "User", "content": "Continue with step 2"}
        ]
        # Cumulative history contains the earlier turns
        cumulative = [
            {"role": "User", "content": "Original goal: build authentication"},
            {"role": "AI", "content": "Implemented auth tokens"},
            {"role": "System", "content": "Ran 'write_file' on 'auth.py'. Result:\nSuccessfully wrote 100 characters to auth.py."}
        ]
        packet = main.compile_handover_packet(chat_history, ["Default"], "Default", cumulative_history=cumulative)
        self.assertIn("Original goal: build authentication", packet)
        self.assertIn("auth.py", packet)
        self.assertIn("Continue with step 2", packet)

    def test_controller_stop_generation_exists(self):
        from browser import AIStudioController
        self.assertTrue(hasattr(AIStudioController, 'stop_generation'))
        self.assertTrue(hasattr(AIStudioController, 'is_page_generating'))

    def test_ui_slash_completer_includes_continue(self):
        import ui
        completer = ui.SlashCommandCompleter()
        self.assertIn('/continue', completer.commands)

    def test_debuglog(self):
        import debuglog
        import tempfile
        from unittest import mock
        _tmp = os.path.join(tempfile.mkdtemp(), 'debug_test.log')
        _patcher = mock.patch.object(debuglog, '_LOG_PATH', _tmp)
        _patcher.start()
        self.addCleanup(_patcher.stop)
        debuglog.init()
        debuglog.log("TEST", "hello world")
        log_file = debuglog.path()
        self.assertTrue(os.path.exists(log_file))
        with open(log_file, "r") as f:
            content = f.read()
        self.assertIn("[TEST] hello world", content)

    def test_cli_config_persistence(self):
        import tempfile
        original_config = main.CLI_CONFIG_FILE
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            main.CLI_CONFIG_FILE = tmp_path
            main.save_cli_config({"manual_mode": True, "test_key": "123"})
            loaded = main.load_cli_config()
            self.assertTrue(loaded.get("manual_mode"))
            self.assertEqual(loaded.get("test_key"), "123")
            main.save_cli_config({"manual_mode": False})
            self.assertFalse(main.load_cli_config().get("manual_mode"))
        finally:
            main.CLI_CONFIG_FILE = original_config
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_controller_modular_methods(self):
        from browser import AIStudioController
        self.assertTrue(hasattr(AIStudioController, 'paste_prompt_to_chat'))
        self.assertTrue(hasattr(AIStudioController, 'trigger_submit_if_needed'))
        self.assertTrue(hasattr(AIStudioController, 'monitor_active_response'))
        self.assertTrue(hasattr(AIStudioController, 'send_prompt_and_get_response'))
        self.assertTrue(hasattr(AIStudioController, 'scroll_chat_to_bottom'))
        self.assertTrue(hasattr(AIStudioController, 'get_chat_box'))
        self.assertTrue(hasattr(AIStudioController, 'wait_for_chat_ready'))

    def test_sandboxed_path_traversal_detection(self):
        cwd = "/home/user/bigfish/projects/test"
        # Relative traversal that escapes project root
        escape_path = os.path.abspath(os.path.join(cwd, "../../../../tmp"))
        self.assertFalse(main.is_sandboxed_path("../../../../tmp", escape_path, cwd))

        # Relative path within project
        inside_path = os.path.abspath(os.path.join(cwd, "src/index.js"))
        self.assertTrue(main.is_sandboxed_path("src/index.js", inside_path, cwd))

        # Absolute paths are not relative traversals
        self.assertTrue(main.is_sandboxed_path("/tmp/foo.txt", "/tmp/foo.txt", cwd))

        # Absolute prefix containing .. escaping cwd
        ws_cwd = "/home/user/bigfish/projects/test/workspace"
        abs_escape = "/home/user/bigfish/projects/test/workspace/../../../../tmp/r5_traversal.txt"
        self.assertFalse(main.is_sandboxed_path(abs_escape, os.path.abspath(abs_escape), ws_cwd))

        # Absolute prefix containing .. escaping workspace to parent project
        parent_escape = "/home/user/bigfish/projects/test/workspace/../r5_escape.txt"
        self.assertFalse(main.is_sandboxed_path(parent_escape, os.path.abspath(parent_escape), ws_cwd))

        # Relative .. escaping workspace
        rel_escape = "../r5_escape.txt"
        self.assertFalse(main.is_sandboxed_path(rel_escape, os.path.abspath(os.path.join(ws_cwd, rel_escape)), ws_cwd))

        # Safe traversal that stays inside cwd
        safe_traversal = "sub/../valid.txt"
        self.assertTrue(main.is_sandboxed_path(safe_traversal, os.path.abspath(os.path.join(ws_cwd, safe_traversal)), ws_cwd))

if __name__ == "__main__":
    unittest.main()


