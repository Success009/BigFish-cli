"""Tests for interface/notify.py (turn-finished notification).

No X11, kitty, audio device or real config is used: the decision logic takes
plain dicts, and config helpers take in-memory load/save functions.
Sections mirror notify.py: CONFIG, DECISION, RAISE, SYNTHESIS, PURITY.
"""
import os
import sys
import tempfile
import unittest
import wave
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import paths  # noqa: F401  (sets up flat imports like main.py)
import notify


def facts(**kw):
    """Active window = some other app (wid 5, pid 100); our terminal wid 9; our chrome pid 200."""
    base = dict(active_wid=5, active_pid=100, term_wid=9, chrome_pid=200, ancestor_pids={1, 2})
    base.update(kw)
    return base


class MemCfg:
    def __init__(self, **init):
        self.d = dict(init)

    def load(self):
        return dict(self.d)

    def save(self, upd):
        self.d.update(upd)


class TestConfig(unittest.TestCase):
    def test_next_in_wraps(self):
        self.assertEqual(notify.next_in(["a", "b", "c"], "c"), "a")
        self.assertEqual(notify.next_in(["a", "b", "c"], "a"), "b")

    def test_next_in_unknown_goes_first(self):
        self.assertEqual(notify.next_in(["a", "b"], "zzz"), "a")

    def test_defaults_on_empty_or_bad_config(self):
        self.assertEqual(notify.get_action(MemCfg().load), notify.DEFAULT_ACTION)
        self.assertEqual(notify.get_action(MemCfg(finish_action="bogus").load), notify.DEFAULT_ACTION)
        self.assertEqual(notify.get_sound(MemCfg().load), notify.DEFAULT)
        self.assertEqual(notify.get_sound(MemCfg(finish_sound="bogus").load), notify.DEFAULT)

    def test_cycle_action_visits_all_and_persists(self):
        c = MemCfg(finish_action="off")
        seen = [notify.cycle_action(c.load, c.save) for _ in range(4)]
        self.assertEqual(seen, ["sound", "raise", "raise+sound", "off"])
        self.assertEqual(c.d["finish_action"], "off")

    def test_cycle_sound_visits_all(self):
        c = MemCfg()
        seen = {notify.cycle_sound(c.load, c.save) for _ in range(len(notify.SOUNDS))}
        self.assertEqual(seen, set(notify.SOUNDS))


class TestDecision(unittest.TestCase):
    def test_other_window_in_front_fires(self):
        self.assertTrue(notify.should_notify("sound", facts(), True))
        self.assertTrue(notify.should_notify("raise", facts(), True))

    def test_terminal_in_front_silent(self):
        f = facts(active_wid=9)
        self.assertFalse(notify.should_notify("sound", f, True))
        self.assertFalse(notify.should_notify("raise", f, True))

    def test_our_chrome_in_front_silent(self):
        self.assertFalse(notify.should_notify("sound", facts(active_pid=200), True))

    def test_different_chrome_profile_does_not_count_as_ours(self):
        # another Chrome instance has a different pid than ours (200) -> not "ours", so we notify
        self.assertTrue(notify.should_notify("sound", facts(active_pid=777), True))

    def test_no_chrome_running_does_not_match_unknown_pid(self):
        self.assertTrue(notify.should_notify("sound", facts(chrome_pid=None), True))

    def test_windowid_unset_uses_ancestor_fallback(self):
        f = facts(term_wid=None, active_pid=2)
        self.assertFalse(notify.should_notify("sound", f, True))
        self.assertTrue(notify.should_notify("sound", facts(term_wid=None, active_pid=999), True))

    def test_windowid_set_ignores_ancestor_fallback(self):
        # with a known terminal window id, an ancestor pid on another window must not suppress
        self.assertTrue(notify.should_notify("sound", facts(active_pid=2), True))

    def test_off_never_fires(self):
        self.assertFalse(notify.should_notify("off", facts(), True))

    def test_unknown_action_never_fires(self):
        self.assertFalse(notify.should_notify("bogus", facts(), True))

    def test_unknown_focus_never_fires(self):
        self.assertFalse(notify.should_notify("sound", None, True))
        self.assertFalse(notify.should_notify("raise", None, True))

    def test_nothing_focused_fires(self):
        self.assertTrue(notify.should_notify("sound", facts(active_wid=None, active_pid=None), True))

    def test_muted_blocks_sound_but_not_raise(self):
        self.assertFalse(notify.should_notify("sound", facts(), False))
        self.assertTrue(notify.should_notify("raise", facts(), False))


