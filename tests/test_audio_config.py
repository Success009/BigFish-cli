# tests/test_audio_config.py - speech toggle must never wipe other settings in .cli_config.json
import json, os, sys, tempfile, unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import paths  # noqa
import audio


class TestAudioSaveConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "cfg.json")
        self._old_path = audio.CONFIG_FILE
        self._old_flag = audio.SPEECH_ENABLED
        audio.CONFIG_FILE = self.path

    def tearDown(self):
        audio.CONFIG_FILE = self._old_path
        audio.SPEECH_ENABLED = self._old_flag

    def _read(self):
        with open(self.path) as f:
            return json.load(f)

    def test_other_keys_survive(self):
        with open(self.path, "w") as f:
            json.dump({"finish_action": "raise+sound", "finish_sound": "ping",
                       "pause_media": True, "auto_transfer_on_rate_limit": False}, f)
        audio.SPEECH_ENABLED = True
        audio.save_config()
        cfg = self._read()
        self.assertEqual(cfg["finish_action"], "raise+sound")
        self.assertEqual(cfg["finish_sound"], "ping")
        self.assertIs(cfg["pause_media"], True)
        self.assertIs(cfg["auto_transfer_on_rate_limit"], False)
        self.assertIs(cfg["speech_enabled"], True)

    def test_missing_file_is_created(self):
        audio.SPEECH_ENABLED = False
        audio.save_config()
        self.assertEqual(self._read(), {"speech_enabled": False})

    def test_corrupt_file_does_not_crash(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        audio.SPEECH_ENABLED = True
        audio.save_config()
        self.assertIs(self._read()["speech_enabled"], True)

    def test_non_dict_json_does_not_crash(self):
        with open(self.path, "w") as f:
            f.write("[1, 2]")
        audio.SPEECH_ENABLED = True
        audio.save_config()
        self.assertEqual(self._read(), {"speech_enabled": True})

    def test_toggle_speech_keeps_other_keys(self):
        with open(self.path, "w") as f:
            json.dump({"finish_action": "sound"}, f)
        audio.SPEECH_ENABLED = False
        audio.toggle_speech()
        cfg = self._read()
        self.assertEqual(cfg["finish_action"], "sound")
        self.assertIs(cfg["speech_enabled"], True)

    def test_no_temp_file_left_behind(self):
        audio.SPEECH_ENABLED = True
        audio.save_config()
        self.assertFalse(os.path.exists(self.path + ".tmp"))


if __name__ == "__main__":
    unittest.main()
