"""Provider-independent attachment handling for the CLI prompt.

Ctrl+V with an image or file on the clipboard inserts an atomic, coloured token
such as [Image 1.png] into the input box. The file is copied into a store under
~/gemini_local_agent/attachments. On send, extract() returns the clean prompt
text plus the stored file paths, ready for whichever provider is active.
"""
import os
import re
import shutil
import subprocess
import time
import urllib.parse

from prompt_toolkit.keys import Keys
from prompt_toolkit.lexers import Lexer

STORE_DIR = os.path.expanduser("~/gemini_local_agent/attachments")
TOKEN_STYLE = "bg:#1f6feb fg:#ffffff bold"
MAX_ATTACHMENTS = 10
MAX_COPY_BYTES = 200 * 1024 * 1024

registry = {}   # token text -> stored file path
_counter = {"image": 0}
_session_dir = {"path": None}


# ---------------------------------------------------------------- store ----

def _dir():
    if not _session_dir["path"]:
        d = os.path.join(STORE_DIR, "%d_%d" % (os.getpid(), int(time.time())))
        os.makedirs(d, exist_ok=True)
        _session_dir["path"] = d
    return _session_dir["path"]


def add_image(data, ext="png"):
    """Stores raw image bytes. Returns the token, or None if the limit is hit."""
    if len(registry) >= MAX_ATTACHMENTS or not data:
        return None
    ext = re.sub(r"[^a-z0-9]", "", (ext or "png").lower()) or "png"
    _counter["image"] += 1
    n = _counter["image"]
    token = "[Image %d.%s]" % (n, ext)
    path = os.path.join(_dir(), "image_%d.%s" % (n, ext))
    with open(path, "wb") as f:
        f.write(data)
    registry[token] = path
    return token


def add_file(src):
    """Copies a file into the store. Returns the token, or None on failure."""
    src = os.path.expanduser(str(src))
    if len(registry) >= MAX_ATTACHMENTS or not os.path.isfile(src):
        return None
    try:
        if os.path.getsize(src) > MAX_COPY_BYTES:
            return None
    except OSError:
        return None
    name = os.path.basename(src).replace("[", "(").replace("]", ")")
    token = "[%s]" % name
    k = 1
    while token in registry:
        k += 1
        token = "[%s (%d)]" % (name, k)
    sub = os.path.join(_dir(), "f%d" % (len(registry) + _counter["image"] + k))
    os.makedirs(sub, exist_ok=True)
    dest = os.path.join(sub, name)
    try:
        shutil.copy2(src, dest)
    except Exception:
        return None
    registry[token] = dest
    return token


def clear():
    """Forgets every attachment and deletes the stored copies."""
    registry.clear()
    _counter["image"] = 0
    d = _session_dir["path"]
    _session_dir["path"] = None
    if d:
        shutil.rmtree(d, ignore_errors=True)


def spans(text):
    """Returns sorted (start, end, token) for every registered token in text."""
    found = []
    for token in registry:
        for m in re.finditer(re.escape(token), text):
            found.append((m.start(), m.end(), token))
    found.sort()
    return found


def extract(text):
    """Returns (clean_text, [file paths]) for the tokens present in text."""
    paths = []
    out = []
    last = 0
    for start, end, token in spans(text):
        if start < last:
            continue
        out.append(text[last:start])
        paths.append(registry[token])
        last = end
    out.append(text[last:])
    clean = re.sub(r"[ \t]{2,}", " ", "".join(out)).strip()
    return clean, paths


# ------------------------------------------------------------ clipboard ----

def _run(cmd, timeout=3):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def _use_wayland():
    return bool(os.environ.get("WAYLAND_DISPLAY")) and shutil.which("wl-paste") is not None


