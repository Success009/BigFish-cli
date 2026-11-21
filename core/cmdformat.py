"""Big Fish CLI command format parser (one tag per tool, no wrapper).

parse_reply(text) -> {'message': str, 'tools': [ {'tool': ..., 'parameters': {...}} ], 'errors': [str]}

If 'errors' is non-empty, 'tools' is ALWAYS empty: nothing in that reply may run.
Nothing is guessed. Tags are only recognised at the start of a line, outside code fences.
Everything inside a say body is prose and is never run.
"""
import bisect
import re

LT, GT, SL = chr(60), chr(62), chr(47)

_ATTR = r'[ \t]+[a-z_]+=(?:"[^"\n]*"|\'[^\'\n]*\')'
OPEN_RE = re.compile('^' + LT + r'([a-z_]+)(?::([A-Za-z0-9_]+))?((?:' + _ATTR + r')*)[ \t]*(/?)' + GT)
CLOSE_RE = re.compile('^' + LT + SL + r'([a-z_]+)(?::([A-Za-z0-9_]+))?' + GT + r'[ \t]*$')
MARK_RE = re.compile('^[ \t]*' + LT + r'/?([a-z_]+)')
ATTR_RE = re.compile(r'([a-z_]+)=(?:"([^"\n]*)"|\'([^\'\n]*)\')')
LINES_RE = re.compile(r'^(\d+)-(\d+)$')

# name -> kind, tool (the name main.py dispatches on), params {attribute: main.py param}, required attributes, body param
SPEC = {
    'sh':        dict(kind='body', tool='run_command', params={}, req=set(), body='command'),
    'write':     dict(kind='body', tool='write_file', params={'path': 'path'}, req={'path'}, body='content'),
    'edit':      dict(kind='body', tool='edit_file',
                      params={'path': 'path', 'lines': None, 'start': 'start_string', 'end': 'end_string'},
                      req={'path'}, body=None),
    'bg_start':  dict(kind='body', tool='start_background_job', params={}, req=set(), body='command'),
    'read':      dict(kind='line', tool='read_file',
                      params={'path': 'path', 'lines': None, 'find': 'search_term',
                              'around': 'surrounding_lines', 'all': 'read_all'}, req={'path'}),
    'ls':        dict(kind='line', tool='list_dir', params={'path': 'path'}, req=set()),
    'cd':        dict(kind='line', tool='change_directory', params={'path': 'path'}, req={'path'}),
    'rm':        dict(kind='line', tool='delete_file', params={'path': 'path'}, req={'path'}),
    'mv':        dict(kind='line', tool='rename_file', params={'from': 'path', 'to': 'new_path'}, req={'from', 'to'}),
    'grep':      dict(kind='line', tool='search_text', params={'dir': 'path', 'text': 'search_text'}, req={'text'}),
    'web':       dict(kind='line', tool='web_search', params={'query': 'query', 'level': 'level'}, req={'query'}),
    'bg_list':   dict(kind='line', tool='list_background_jobs', params={}, req=set()),
    'bg_stop':   dict(kind='line', tool='stop_background_job', params={'job': 'job_id'}, req={'job'}),
    'confirm':   dict(kind='line', tool='confirm_command',
                      params={'id': 'command_id', 'decision': 'decision'}, req={'id', 'decision'}),
    # sub-agent group: kept until the sub-agent redesign is decided (one removable block)
    'spawn_subagent':  dict(kind='body', tool='spawn_subagent',
                            params={'role': 'role', 'scope': 'scope', 'write_paths': 'write_paths', 'wait': 'wait'},
                            req={'role'}, body='brief'),
    'list_subagents':  dict(kind='line', tool='list_subagents', params={}, req=set()),
    'cancel_subagent': dict(kind='line', tool='cancel_subagent', params={'id': 'id'}, req={'id'}),
}
NAMES = set(SPEC) | {'say'}


def _t(name):
    return LT + name + GT


def _attrs(hdr):
    out = {}
    for k, dq, sq in ATTR_RE.findall(hdr or ''):
        if k in out:
            return None, "duplicate attribute '%s'" % k
        out[k] = dq if dq != '' else sq
    return out, None


def _edit_blocks(body, label):
    """Split an edit body into (old, new). Returns an error string on failure."""
    sfx = (':' + label) if label else ''

    def op(n):
        return LT + n + sfx + GT

    def cl(n):
        return LT + SL + n + sfx + GT

    lines = body.split('\n')
    i = 0
    out = {}
    for n in ('old', 'new'):
        while i < len(lines) and lines[i].strip() == '':
            i += 1
        if i >= len(lines) or lines[i].strip() != op(n):
            return 'expected a ' + n + ' block starting with ' + op(n)
        i += 1
        j = i
        while j < len(lines) and lines[j].strip() != cl(n):
            j += 1
        if j >= len(lines):
            return 'the ' + n + ' block has no closing ' + cl(n)
        out[n] = '\n'.join(lines[i:j])
        i = j + 1
    while i < len(lines):
        if lines[i].strip() != '':
            return 'unexpected text after the new block'
        i += 1
    if out['old'].strip() == '':
        return 'old text is empty'
    return out['old'], out['new']