class TestRaise(unittest.TestCase):
    def test_targets_only_this_window_id(self):
        self.assertEqual(notify.raise_commands(33554446), [["xdotool", "windowactivate", "33554446"]])

    def test_no_window_id_no_commands(self):
        self.assertEqual(notify.raise_commands(None), [])
        self.assertEqual(notify.raise_commands(0), [])

    def test_never_uses_kitty_remote_control(self):
        for cmd in notify.raise_commands(42):
            self.assertNotIn("kitty", cmd[0])
            self.assertNotIn("@", " ".join(cmd))

    def test_raise_terminal_without_windowid_runs_nothing(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WINDOWID", None)
            with mock.patch.object(notify, "_run") as run:
                notify.raise_terminal()
                run.assert_not_called()


class TestSynthesis(unittest.TestCase):
    def test_every_chime_is_short_valid_wav(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(notify, "SOUND_DIR", d):
                for name in notify.SOUNDS:
                    with wave.open(notify.sound_path(name)) as w:
                        secs = w.getnframes() / w.getframerate()
                        self.assertGreater(secs, 0.1, name)
                        self.assertLess(secs, 0.5, name)

    def test_unknown_name_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(notify, "SOUND_DIR", d):
                self.assertTrue(notify.sound_path("nope").endswith(f"finish_{notify.DEFAULT}.wav"))


class TestPurity(unittest.TestCase):
    """PROTECTED: the decision functions must never touch the system."""

    def test_decision_makes_no_system_calls(self):
        with mock.patch.object(notify, "_run", side_effect=AssertionError("system call")), \
             mock.patch.object(notify.subprocess, "run", side_effect=AssertionError("system call")), \
             mock.patch.object(notify.subprocess, "Popen", side_effect=AssertionError("system call")):
            notify.is_front(facts())
            notify.should_notify("sound", facts(), True)
            notify.should_notify("raise", None, False)

    def test_on_turn_finished_off_does_nothing(self):
        c = MemCfg(finish_action="off")
        with mock.patch.object(notify.threading, "Thread") as t:
            notify.on_turn_finished(c.load)
            t.assert_not_called()


class TestRaisePlusSound(unittest.TestCase):
    def test_fires_when_other_window_in_front(self):
        self.assertTrue(notify.should_notify("raise+sound", facts(), True))

    def test_silent_when_terminal_or_our_chrome_in_front(self):
        self.assertFalse(notify.should_notify("raise+sound", facts(active_wid=9), True))
        self.assertFalse(notify.should_notify("raise+sound", facts(active_pid=200), True))

    def test_muted_still_fires_so_raise_happens(self):
        self.assertTrue(notify.should_notify("raise+sound", facts(), False))

    def test_unknown_focus_never_fires(self):
        self.assertFalse(notify.should_notify("raise+sound", None, True))

    def test_is_in_action_list_after_raise(self):
        self.assertEqual(notify.ACTIONS, ["off", "sound", "raise", "raise+sound"])


class TestPauseMedia(unittest.TestCase):
    def test_pauses_only_playing_players(self):
        cmds = notify.pause_commands([("firefox.instance_1_54", "Playing"), ("vlc", "Paused"), ("spotify", "Stopped")])
        self.assertEqual(cmds, [["playerctl", "-p", "firefox.instance_1_54", "pause"]])

    def test_nothing_playing_no_commands(self):
        self.assertEqual(notify.pause_commands([("vlc", "Paused")]), [])
        self.assertEqual(notify.pause_commands([]), [])

    def test_skips_our_own_chrome(self):
        players = [("chromium.instance200", "Playing"), ("chromium.instance300", "Playing")]
        self.assertEqual(notify.pause_commands(players, exclude_pid=200),
                         [["playerctl", "-p", "chromium.instance300", "pause"]])

    def test_pause_media_default_on_and_configurable(self):
        self.assertTrue(notify.get_pause_media(MemCfg().load))
        self.assertFalse(notify.get_pause_media(MemCfg(pause_media=False).load))

    def test_pause_commands_is_pure(self):
        with mock.patch.object(notify, "_run", side_effect=AssertionError("system call")), \
             mock.patch.object(notify.subprocess, "run", side_effect=AssertionError("system call")):
            notify.pause_commands([("x", "Playing")], 1)


if __name__ == "__main__":
    unittest.main()
