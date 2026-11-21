"""Turn-finished notification for Big Fish CLI.

When a turn ends and neither this terminal window nor this instance's Chrome
window is in front, do one of: nothing, play a short chime, or raise the
terminal window.  X11 only (xdotool); everywhere else it silently does nothing.

SECTIONS (keep them separate; tests rely on the split)
  1. CONFIG     - finish_action / finish_sound values and cycling helpers.
  2. SYNTHESIS  - generates the chime WAV files (cached on disk).
  3. PROBES     - the ONLY code that touches the system (ps, xdotool, pactl).
  4. DECISION   - should_notify(): pure function, no I/O. Unit tested.
  5. ACTIONS    - play(), raise_terminal(), on_turn_finished() (public entry).

PROTECTED: should_notify() must stay pure and must never match a Chrome that
is not the one owning remote-debugging-port=9222 (different profile = no match).
"""
from __future__ import annotations

import math
import os
import re
import shutil
import struct
import subprocess
import threading
import wave

# ---------------------------------------------------------------- 1. CONFIG
ACTIONS = ["off", "sound", "raise", "raise+sound"]
DEFAULT_ACTION = "sound"
SOUND_DIR = os.path.expanduser("~/gemini_local_agent/sounds")
CHROME_PORT_FLAG = "remote-debugging-port=9222"
RATE = 44100

# name -> list of (frequency_hz, seconds); 0 Hz = silence
SOUNDS = {
    "ping": [(880.0, 0.11), (1318.5, 0.16)],
    "drop": [(1174.7, 0.11), (783.99, 0.17)],
    "triple": [(1568.0, 0.05), (0.0, 0.04), (1568.0, 0.05), (0.0, 0.04), (1568.0, 0.05)],
}
DEFAULT = "ping"


def get_pause_media(load):
    """Whether the raise action also pauses playing media (default on)."""
    return bool(load().get("pause_media", True))


def next_in(options, current):
    """Return the option after `current` (wraps; unknown current -> first)."""
    if current in options:
        return options[(options.index(current) + 1) % len(options)]
    return options[0]


def get_action(load):
    a = load().get("finish_action", DEFAULT_ACTION)
    return a if a in ACTIONS else DEFAULT_ACTION


def get_sound(load):
    s = load().get("finish_sound", DEFAULT)
    return s if s in SOUNDS else DEFAULT


def cycle_action(load, save):
    new = next_in(ACTIONS, get_action(load))
    save({"finish_action": new})
    return new


def cycle_sound(load, save):
    new = next_in(list(SOUNDS), get_sound(load))
    save({"finish_sound": new})
    return new


# -------------------------------------------------------------- 2. SYNTHESIS
def _tone(freq, secs, amp=0.55):
    n = int(RATE * secs)
    out = []
    for i in range(n):
        if freq <= 0:
            out.append(0)
            continue
        t = i / RATE
        env = min(1.0, i / (RATE * 0.005)) * min(1.0, (n - i) / (RATE * 0.04))
        s = math.sin(2 * math.pi * freq * t) + 0.25 * math.sin(2 * math.pi * freq * 2 * t)
        out.append(int(32767 * amp * env * s / 1.25))
    return out


def sound_path(name):
    if name not in SOUNDS:
        name = DEFAULT
    path = os.path.join(SOUND_DIR, f"finish_{name}.wav")
    if not os.path.exists(path):
        os.makedirs(SOUND_DIR, exist_ok=True)
        samples = []
        for f, s in SOUNDS[name]:
            samples += _tone(f, s)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(b"".join(struct.pack("<h", x) for x in samples))
    return path


# ----------------------------------------------------------------- 3. PROBES
def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=2,
                              stdin=subprocess.DEVNULL).stdout.strip()
    except Exception:
        return ""


def chrome_pid():
    """Main Chrome process that owns the debug port (not renderers/helpers/sh)."""
    for line in _run(["ps", "-eo", "pid,args"]).splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2 or CHROME_PORT_FLAG not in parts[1]:
            continue
        args = parts[1]
        if "--type=" in args or args.startswith("/bin/sh") or args.startswith("sh "):
            continue
        try:
            return int(parts[0])
        except ValueError:
            continue
    return None


def ancestors():
    """PIDs of this process and its parents (used when WINDOWID is unset)."""
    pids, pid = set(), os.getpid()
    while pid > 1:
        pids.add(pid)
        try:
            with open(f"/proc/{pid}/stat") as f:
                pid = int(f.read().rsplit(")", 1)[1].split()[1])
        except Exception:
            break
    return pids


