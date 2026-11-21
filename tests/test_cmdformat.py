import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import cmdformat as c

L, G, S = chr(60), chr(62), chr(47)


def o(name, attrs='', label=''):
    return L + name + (':' + label if label else '') + (' ' + attrs if attrs else '') + G


def x(name, label=''):
    return L + S + name + (':' + label if label else '') + G


def line(name, attrs=''):
    return L + name + (' ' + attrs if attrs else '') + S + G


def tools(text):
    r = c.parse_reply(text)
    assert not r['errors'], r['errors']
    return r['tools']


def errors(text):
    r = c.parse_reply(text)
    assert r['errors'], 'expected errors, got none'
    assert r['tools'] == [], 'tools must be empty on error'
    return r['errors']


class TestBodyTags(unittest.TestCase):
    def test_sh(self):
        t = tools(o('sh') + 'git status' + x('sh'))
        self.assertEqual(t, [{'tool': 'run_command', 'parameters': {'command': 'git status'}}])

    def test_sh_multiline(self):
        t = tools(o('sh') + '\nls\npwd\n' + x('sh'))
        self.assertEqual(t[0]['parameters']['command'], 'ls\npwd')

    def test_sh_empty(self):
        errors(o('sh') + x('sh'))

    def test_bg_start(self):
        t = tools(o('bg_start') + 'npm run dev' + x('bg_start'))
        self.assertEqual(t, [{'tool': 'start_background_job', 'parameters': {'command': 'npm run dev'}}])

    def test_write(self):
        t = tools(o('write', 'path="a.txt"') + '\nhello\nworld\n' + x('write'))
        self.assertEqual(t, [{'tool': 'write_file', 'parameters': {'path': 'a.txt', 'content': 'hello\nworld\n'}}])

    def test_write_requires_path(self):
        errors(o('write') + '\nx\n' + x('write'))

    def test_write_empty_body(self):
        t = tools(o('write', 'path="a.txt"') + x('write'))
        self.assertEqual(t[0]['parameters']['content'], '')


class TestEdit(unittest.TestCase):
    def test_old_new(self):
        t = tools(o('edit', 'path="a.py"') + '\n' + o('old') + '\nfoo\n' + x('old') + '\n'
                  + o('new') + '\nbar\nbaz\n' + x('new') + '\n' + x('edit'))
        self.assertEqual(t, [{'tool': 'edit_file',
                              'parameters': {'path': 'a.py', 'search': 'foo', 'replace': 'bar\nbaz'}}])

    def test_start_end(self):
        t = tools(o('edit', 'path="a.py" start="def a" end="return 1"') + '\nnew text\n' + x('edit'))
        self.assertEqual(t[0]['parameters'], {'path': 'a.py', 'start_string': 'def a',
                                              'end_string': 'return 1', 'replace': 'new text'})

    def test_lines(self):
        t = tools(o('edit', 'path="a.py" lines="10-20"') + '\nnew\n' + x('edit'))
        self.assertEqual(t[0]['parameters'], {'path': 'a.py', 'start_line': '10', 'end_line': '20', 'replace': 'new'})

    def test_lines_and_start_conflict(self):
        errors(o('edit', 'path="a.py" lines="1-2" start="a" end="b"') + '\nx\n' + x('edit'))

    def test_start_without_end(self):
        errors(o('edit', 'path="a.py" start="a"') + '\nx\n' + x('edit'))

    def test_bad_lines(self):
        errors(o('edit', 'path="a.py" lines="20-10"') + '\nx\n' + x('edit'))
        errors(o('edit', 'path="a.py" lines="0-3"') + '\nx\n' + x('edit'))
        errors(o('edit', 'path="a.py" lines="abc"') + '\nx\n' + x('edit'))

    def test_empty_old(self):
        errors(o('edit', 'path="a.py"') + '\n' + o('old') + '\n\n' + x('old') + '\n'
               + o('new') + '\nb\n' + x('new') + '\n' + x('edit'))

    def test_missing_new_block(self):
        errors(o('edit', 'path="a.py"') + '\n' + o('old') + '\nfoo\n' + x('old') + '\n' + x('edit'))

    def test_text_after_new(self):
        errors(o('edit', 'path="a.py"') + '\n' + o('old') + '\nfoo\n' + x('old') + '\n'
               + o('new') + '\nbar\n' + x('new') + '\nstray\n' + x('edit'))

    def test_labelled_edit(self):
        body = x('old')  # a line equal to the plain closer, inside the old text
        t = tools(o('edit', 'path="a.py"', 'x1') + '\n' + o('old', '', 'x1') + '\nfoo\n' + body + '\n'
                  + x('old', 'x1') + '\n' + o('new', '', 'x1') + '\nbar\n' + x('new', 'x1') + '\n' + x('edit', 'x1'))
        self.assertEqual(t[0]['parameters']['search'], 'foo\n' + body)
        self.assertEqual(t[0]['parameters']['replace'], 'bar')


