import tempfile
import threading
import time
import unittest

from subagents import contracts as C
from subagents.channel_web import WebChannel, ChannelError
from subagents.factory import SubFactory


class Tracker:
    def __init__(self):
        self.lock = threading.Lock()
        self.current = 0
        self.max_seen = 0
        self.starts = []


class FakeSlot:
    tracker = None
    fail = False

    def __init__(self, src, profile, work_root, name, **kw):
        self.name = name

    def start(self):
        t = FakeSlot.tracker
        with t.lock:
            t.current += 1
            t.max_seen = max(t.max_seen, t.current)
            t.starts.append((self.name, time.time()))
        time.sleep(0.05)
        with t.lock:
            t.current -= 1
        if FakeSlot.fail:
            raise RuntimeError("no chrome")
        return "http://127.0.0.1:1"

    def close(self):
        pass


class FakeCtl:
    def __init__(self, cdp, provider):
        pass

    def connect(self, timeout=0, target_url=None):
        return True

    def close(self):
        pass


class KwChannel:
    def __init__(self, *a, **kw):
        self.kw = kw

    def close(self):
        pass


def make(name, gate=None, pause=0.0):
    return WebChannel("gemini", "/src", "Profile 1", "/w", name, slot_factory=FakeSlot,
                      controller_factory=FakeCtl, start_url="http://x", start_gate=gate,
                      start_pause=pause, limiter=False)


def run_starts(chans):
    threads = [threading.Thread(target=c._start) for c in chans]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


class TestStartGate(unittest.TestCase):
    def setUp(self):
        FakeSlot.tracker = Tracker()
        FakeSlot.fail = False

    def test_gate_serialises_starts(self):
        gate = threading.Lock()
        run_starts([make("a", gate), make("b", gate), make("c", gate)])
        self.assertEqual(FakeSlot.tracker.max_seen, 1)

    def test_without_gate_starts_overlap(self):
        run_starts([make("a"), make("b"), make("c")])
        self.assertGreater(FakeSlot.tracker.max_seen, 1)

    def test_pause_is_held_between_starts(self):
        gate = threading.Lock()
        run_starts([make("a", gate, 0.2), make("b", gate, 0.2)])
        times = sorted(t for _, t in FakeSlot.tracker.starts)
        self.assertGreaterEqual(times[1] - times[0], 0.2)

    def test_failed_start_releases_gate(self):
        gate = threading.Lock()
        FakeSlot.fail = True
        with self.assertRaises(ChannelError):
            make("a", gate)._start()
        self.assertFalse(gate.locked())
        FakeSlot.fail = False
        make("b", gate)._start()

    def test_factory_shares_one_gate(self):
        work = tempfile.mkdtemp()
        f = SubFactory(work, profiles=lambda: ["Profile 1", "Profile 2"], src_dir="/src",
                       channel_cls=KwChannel, start_pause=1.5)
        role = C.Role(name="explore", provider="gemini", tools=["read_file"], prompt="p")
        t1 = C.make_task(role, "brief one")
        t2 = C.make_task(role, "brief two")
        k1, k2 = f(t1), f(t2)
        self.assertIs(k1.kw["start_gate"], k2.kw["start_gate"])
        self.assertEqual(k1.kw["start_pause"], 1.5)


if __name__ == "__main__":
    unittest.main()
