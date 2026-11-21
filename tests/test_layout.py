# Guards against path lookups breaking when modules move between folders
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import paths  # noqa

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class TestLayoutPaths(unittest.TestCase):
    def test_user_data_dir_uses_sandbox_when_present(self):
        import profiles
        sandbox = os.path.join(ROOT, "~", ".config", "google-chrome")
        if os.path.exists(sandbox):
            self.assertEqual(os.path.abspath(profiles.get_chrome_user_data_dir()), sandbox)
        else:
            self.assertEqual(profiles.get_chrome_user_data_dir(), os.path.expanduser("~/.config/google-chrome"))

    def test_skills_dir_is_project_skills(self):
        import tools
        self.assertEqual(os.path.abspath(tools.SKILLS_DIR), os.path.join(ROOT, "skills"))

    def test_main_reexports_moved_names(self):
        import main
        for name in ("load_cli_config", "save_cli_config", "get_all_available_profiles",
                     "compile_handover_packet", "needs_elevation_confirm", "restart_browser",
                     "launch_chrome_process", "is_chrome_cdp_alive", "load_session_by_filename"):
            self.assertTrue(callable(getattr(main, name)), name)


if __name__ == "__main__":
    unittest.main()
