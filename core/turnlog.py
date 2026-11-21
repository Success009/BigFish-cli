# turnlog.py - bounded per-turn capture record.
# One JSON line per turn: which source produced the reply and why.
# Rotates by size and keeps only the newest few files, so it can never grow without limit.
import json
import os
import time

LOG_DIR = os.path.expanduser("~/gemini_local_agent/turnlog")
MAX_FILE_BYTES = 150 * 1024
MAX_FILES = 4
MAX_FIELD_CHARS = 200


def _current_path():
    os.makedirs(LOG_DIR, exist_ok=True)
    files = sorted(f for f in os.listdir(LOG_DIR) if f.startswith("turns_") and f.endswith(".jsonl"))
    if files:
        last = os.path.join(LOG_DIR, files[-1])
        if os.path.getsize(last) < MAX_FILE_BYTES:
            return last
    nxt = 0
    if files:
        try:
            nxt = int(files[-1].rsplit('_', 1)[1].split('.')[0]) + 1
        except Exception:
            nxt = len(files)
    name = "turns_" + time.strftime("%Y%m%d_%H%M%S") + "_" + str(nxt).zfill(6) + ".jsonl"
    return os.path.join(LOG_DIR, name)


def _prune():
    files = sorted(f for f in os.listdir(LOG_DIR) if f.startswith("turns_") and f.endswith(".jsonl"))
    for old in files[:-MAX_FILES]:
        try:
            os.remove(os.path.join(LOG_DIR, old))
        except Exception:
            pass


def record(**fields):
    """Append one turn record. Never raises."""
    try:
        row = {"t": time.strftime("%Y-%m-%d %H:%M:%S")}
        for k, v in fields.items():
            if isinstance(v, str) and len(v) > MAX_FIELD_CHARS:
                v = v[:MAX_FIELD_CHARS]
            row[k] = v
        line = json.dumps(row, ensure_ascii=True) + "\n"
        path = _current_path()
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        _prune()
    except Exception:
        pass
