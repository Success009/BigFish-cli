# Offline tests for interface/attachments.py, browser.attach_files
import os
import sys
import shutil
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import paths  # noqa

import attachments as A


class AttachBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = A.STORE_DIR
        A.STORE_DIR = self.tmp
        A.clear()

    def tearDown(self):
        A.clear()
        A.STORE_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_file(self, name="notes.txt", data="hi"):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            f.write(data)
        return p


class TestStore(AttachBase):
    def test_image_tokens_numbered(self):
        self.assertEqual(A.add_image(b"a", "png"), "[Image 1.png]")
        self.assertEqual(A.add_image(b"b", "jpeg"), "[Image 2.jpeg]")

    def test_empty_image_rejected(self):
        self.assertIsNone(A.add_image(b"", "png"))

    def test_file_duplicate_names(self):
        p = self.make_file()
        self.assertEqual(A.add_file(p), "[notes.txt]")
        self.assertEqual(A.add_file(p), "[notes.txt (2)]")

    def test_brackets_in_name_sanitised(self):
        p = self.make_file("my[x].txt")
        self.assertEqual(A.add_file(p), "[my(x).txt]")

    def test_missing_file(self):
        self.assertIsNone(A.add_file("/nonexistent/file.txt"))

    def test_limit(self):
        for _ in range(A.MAX_ATTACHMENTS + 5):
            A.add_image(b"z")
        self.assertEqual(len(A.registry), A.MAX_ATTACHMENTS)

    def test_clear_removes_store(self):
        A.add_image(b"z")
        d = A._session_dir["path"]
        A.clear()
        self.assertEqual(len(A.registry), 0)
        self.assertFalse(os.path.exists(d))


class TestExtract(AttachBase):
    def test_plain_text_untouched(self):
        self.assertEqual(A.extract("hello world"), ("hello world", []))

    def test_extract_paths_and_clean(self):
        t1 = A.add_image(b"a")
        t2 = A.add_file(self.make_file())
        clean, paths_ = A.extract("see %s and %s ok" % (t1, t2))
        self.assertEqual(clean, "see and ok")
        self.assertEqual(len(paths_), 2)
        self.assertTrue(all(os.path.isfile(p) for p in paths_))

    def test_only_attachment_gives_empty_text(self):
        t1 = A.add_image(b"a")
        clean, paths_ = A.extract(t1)
        self.assertEqual(clean, "")
        self.assertEqual(len(paths_), 1)

    def test_unregistered_bracket_text_kept(self):
        self.assertEqual(A.extract("[Image 9.png] hi")[0], "[Image 9.png] hi")

    def test_spans_positions(self):
        t1 = A.add_image(b"a")
        text = "ab " + t1
        self.assertEqual([(s, e) for s, e, _ in A.spans(text)], [(3, 3 + len(t1))])

    def test_lexer_colours_token(self):
        from prompt_toolkit.document import Document
        t1 = A.add_image(b"a")
        parts = A.AttachmentLexer().lex_document(Document("x " + t1))(0)
        self.assertEqual(parts[1], (A.TOKEN_STYLE, t1))


class TestClipboardParsing(AttachBase):
    def test_uri_list(self):
        p = self.make_file("a b.txt")
        raw = ("# c\nfile://%s\n" % p.replace(" ", "%20")).encode()
        self.assertEqual(A._parse_uri_list(raw), [p])

    def test_uri_list_ignores_copy_and_missing(self):
        raw = b"copy\nfile:///nope/x.txt\n"
        self.assertEqual(A._parse_uri_list(raw), [])

    def test_priority_files_over_image(self):
        p = self.make_file()
        with mock.patch.object(A, "_targets", return_value=["text/uri-list", "image/png"]), \
             mock.patch.object(A, "_get", return_value=("file://%s" % p).encode()):
            kind, payload = A.read_clipboard()
        self.assertEqual(kind, "files")
        self.assertEqual(payload, [p])

    def test_image_when_no_files(self):
        with mock.patch.object(A, "_targets", return_value=["image/png"]), \
             mock.patch.object(A, "_get", return_value=b"PNG"):
            kind, payload = A.read_clipboard()
        self.assertEqual((kind, payload), ("image", (b"PNG", "png")))

    def test_text_fallback(self):
        import pyperclip
        with mock.patch.object(A, "_targets", return_value=["text/plain"]), \
             mock.patch.object(pyperclip, "paste", return_value="hello"):
            self.assertEqual(A.read_clipboard(), ("text", "hello"))

    def test_nothing_on_clipboard(self):
        import pyperclip
        with mock.patch.object(A, "_targets", return_value=[]), \
             mock.patch.object(pyperclip, "paste", return_value=""):
            self.assertIsNone(A.read_clipboard())