def probe_focus():
    """Raw facts about the active window, or None if X11 tools are unavailable."""
    if not shutil.which("xdotool") or not os.environ.get("DISPLAY"):
        return None
    active = _run(["xdotool", "getactivewindow"])
    apid = _run(["xdotool", "getwindowpid", active]) if active else ""
    return {
        "active_wid": int(active) if active.isdigit() else None,
        "active_pid": int(apid) if apid.isdigit() else None,
        "term_wid": int(os.environ["WINDOWID"]) if os.environ.get("WINDOWID", "").isdigit() else None,
        "chrome_pid": chrome_pid(),
        "ancestor_pids": ancestors(),
    }


def volume_audible():
    """False if the default sink is muted or at 0%."""
    if not shutil.which("pactl"):
        return True
    if "yes" in _run(["pactl", "get-sink-mute", "@DEFAULT_SINK@"]).lower():
        return False
    m = re.search(r"(\d+)%", _run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"]))
    return not (m and int(m.group(1)) == 0)


# --------------------------------------------------------------- 4. DECISION
def is_front(facts):
    """True if the active window is our terminal or this instance's Chrome.
    `facts` is the dict from probe_focus(); pure, no I/O."""
    if facts is None:
        return None  # unknown
    aw, ap = facts["active_wid"], facts["active_pid"]
    if aw is None:
        return False
    if facts["term_wid"] is not None and aw == facts["term_wid"]:
        return True
    if ap is None:
        return False
    if facts["chrome_pid"] is not None and ap == facts["chrome_pid"]:
        return True
    if facts["term_wid"] is None and ap in facts["ancestor_pids"]:
        return True
    return False


def should_notify(action, facts, audible=True):
    """Decide whether to fire. Pure function.
    - off: never.  - unknown focus (None): never.  - something of ours in front: never.
    - sound additionally requires audible volume; raise does not."""
    if action not in ("sound", "raise", "raise+sound"):
        return False
    front = is_front(facts)
    if front is None or front:
        return False
    if action == "sound" and not audible:
        return False
    return True


# ---------------------------------------------------------------- 5. ACTIONS
def play(name=DEFAULT):
    player = shutil.which("paplay") or shutil.which("aplay")
    if not player:
        return
    try:
        subprocess.Popen([player, sound_path(name)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def raise_commands(term_wid):
    """Commands that bring the terminal's OS window to the front (testable).
    Uses WINDOWID, which kitty sets per OS window, so other kitty windows are
    never touched. kitty remote control is deliberately NOT used: it is off by
    default and cannot be assumed."""
    if not term_wid:
        return []
    return [["xdotool", "windowactivate", str(term_wid)]]


def raise_terminal():
    wid = os.environ.get("WINDOWID", "")
    if not wid.isdigit() or not shutil.which("xdotool"):
        return
    for cmd in raise_commands(int(wid)):
        _run(cmd)


def pause_commands(players, exclude_pid=None):
    """Pure. players = [(mpris_name, status)]. Returns pause commands for players
    that are Playing, skipping this instance's own Chrome (name ends instance<pid>)."""
    cmds = []
    for name, status in players:
        if status != "Playing":
            continue
        if exclude_pid and name.startswith("chrom") and name.endswith(f"instance{exclude_pid}"):
            continue
        cmds.append(["playerctl", "-p", name, "pause"])
    return cmds


def list_players():
    """Probe: [(name, status)] for every MPRIS player (empty if playerctl is missing)."""
    if not shutil.which("playerctl"):
        return []
    names = [n for n in _run(["playerctl", "-l"]).splitlines() if n.strip()]
    return [(n, _run(["playerctl", "-p", n, "status"])) for n in names]


def pause_media(exclude_pid=None):
    for cmd in pause_commands(list_players(), exclude_pid):
        _run(cmd)


def on_turn_finished(load, sound_override=None):
    """Public entry. Non-blocking; safe to call from the main loop."""
    action = get_action(load)
    if action == "off":
        return
    name = sound_override or get_sound(load)

    def _work():
        try:
            facts = probe_focus()
            wants_sound = action in ("sound", "raise+sound")
            audible = volume_audible() if wants_sound else True
            if should_notify(action, facts, audible):
                if action in ("raise", "raise+sound"):
                    if get_pause_media(load):
                        pause_media(facts.get("chrome_pid") if facts else None)
                    raise_terminal()
                if wants_sound and audible:
                    play(name)
        except Exception:
            pass

    threading.Thread(target=_work, daemon=True).start()