class TestLabelledWrite(unittest.TestCase):
    def test_plain_closer_line_stays_in_body(self):
        t = tools(o('write', 'path="a.txt"', 'x1') + '\nline1\n' + x('write') + '\nline3\n' + x('write', 'x1'))
        self.assertEqual(t[0]['parameters']['content'], 'line1\n' + x('write') + '\nline3\n')


class TestLineTags(unittest.TestCase):
    def check(self, text, tool, params):
        self.assertEqual(tools(text), [{'tool': tool, 'parameters': params}])

    def test_read(self):
        self.check(line('read', 'path="a.py"'), 'read_file', {'path': 'a.py'})

    def test_read_lines(self):
        self.check(line('read', 'path="a.py" lines="10-60"'), 'read_file',
                   {'path': 'a.py', 'start_line': '10', 'end_line': '60'})

    def test_read_find_around_all(self):
        self.check(line('read', 'path="a.py" find="def main" around="5"'), 'read_file',
                   {'path': 'a.py', 'search_term': 'def main', 'surrounding_lines': '5'})
        self.check(line('read', 'path="a.py" all="true"'), 'read_file', {'path': 'a.py', 'read_all': 'true'})

    def test_read_single_quotes(self):
        self.check(L + "read path='a.py'" + S + G, 'read_file', {'path': 'a.py'})

    def test_read_other_quote_inside(self):
        self.check(L + "read path='a.py' find='say \"hi\"'" + S + G, 'read_file',
                   {'path': 'a.py', 'search_term': 'say "hi"'})

    def test_read_missing_path(self):
        errors(line('read', 'lines="1-5"'))

    def test_ls(self):
        self.check(line('ls', 'path="src"'), 'list_dir', {'path': 'src'})
        self.check(line('ls'), 'list_dir', {})

    def test_cd(self):
        self.check(line('cd', 'path="src"'), 'change_directory', {'path': 'src'})

    def test_rm(self):
        self.check(line('rm', 'path="src/old.py"'), 'delete_file', {'path': 'src/old.py'})

    def test_mv(self):
        self.check(line('mv', 'from="a.py" to="b.py"'), 'rename_file', {'path': 'a.py', 'new_path': 'b.py'})

    def test_mv_missing_to(self):
        errors(line('mv', 'from="a.py"'))

    def test_grep(self):
        self.check(line('grep', 'dir="src" text="TODO"'), 'search_text', {'path': 'src', 'search_text': 'TODO'})
        self.check(line('grep', 'text="TODO"'), 'search_text', {'search_text': 'TODO'})

    def test_web(self):
        self.check(line('web', 'query="python release"'), 'web_search', {'query': 'python release'})

    def test_bg_list_stop(self):
        self.check(line('bg_list'), 'list_background_jobs', {})
        self.check(line('bg_stop', 'job="job_00001"'), 'stop_background_job', {'job_id': 'job_00001'})

    def test_confirm(self):
        self.check(line('confirm', 'id="ec_0001" decision="approve"'), 'confirm_command',
                   {'command_id': 'ec_0001', 'decision': 'approve'})

    def test_confirm_bad_decision(self):
        errors(line('confirm', 'id="ec_0001" decision="maybe"'))

    def test_unknown_attribute(self):
        errors(line('read', 'path="a.py" bogus="1"'))

    def test_duplicate_attribute(self):
        errors(line('read', 'path="a.py" path="b.py"'))

    def test_text_after_line_tag(self):
        errors(line('ls', 'path="src"') + ' extra')

    def test_line_tag_without_slash(self):
        self.assertEqual(tools(L + 'ls path="src"' + G)[0]['tool'], 'list_dir')

    def test_body_tag_with_slash(self):
        errors(L + 'sh' + S + G)

    def test_removed_tags_are_prose(self):
        for name in ('note', 'mkdir', 'skill'):
            r = c.parse_reply(line(name, 'path="a"'))
            self.assertEqual((r['tools'], r['errors']), ([], []), name)