class TestEditing(AttachBase):
    """Uses a real prompt_toolkit Buffer; the handlers are exercised through install()."""

    def _handlers(self):
        from prompt_toolkit.key_binding import KeyBindings
        kb = KeyBindings()
        sess = mock.MagicMock()
        A.install(kb, sess)
        out = {}
        for b in kb.bindings:
            name = str(b.keys[0]).replace('Keys.', '')
            name = {'ControlH': 'backspace', 'Delete': 'delete', 'Left': 'left', 'Right': 'right'}.get(name, name)
            out[(name,)] = b.handler
        return out

    def _event(self, buff):
        ev = mock.MagicMock()
        ev.current_buffer = buff
        ev.arg = 1
        return ev

    def _buffer(self, text, pos):
        from prompt_toolkit.buffer import Buffer
        b = Buffer()
        b.text = text
        b.cursor_position = pos
        return b

    def test_backspace_removes_whole_token(self):
        t = A.add_image(b"a")
        b = self._buffer("hi " + t, 3 + len(t))
        self._handlers()[("backspace",)](self._event(b))
        self.assertEqual(b.text, "hi ")

    def test_backspace_normal_char(self):
        b = self._buffer("abc", 3)
        self._handlers()[("backspace",)](self._event(b))
        self.assertEqual(b.text, "ab")

    def test_delete_removes_whole_token(self):
        t = A.add_image(b"a")
        b = self._buffer(t + " x", 0)
        self._handlers()[("delete",)](self._event(b))
        self.assertEqual(b.text, " x")

    def test_left_right_skip_token(self):
        t = A.add_image(b"a")
        b = self._buffer("a" + t, 1)
        h = self._handlers()
        h[("right",)](self._event(b))
        self.assertEqual(b.cursor_position, 1 + len(t))
        h[("left",)](self._event(b))
        self.assertEqual(b.cursor_position, 1)

    def test_snap_moves_cursor_out_of_token(self):
        t = A.add_image(b"a")
        b = self._buffer(t, 3)
        A._snap(b)
        self.assertIn(b.cursor_position, (0, len(t)))


class TestImagePaste(AttachBase):
    def _buffer(self):
        from prompt_toolkit.buffer import Buffer
        return Buffer()

    def test_image_file_keeps_real_name(self):
        p = self.make_file("shot.png", "data")
        self.assertEqual(A.add_file(p), "[shot.png]")

    def test_non_image_file_keeps_name_token(self):
        p = self.make_file("notes.txt")
        self.assertEqual(A.add_file(p), "[notes.txt]")

    def test_many_files_keep_real_names(self):
        paths = [self.make_file("a%d.jpg" % i, "x") for i in range(3)]
        with mock.patch.object(A, "read_clipboard", return_value=("files", paths)):
            b = self._buffer()
            A._paste_from_clipboard(b)
        self.assertEqual(b.text, "[a0.jpg] [a1.jpg] [a2.jpg] ")

    def test_ctrl_shift_v_keeps_path_as_text(self):
        p = self.make_file("shot.png", "data")
        b = self._buffer()
        A._paste_text_only(b, p)
        self.assertEqual(b.text, p)
        self.assertEqual(len(A.registry), 0)

    def test_ctrl_shift_v_converts_file_uri_to_path(self):
        p = self.make_file("a b.png", "data")
        b = self._buffer()
        A._paste_text_only(b, "file://" + p.replace(" ", "%20"))
        self.assertEqual(b.text, p)
        self.assertEqual(len(A.registry), 0)

    def test_ctrl_shift_v_empty_paste_uses_clipboard_paths(self):
        p = self.make_file("shot.png", "data")
        with mock.patch.object(A, "read_clipboard", return_value=("files", [p])):
            b = self._buffer()
            A._paste_text_only(b, "")
        self.assertEqual(b.text, p)
        self.assertEqual(len(A.registry), 0)


class FakeInput:
    def __init__(self, accepts=True):
        self.accepts = accepts
        self.calls = []

    def set_input_files(self, files):
        if not self.accepts:
            raise RuntimeError("rejected")
        self.calls.append(files)


class FakePage:
    def __init__(self, inputs):
        self.inputs = inputs

    def query_selector_all(self, sel):
        return self.inputs


class TestBrowserAttach(AttachBase):
    def _ctrl(self, inputs):
        import browser
        c = browser.WebAgentController.__new__(browser.WebAgentController)
        c.page = FakePage(inputs)
        return c

    def test_attach_all_at_once(self):
        f = FakeInput()
        c = self._ctrl([f])
        p = self.make_file()
        with mock.patch("time.sleep"):
            self.assertTrue(c.attach_files([p]))
        self.assertEqual(f.calls, [[p]])

    def test_skips_rejecting_input(self):
        bad, good = FakeInput(False), FakeInput()
        c = self._ctrl([bad, good])
        p = self.make_file()
        with mock.patch("time.sleep"):
            self.assertTrue(c.attach_files([p]))
        self.assertEqual(len(good.calls), 1)

    def test_no_input_returns_false(self):
        c = self._ctrl([])
        p = self.make_file()
        with mock.patch("time.sleep"):
            self.assertFalse(c.attach_files([p]))

    def test_missing_files_filtered(self):
        c = self._ctrl([FakeInput()])
        self.assertTrue(c.attach_files(["/nonexistent/x"]))


class FakeChatBox:
    def inner_text(self):
        return ""

    def click(self, force=True):
        pass


if __name__ == "__main__":
    unittest.main()
