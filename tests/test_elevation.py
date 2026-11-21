import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import unittest
import main as m

class TestNeedsElevationConfirm(unittest.TestCase):
    def test_sudo_detected(self):
        self.assertTrue(m.needs_elevation_confirm("sudo cp file /etc/hosts"))
        self.assertTrue(m.needs_elevation_confirm("echo hi && sudo rm -rf /var"))

    def test_dangerous_rm_detected(self):
        self.assertTrue(m.needs_elevation_confirm("rm -rf /etc"))
        self.assertTrue(m.needs_elevation_confirm("rm -rf /usr/lib"))
        # bare ~ (home root wipe) should flag
        self.assertTrue(m.needs_elevation_confirm("rm -rf ~"))

    def test_system_write_detected(self):
        self.assertTrue(m.needs_elevation_confirm("echo test > /etc/hosts"))
        self.assertTrue(m.needs_elevation_confirm("tee /usr/local/bin/script"))

    def test_safe_commands_pass(self):
        self.assertFalse(m.needs_elevation_confirm("ls -la"))
        self.assertFalse(m.needs_elevation_confirm("rm -rf /home/user/projects/test"))
        self.assertFalse(m.needs_elevation_confirm("echo hello > /home/user/file.txt"))
        self.assertFalse(m.needs_elevation_confirm("git status"))
        self.assertFalse(m.needs_elevation_confirm("python3 main.py"))

if __name__ == "__main__":
    unittest.main()
