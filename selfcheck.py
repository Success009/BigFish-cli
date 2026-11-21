#!/usr/bin/env python3
"""selfcheck.py - runs before main.py (called from start-agent).

Fast path: if no Python file changed since the last known-good snapshot, exit at once.
Otherwise: compile every file and run the headless key tests (tests/test_keys.py).
  * all good  -> copy the files to known_good/ (the new last-known-good snapshot)
  * broken    -> print why and point to ./restore-known-good; exit 1 so main.py is not started
Escape hatches: --skip-check (or BIGFISH_SKIP_SELFCHECK=1) skips everything;
--safe (or BIGFISH_SAFE_INPUT=1) never blocks startup and never writes a snapshot.
"""
import hashlib
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
EXCLUDE_DIRS = {
    "venv", ".venv-local", "projects", "backups", "runs", "tests", "scratch",
    "sessions", "logs", "known_good", "known_good.new", "known_good.old",
    "__pycache__", ".git", "~",
}
SNAP_DIR = "known_good"
FP_FILE = ".fingerprint"


def find_py_files(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for name in filenames:
            if name.endswith(".py"):
                full = os.path.join(dirpath, name)
                out.append(os.path.relpath(full, root))
    return sorted(out)


def fingerprint(root, files):
    h = hashlib.sha256()
    for rel in files:
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        with open(os.path.join(root, rel), "rb") as f:
            h.update(f.read())
        h.update(b"\0")
    return h.hexdigest()


def stored_fingerprint(root):
    try:
        with open(os.path.join(root, SNAP_DIR, FP_FILE), "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def compile_errors(root, files):
    errors = []
    for rel in files:
        path = os.path.join(root, rel)
        try:
            with open(path, "rb") as f:
                compile(f.read(), path, "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as e:
            errors.append("%s: %s" % (rel, e))
    return errors


def key_test_errors(root, timeout=90):
    if not os.path.exists(os.path.join(root, "tests", "test_keys.py")):
        return []
    try:
        r = subprocess.run([sys.executable, "-m", "unittest", "tests.test_keys"],
                           cwd=root, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ["key tests timed out after %ss" % timeout]
    except OSError as e:
        return ["could not run key tests: %s" % e]
    if r.returncode != 0:
        tail = (r.stdout + r.stderr).strip().splitlines()[-12:]
        return ["key tests failed:"] + ["    " + ln for ln in tail]
    return []


def snapshot(root, files, fp):
    new = os.path.join(root, SNAP_DIR + ".new")
    cur = os.path.join(root, SNAP_DIR)
    old = os.path.join(root, SNAP_DIR + ".old")
    for p in (new, old):
        if os.path.exists(p):
            shutil.rmtree(p)
    for rel in files:
        dest = os.path.join(new, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(os.path.join(root, rel), dest)
    with open(os.path.join(new, FP_FILE), "w", encoding="utf-8") as f:
        f.write(fp)
    if os.path.exists(cur):
        os.replace(cur, old)
    os.replace(new, cur)
    if os.path.exists(old):
        shutil.rmtree(old)


def run(root=ROOT, argv=None, environ=None):
    argv = sys.argv[1:] if argv is None else argv
    environ = os.environ if environ is None else environ
    if "--skip-check" in argv or environ.get("BIGFISH_SKIP_SELFCHECK") == "1":
        return 0
    safe = ("--safe" in argv
            or environ.get("BIGFISH_SAFE_INPUT", "").strip() in ("1", "true", "yes"))
    files = find_py_files(root)
    fp = fingerprint(root, files)
    if fp == stored_fingerprint(root):
        return 0
    errors = compile_errors(root, files)
    if not errors and not safe:
        errors = key_test_errors(root)
    if errors:
        print("[selfcheck] PROBLEM: the code on disk did not pass its startup check:", file=sys.stderr)
        for e in errors:
            print("  " + e, file=sys.stderr)
        if os.path.isdir(os.path.join(root, SNAP_DIR)):
            print("[selfcheck] Roll back with: ./restore-known-good", file=sys.stderr)
        else:
            print("[selfcheck] No known-good snapshot exists yet.", file=sys.stderr)
        print("[selfcheck] Start anyway with --skip-check, or use --safe for plain input.", file=sys.stderr)
        return 0 if safe else 1
    if not safe:
        try:
            snapshot(root, files, fp)
        except OSError as e:
            print("[selfcheck] could not write snapshot: %s" % e, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(run())
