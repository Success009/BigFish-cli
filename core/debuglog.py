# debuglog.py - Compact rolling debug log (max 1000 lines, single file, per-instance)
import os, time, threading

_LOG_PATH = os.path.expanduser("~/gemini_local_agent/.bigfish_debug.log")
_MAX_LINES = 1000
_lock = threading.Lock()
_line_count = [None]  # in-memory count; None = not yet counted
_session_id = f"S{int(time.time())}"

def _ensure_dir():
    os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)

def init():
    """Wipe old log, start fresh for this instance."""
    _ensure_dir()
    with _lock:
        with open(_LOG_PATH, 'w') as f:
            f.write(f"[{_ts()}] === BigFish Debug Log (session {_session_id}) ===\n")

def _ts():
    return time.strftime("%H:%M:%S")

def log(tag, msg):
    """Append one compact line. Auto-truncate if > MAX_LINES."""
    line = f"[{_ts()}][{tag}] {msg}\n"
    with _lock:
        try:
            _ensure_dir()
            with open(_LOG_PATH, 'a') as f:
                f.write(line)
            # Truncate if too big. Counted in memory; the file is only read
            # when the count passes the limit by a margin (self-correcting).
            _line_count[0] = (_line_count[0] or 0) + 1
            if _line_count[0] > _MAX_LINES + 50:
                with open(_LOG_PATH, 'r') as f:
                    lines = f.readlines()
                lines = lines[-_MAX_LINES:]
                with open(_LOG_PATH, 'w') as f:
                    f.writelines(lines)
                _line_count[0] = len(lines)
        except Exception:
            pass

def path():
    return _LOG_PATH