def _build(name, attrs, body, errs, where, label=None):
    spec = SPEC[name]

    def err(msg):
        errs.append('%s %s: %s' % (where, _t(name), msg))
        return None

    for k in attrs:
        if k not in spec['params']:
            return err("unknown attribute '%s'. Valid: %s" % (k, ', '.join(sorted(spec['params'])) or 'none'))
    for r in sorted(spec['req']):
        if attrs.get(r, '') == '':
            return err("missing required attribute '%s'" % r)
    p = {}
    for k, v in attrs.items():
        if k == 'lines':
            m = LINES_RE.match(v)
            if not m or int(m.group(1)) > int(m.group(2)) or int(m.group(1)) < 1:
                return err("lines must look like 10-20, got '%s'" % v)
            p['start_line'], p['end_line'] = m.group(1), m.group(2)
        else:
            p[spec['params'][k]] = v
    if name == 'confirm' and attrs.get('decision') not in ('approve', 'deny'):
        return err("decision must be approve or deny")
    if name == 'edit':
        if 'lines' in attrs and ('start' in attrs or 'end' in attrs):
            return err('use lines OR start/end, not both')
        if ('start' in attrs) != ('end' in attrs):
            return err('start and end must be given together')
        if 'lines' in attrs or 'start' in attrs:
            p['replace'] = body
        else:
            res = _edit_blocks(body, label)
            if isinstance(res, str):
                return err(res)
            p['search'], p['replace'] = res
    elif spec['kind'] == 'body':
        b = body
        if name == 'write':
            b = body + '\n' if body != '' else ''
        elif name in ('sh', 'bg_start'):
            b = body.strip('\n')
            if b.strip() == '':
                return err('empty command')
        elif name == 'spawn_subagent' and body.strip() == '':
            return err('empty brief')
        p[spec['body']] = b
    return {'tool': spec['tool'], 'parameters': p}


def parse_reply(text):
    text = (text or '').replace('\r\n', '\n')
    lines = text.split('\n')
    starts = []
    pos = 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln) + 1
    n = len(lines)
    say, tools, errs = [], [], []
    fence = False
    i = 0
    while i < n:
        ln = lines[i]
        where = 'line %d' % (i + 1)
        if ln.lstrip().startswith('```'):
            fence = not fence
            i += 1
            continue
        if fence:
            i += 1
            continue
        if ln.startswith(('System Info:', 'Tool Results:')) or (ln.startswith("Tool '") and ' executed' in ln):
            break
        c = CLOSE_RE.match(ln)
        if c and c.group(1) in NAMES:
            errs.append('%s: closing tag %s has no opening tag' % (where, LT + SL + c.group(1) + GT))
            i += 1
            continue
        m = OPEN_RE.match(ln)
        if not m:
            bad = MARK_RE.match(ln)
            if bad and bad.group(1) in NAMES:
                errs.append('%s: tag %s is malformed. Tags start at column 0 and use name="value" attributes' % (where, _t(bad.group(1))))
            i += 1
            continue
        name, label, hdr, slash = m.group(1), m.group(2), m.group(3), m.group(4)
        if name not in NAMES:
            i += 1
            continue
        is_body = name == 'say' or SPEC[name]['kind'] == 'body'
        attrs, aerr = _attrs(hdr)
        if aerr:
            errs.append('%s %s: %s' % (where, _t(name), aerr))
        rest = ln[m.end():]
        if not is_body:
            if rest.strip():
                errs.append('%s: text after %s tag' % (where, _t(name)))
            elif not aerr:
                t = _build(name, attrs, '', errs, where)
                if t:
                    tools.append(t)
            i += 1
            continue
        if slash:
            errs.append('%s: %s has a body and needs a closing tag, not a self-closing slash' % (where, _t(name)))
            i += 1
            continue
        closer = LT + SL + name + ((':' + label) if label else '') + GT
        begin = starts[i] + m.end()
        idx = text.find(closer, begin)
        if idx == -1:
            errs.append('%s: %s has no closing tag %s (reply cut off?)' % (where, _t(name), closer))
            break
        body = text[begin:idx]
        if body.startswith('\n'):
            body = body[1:]
        if body.endswith('\n'):
            body = body[:-1]
        after_start = idx + len(closer)
        eol = text.find('\n', after_start)
        trailing = text[after_start:(eol if eol != -1 else len(text))].strip()
        if trailing:
            errs.append('%s: text after closing tag %s on the same line' % (where, closer))
        if name == 'say':
            if (hdr or '').strip():
                errs.append('%s: say takes no attributes' % where)
            else:
                say.append(body)
        elif not aerr:
            t = _build(name, attrs, body, errs, where, label=label)
            if t:
                tools.append(t)
        i = bisect.bisect_right(starts, after_start - 1)
    if fence and not errs:
        errs.append('a code fence was opened and never closed, so every tag after it was ignored')
    if errs:
        return {'message': '\n'.join(say), 'tools': [], 'errors': errs}
    return {'message': '\n'.join(say), 'tools': tools, 'errors': []}