class TestSayAndProse(unittest.TestCase):
    def test_say_message(self):
        r = c.parse_reply(o('say') + 'Hello' + x('say'))
        self.assertEqual((r['message'], r['tools'], r['errors']), ('Hello', [], []))

    def test_several_say_joined(self):
        r = c.parse_reply(o('say') + 'a' + x('say') + '\n' + o('say') + 'b' + x('say'))
        self.assertEqual(r['message'], 'a\nb')

    def test_say_with_attrs_error(self):
        errors(o('say', 'x="1"') + 'hi' + x('say'))

    def test_tags_inside_say_never_run(self):
        t = o('say') + 'try:\n' + o('sh') + 'rm -rf /' + x('sh') + '\n' + x('say')
        r = c.parse_reply(t)
        self.assertEqual((r['tools'], r['errors']), ([], []))
        self.assertIn('rm -rf', r['message'])

    def test_tags_in_code_fence_ignored(self):
        t = '```\n' + o('sh') + 'rm -rf /' + x('sh') + '\n```\n' + line('ls')
        self.assertEqual([k['tool'] for k in tools(t)], ['list_dir'])

    def test_unclosed_fence(self):
        errors('```\n' + line('ls'))

    def test_prose_only(self):
        r = c.parse_reply('just words, no tags')
        self.assertEqual((r['message'], r['tools'], r['errors']), ('', [], []))

    def test_unknown_tag_is_prose(self):
        r = c.parse_reply(L + 'div class="a"' + G + 'x' + L + S + 'div' + G)
        self.assertEqual((r['tools'], r['errors']), ([], []))

    def test_indented_tag_is_prose_or_error(self):
        r = c.parse_reply('  ' + line('ls'))
        self.assertEqual(r['tools'], [])

    def test_stray_closer(self):
        errors(x('sh'))

    def test_malformed_known_tag(self):
        errors(L + 'ls path=src' + S + G)

    def test_empty_and_none(self):
        for v in ('', None, '\n\n'):
            r = c.parse_reply(v)
            self.assertEqual((r['tools'], r['errors']), ([], []))


class TestRobustness(unittest.TestCase):
    def test_crlf(self):
        t = tools(o('write', 'path="a.txt"') + '\r\nhi\r\n' + x('write') + '\r\n' + line('ls'))
        self.assertEqual([k['tool'] for k in t], ['write_file', 'list_dir'])
        self.assertEqual(t[0]['parameters']['content'], 'hi\n')

    def test_unicode(self):
        t = tools(o('write', 'path="é.txt"') + '\nпривет 你好 😀\n' + x('write'))
        self.assertEqual(t[0]['parameters']['path'], 'é.txt')
        self.assertIn('😀', t[0]['parameters']['content'])

    def test_long_body(self):
        big = 'x' * 500000
        t = tools(o('write', 'path="big.txt"') + '\n' + big + '\n' + x('write'))
        self.assertEqual(len(t[0]['parameters']['content']), 500001)

    def test_order_preserved(self):
        t = tools(line('ls') + '\n' + o('sh') + 'a' + x('sh') + '\n' + line('cd', 'path="x"'))
        self.assertEqual([k['tool'] for k in t], ['list_dir', 'run_command', 'change_directory'])

    def test_text_after_closer_same_line(self):
        errors(o('sh') + 'ls' + x('sh') + ' extra')

    def test_error_names_line_and_tag(self):
        e = errors('ok text\n' + line('read', 'bogus="1"'))
        self.assertIn('line 2', e[0])
        self.assertIn('read', e[0])

    def test_second_tool_error_blocks_all(self):
        r = c.parse_reply(line('ls') + '\n' + line('read', 'bogus="1"'))
        self.assertEqual(r['tools'], [])
        self.assertTrue(r['errors'])

    def test_cut_at_every_byte(self):
        full = (o('say') + 'plan' + x('say') + '\n'
                + o('write', 'path="a.txt"') + '\nbody one\nbody two\n' + x('write') + '\n'
                + o('edit', 'path="a.py"') + '\n' + o('old') + '\nfoo\n' + x('old') + '\n'
                + o('new') + '\nbar\n' + x('new') + '\n' + x('edit') + '\n'
                + o('sh') + 'git status' + x('sh') + '\n'
                + line('read', 'path="a.py" lines="1-5"'))
        whole = c.parse_reply(full)
        self.assertEqual(whole['errors'], [])
        good = [t for t in whole['tools']]
        for n in range(len(full) + 1):
            r = c.parse_reply(full[:n])
            if r['errors']:
                self.assertEqual(r['tools'], [], n)
                continue
            # any tool that did run must be an exact, whole tool from the full reply, in order
            self.assertEqual(r['tools'], good[:len(r['tools'])], n)


if __name__ == '__main__':
    unittest.main()