def _targets():
    if _use_wayland():
        out = _run(["wl-paste", "--list-types"])
    elif shutil.which("xclip"):
        out = _run(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"])
    else:
        out = None
    if not out:
        return []
    return [t.strip() for t in out.decode("utf-8", "ignore").splitlines() if t.strip()]


def _get(target):
    if _use_wayland():
        return _run(["wl-paste", "--type", target], timeout=8)
    return _run(["xclip", "-selection", "clipboard", "-t", target, "-o"], timeout=8)


def _parse_uri_list(raw):
    files = []
    for line in raw.decode("utf-8", "ignore").splitlines():
        line = line.strip()
        if line in ("copy", "cut") or not line or line.startswith("#"):
            continue
        if line.startswith("file://"):
            line = urllib.parse.unquote(line[7:])
        if os.path.isfile(line):
            files.append(line)
    return files


def read_clipboard():
    """Returns ('files', [paths]), ('image', (bytes, ext)), ('text', str) or None."""
    targets = _targets()
    for t in ("text/uri-list", "x-special/gnome-copied-files"):
        if t in targets:
            raw = _get(t)
            files = _parse_uri_list(raw) if raw else []
            if files:
                return ("files", files)
    for t in targets:
        if t.startswith("image/"):
            raw = _get(t)
            if raw:
                return ("image", (raw, t.split("/", 1)[1].split(";")[0].replace("jpeg", "jpg")))
    text = None
    try:
        import pyperclip
        text = pyperclip.paste()
    except Exception:
        pass
    if text:
        return ("text", text)
    return None


# ------------------------------------------------------------ prompt UI ----

class AttachmentLexer(Lexer):
    """Draws registered tokens in their own colour."""

    def lex_document(self, document):
        lines = document.lines

        def get_line(lineno):
            line = lines[lineno] if lineno < len(lines) else ""
            parts = []
            last = 0
            for start, end, _tok in spans(line):
                if start < last:
                    continue
                if start > last:
                    parts.append(("", line[last:start]))
                parts.append((TOKEN_STYLE, line[start:end]))
                last = end
            if last < len(line):
                parts.append(("", line[last:]))
            return parts

        return get_line


def _norm(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _insert_token(buff, token):
    buff.insert_text(token + " ")


def _paste_from_clipboard(buff):
    res = read_clipboard()
    if not res:
        return
    kind, payload = res
    if kind == "text":
        buff.insert_text(_norm(payload))
    elif kind == "image":
        tok = add_image(payload[0], payload[1])
        if tok:
            _insert_token(buff, tok)
    elif kind == "files":
        for p in payload:
            tok = add_file(p)
            if tok:
                _insert_token(buff, tok)


def _paste_text_only(buff, data):
    """Ctrl+Shift+V: insert text only. file:// URIs become plain paths; never a token."""
    data = _norm(data or "")
    if not data.strip():
        res = read_clipboard()
        if not res:
            return
        kind, payload = res
        if kind == "files":
            data = " ".join(payload)
        elif kind == "text":
            data = _norm(payload)
        else:
            return
    lines = []
    for line in data.split("\n"):
        s = line.strip()
        if s.startswith("file://"):
            line = urllib.parse.unquote(s[7:])
        lines.append(line)
    buff.insert_text("\n".join(lines))


def _snap(buff):
    """Keeps the cursor out of the middle of a token (e.g. after a mouse click)."""
    pos = buff.cursor_position
    for start, end, _tok in spans(buff.text):
        if start < pos < end:
            buff.cursor_position = start if (pos - start) <= (end - pos) else end
            return


def install(bindings, session):
    """Registers key bindings and cursor snapping on a PromptSession."""

    @bindings.add("c-v")
    def _(event):
        _paste_from_clipboard(event.current_buffer)

    @bindings.add(Keys.BracketedPaste)
    def _(event):
        _paste_text_only(event.current_buffer, event.data or "")

    @bindings.add("backspace")
    def _(event):
        buff = event.current_buffer
        pos = buff.cursor_position
        for start, end, _tok in spans(buff.text):
            if end == pos:
                buff.delete_before_cursor(end - start)
                return
        buff.delete_before_cursor(count=event.arg)

    @bindings.add("delete")
    def _(event):
        buff = event.current_buffer
        pos = buff.cursor_position
        for start, end, _tok in spans(buff.text):
            if start == pos:
                buff.delete(end - start)
                return
        buff.delete(count=event.arg)

    @bindings.add("left")
    def _(event):
        buff = event.current_buffer
        pos = buff.cursor_position
        for start, end, _tok in spans(buff.text):
            if end == pos:
                buff.cursor_position = start
                return
        buff.cursor_left(count=event.arg)

    @bindings.add("right")
    def _(event):
        buff = event.current_buffer
        pos = buff.cursor_position
        for start, end, _tok in spans(buff.text):
            if start == pos:
                buff.cursor_position = end
                return
        buff.cursor_right(count=event.arg)

    try:
        session.default_buffer.on_cursor_position_changed += _snap
    except Exception:
        pass
