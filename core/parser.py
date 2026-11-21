# parser.py - AI Output Interpreter (Bulletproof Tag Extractor Edition)
import re
import html

OPEN_TOOL_TAGS = ['command', 'execute_tool', 'call_to_tool', 'tool_call', 'call']
CLOSE_TOOL_TAGS = ['command', 'execute_tool', 'call_to_tool', 'tool_call', 'call', 'tool_tool', 'tool']
EXEC_FAMILY_TAGS = {'execute_tool', 'call_to_tool', 'tool_call', 'call', 'tool_tool', 'tool'}

# Old tool names (with the sys_ prefix) are still accepted and mapped to the new names.
LEGACY_TOOL_NAMES = {
    'sys_read_file': 'read_file',
    'sys_run_command': 'run_command',
    'sys_write_file': 'write_file',
    'sys_list_dir': 'list_dir',
    'sys_edit_file': 'edit_file',
    'sys_delete_file': 'delete_file',
    'sys_search_text': 'search_text',
    'sys_rename_file': 'rename_file',
    'sys_create_dir': 'create_dir',
    'sys_confirm_command': 'confirm_command',
    'sys_start_background_job': 'start_background_job',
    'sys_stop_background_job': 'stop_background_job',
    'sys_list_background_jobs': 'list_background_jobs',
    'sys_web_search': 'web_search',
    'sys_load_skill': 'load_skill',
    'sys_change_directory': 'change_directory',
    'sys_run_interactive': 'run_interactive',
    'sys_write_to_scratchpad': 'write_to_scratchpad',
    'sys_ask_user': 'ask_user',
    'sys_complete': 'complete',
    'sys_spawn_subagent': 'spawn_subagent',
    'sys_list_subagents': 'list_subagents',
    'sys_cancel_subagent': 'cancel_subagent',
}

# ── DSML / provider-native syntax stripper ──────────────────────────────────
# DeepSeek has its own internal format (DSML) that leaks into output when our
# prompt is not followed.  Strip it before any other parsing so it never
# confuses the tool scanner.
#
# Patterns that must be removed (all are DeepSeek-internal, never user content):
#   <｜｜DSML｜｜...>  </｜｜DSML｜｜...>  any variant spelling
#   <｜tool▁call▁begin｜>  <｜plugin▁sep｜>  <｜tool▁outputs▁begin｜>  etc.
#   </parameter>  (DSML filler, spammed in broken outputs)
#   </201>  </processing>  (DSML numeric/state tags sometimes emitted)
#   JSON function-call blocks: {"name": "...", "arguments": ...}  (flat, not nested)
#
_DSML_PATTERNS = [
    # DSML pipe-bar wrapper tags  <｜｜DSML｜｜...>  (U+FF5C = ｜, U+007C = |)
    re.compile(r'<[/]?[｜|]{2}DSML[｜|]{2}[^>]*>', re.IGNORECASE),
    # DeepSeek special tokens  <｜anything｜>
    re.compile(r'<[｜|][^｜|>]{1,40}[｜|]>', re.IGNORECASE),
    # Stray </parameter> filler spam
    re.compile(r'</parameter\s*>', re.IGNORECASE),
    # Numeric / state junk close-tags: </201>, </processing>, </status>
    re.compile(r'</(?:\d+|processing|status|response|output|result)\s*>', re.IGNORECASE),
]


def _strip_dsml(text: str) -> str:
    """Remove DeepSeek-native DSML tags and filler from a raw model response.
    Operates on the full text BEFORE any other parsing step.
    Does NOT touch legitimate <command>, <message>, or CDATA content."""
    for pat in _DSML_PATTERNS:
        text = pat.sub('', text)
    return text

def is_matching_close(open_tag, close_tag):
    if open_tag == close_tag:
        return True
    if open_tag in EXEC_FAMILY_TAGS and close_tag in EXEC_FAMILY_TAGS:
        return True
    return False

CLOSE_CDATA_RE = re.compile(
    r'\]\]>\s*</(?:content|replace|search|command|execute_tool|call_to_tool|tool_call|call|message|talk_to_user|msg|explanation)>',
    re.IGNORECASE
)


def _mask_cdata(text):
    """Blank the interior of every CDATA block so tag scans never treat markup-like
    text inside CDATA as structural tags.

    Uses CLOSE_CDATA_RE to find the true CDATA close (]]> followed by a closing XML tag).
    This safely protects file content that contains literal ]]>, </command>, </content>,
    or nested <![CDATA[ from being prematurely cut off.
    """
    out = list(text)
    pos = 0
    OPEN = '<![CDATA['
    while pos < len(text):
        start = text.find(OPEN, pos)
        if start == -1:
            break
        content_start = start + len(OPEN)
        cm = CLOSE_CDATA_RE.search(text, content_start)
        if cm:
            # Mask from content_start through the ]]> itself so it cannot terminate tags early
            for i in range(content_start, cm.start() + 3):
                if out[i] != chr(10):
                    out[i] = chr(0)
            pos = cm.end()
        else:
            # Fallback for unclosed or non-standard CDATA blocks
            last_close = text.rfind(']]>', content_start)
            mask_end = last_close if last_close != -1 else len(text)
            for i in range(content_start, mask_end):
                if out[i] != chr(10):
                    out[i] = chr(0)
            pos = mask_end + 3 if last_close != -1 else len(text)
    return ''.join(out)


OPEN_MSG_RE = re.compile(r'<(message|talk_to_user|msg|explanation)(?:\s+[^>]*)?>', re.IGNORECASE)
STRUCTURAL_MSG_CLOSE_RE = re.compile(
    r'</(message|talk_to_user|msg|explanation)>(?=\s*(?:__(?:DONE|done)__|\bDONE\b|<(?:command|execute_tool|call_to_tool|tool_call|call|message|talk_to_user|msg|explanation)\b|$))',
    re.IGNORECASE
)
MSG_BODY_RE = re.compile(r'<(message|talk_to_user|msg|explanation)(?:\s+[^>]*)?>(.*?)</\1>', re.DOTALL | re.IGNORECASE)


OPEN_CONTENT_RE = re.compile(r'<content(?:\s+[^>]*)?>', re.IGNORECASE)
CLOSE_CONTENT_RE = re.compile(
    r'</content>(?=\s*</(?:command|execute_tool|call_to_tool|tool_call|call|tool_tool|tool)>(?:\s*(?:__(?:DONE|done)__|\bDONE\b|<(?:command|execute_tool|call_to_tool|tool_call|call|message|talk_to_user|msg)\b|$)))',
    re.IGNORECASE
)


def _pick_content_close(text, c_start):
    """Real </content> for a block opened at c_start. If the body starts with a CDATA
    wrapper, the real close directly follows the closing CDATA marker, so look-alike
    closes quoted inside the file text are skipped. Falls back to the first match."""
    cdata = re.match(r'\s*<!\[CDATA\[', text[c_start:c_start + 40]) is not None
    first = None
    for m in CLOSE_CONTENT_RE.finditer(text, c_start):
        if first is None:
            first = m
        if not cdata:
            return m
        if text[max(c_start, m.start() - 12):m.start()].rstrip().endswith(']]>'):
            return m
    return first


def _mask_content_bodies(text):
    """Blank the interior of <content> blocks (newlines kept) so that arbitrary tags,
    closing tool tags, or nested CDATA inside file content are never matched as structural tags."""
    out = list(text)
    pos = 0
    while pos < len(text):
        om = OPEN_CONTENT_RE.search(text, pos)
        if not om:
            break
        c_start = om.end()
        cm = _pick_content_close(text, c_start)
        if not cm:
            last_close = text.rfind('</content>', c_start)
            if last_close != -1:
                for i in range(c_start, last_close):
                    if out[i] != chr(10):
                        out[i] = chr(0)
                pos = last_close + len('</content>')
            else:
                pos = len(text)
            continue
        c_end = cm.start()
        for i in range(c_start, c_end):
            if out[i] != chr(10):
                out[i] = chr(0)
        pos = cm.end()
    return ''.join(out)


def _find_real_tool_block(text, start):
    """Index of first GENUINE tool opener in text[start:]. Genuine means the
    opener tag is IMMEDIATELY followed (allowing whitespace) by a
    <tool_name>/<name>/<tool_call_name> child tag. Literal <command>...</command>
    examples quoted inline inside a message body have no such child tag, so they
    are correctly skipped instead of being fabricated into tool calls."""
    for m in re.finditer(r'<(?:command|execute_tool|call_to_tool|tool_call|call)\b[^>]*>',
                         text[start:], re.IGNORECASE):
        body_start = start + m.end()
        head = text[body_start:body_start + 200]
        if re.match(r'\s*</', head, re.IGNORECASE):
            continue
        if re.match(r'\s*<(?:tool_name|name|tool_call_name)\b', head, re.IGNORECASE):
            return start + m.start()
    return None


def _protected_content_spans(text):
    """Spans (start, end) of CDATA-wrapped content bodies. Message openers found
    inside these spans are file text, never real messages."""
    spans = []
    pos = 0
    while pos < len(text):
        om = OPEN_CONTENT_RE.search(text, pos)
        if not om:
            break
        c_start = om.end()
        if re.match(r'\s*<!\[CDATA\[', text[c_start:c_start + 40]) is None:
            pos = c_start
            continue
        cm = _pick_content_close(text, c_start)
        if cm is None:
            break
        spans.append((c_start, cm.start()))
        pos = cm.end()
    return spans


_INLINE_CODE_TAG_RE = re.compile(
    r'(?<!`)`(</?[A-Za-z_][A-Za-z0-9_]*(?:\s[^<>`\n]*)?>)`(?!`)')


def _mask_inline_code_tags(text):
    """Blank any tag written inside single backticks (prose such as: the tool is sent as a tag).
    A real call is never wrapped in single backticks. Same length, offsets unchanged."""
    out = list(text)
    for m in _INLINE_CODE_TAG_RE.finditer(text):
        for i in range(m.start(1), m.end(1)):
            out[i] = chr(0)
    return ''.join(out)


def _mask_message_bodies(text):
    """Blank the BODY of every conversational message block (same length, newlines kept)
    so that tag-shaped text inside a message is never treated as a real tool call."""
    out = list(text)
    spans = _protected_content_spans(text)
    pos = 0
    while pos < len(text):
        om = OPEN_MSG_RE.search(text, pos)
        if not om:
            break
        _sp = next((s for s in spans if s[0] <= om.start() < s[1]), None)
        if _sp is not None:
            pos = _sp[1]
            continue
        tag = om.group(1).lower()
        start = om.end()
        cm = STRUCTURAL_MSG_CLOSE_RE.search(text, start)
        real_tool = _find_real_tool_block(text, start)
        inline_example = False
        if cm and cm.group(1).lower() == tag and real_tool is not None and real_tool < cm.start():
            seg = text[start:cm.start()]
            if (not OPEN_MSG_RE.search(seg)
                    and re.search(r'</(?:command|execute_tool|call_to_tool|tool_call|call)\s*>',
                                  text[real_tool:cm.start()], re.IGNORECASE)):
                inline_example = True
        if cm and cm.group(1).lower() == tag and (real_tool is None or cm.start() < real_tool or inline_example):
            for i in range(start, cm.start()):
                if out[i] != chr(10):
                    out[i] = chr(0)
            pos = cm.end()
        else:
            bound = real_tool if real_tool is not None else len(text)
            for i in range(start, bound):
                if out[i] != chr(10):
                    out[i] = chr(0)
            pos = bound
    return ''.join(out)


def extract_tool_blocks(text):
    """Extract tool blocks, CDATA- and content-aware: closing tags inside <content>
    or CDATA regions do not terminate the block early. Slices come from the ORIGINAL text."""
    blocks = []
    masked = _mask_cdata(_mask_content_bodies(_mask_message_bodies(_mask_inline_code_tags(text))))
    open_pattern = re.compile(r'<(' + '|'.join(OPEN_TOOL_TAGS) + r')(?:\s+[^>]*)?>', re.IGNORECASE)
    tag_scan = re.compile(
        r'<(' + '|'.join(OPEN_TOOL_TAGS) + r')(?:\s+[^>]*)?>|</(' + '|'.join(CLOSE_TOOL_TAGS) + r')>',
        re.IGNORECASE
    )

    pos = 0
    while pos < len(masked):
        m = open_pattern.search(masked, pos)
        if not m:
            break

        tag_name = m.group(1).lower()
        start_idx = m.end()

        depth = 1
        curr = start_idx
        found_close = False
        close_end = len(masked)

        while curr < len(masked):
            sm = tag_scan.search(masked, curr)
            if not sm:
                break
            if sm.group(1):
                if sm.group(1).lower() == tag_name:
                    depth += 1
                curr = sm.end()
            elif sm.group(2):
                if is_matching_close(tag_name, sm.group(2).lower()):
                    depth -= 1
                    if depth == 0:
                        found_close = True
                        close_end = sm.start()
                        pos = sm.end()
                        break
                curr = sm.end()

        if not found_close:
            content = text[start_idx:]
            if re.search(r'<(?:tool_name|name|tool_call_name)>', content, re.IGNORECASE):
                blocks.append((tag_name, content))
            pos = len(masked)
        else:
            content = text[start_idx:close_end]
            blocks.append((tag_name, content))

    return blocks

# Parameter tag names that mark a body as a structured call (not plain shell text).
_SHELL_PARAM_TAGS = (
    'path', 'new_path', 'content', 'search', 'replace', 'start_line', 'end_line',
    'start_string', 'end_string', 'search_term', 'read_all', 'directory',
    'search_text', 'job_id', 'query', 'skill', 'command_id', 'decision',
    'cmd', 'shell', 'command',
)
_SHELL_PARAM_TAG_RE = re.compile(
    r'</?(?:' + '|'.join(_SHELL_PARAM_TAGS) + r')(?:\s[^>]*)?>', re.IGNORECASE)


def parse_tool_block(block_text, outer_tag=None):
    """
    Parses a single tool execution block and extracts the tool name and all parameter key-value pairs.
    Handles raw text, CDATA wrappers, markdown backticks, and unclosed trailing tags.
    """
    if not block_text or not block_text.strip():
        return None

    # 1. Extract tool name
    tool_name_match = re.search(r'<(?:tool_name|name|tool_call_name)>\s*([a-zA-Z0-9_]+)\s*</(?:tool_name|name|tool_call_name)>', block_text, re.IGNORECASE)
    if not tool_name_match:
        tool_name_match = re.search(r'<(?:tool_name|name|tool_call_name)>\s*([a-zA-Z0-9_]+)', block_text, re.IGNORECASE)
        if not tool_name_match:
            # Shorthand: <command>shell text here</command> with no tool_name -> run_command.
            # Applies ONLY when ALL hold:
            #   - the outer tag is 'command' (bash/sh are never found by extract_tool_blocks)
            #   - the body contains none of the known PARAMETER tags (_SHELL_PARAM_TAGS): a body
            #     with <path>/<content>/... but no tool_name is a malformed structured call, not shell
            #   - the body is not pure thought text
            # Angle-bracket words inside the shell text (grep <div> f, List<int>) are allowed.
            if outer_tag and outer_tag.lower() == 'command':
                has_param_tags = bool(_SHELL_PARAM_TAG_RE.search(block_text))
                if not has_param_tags and not is_pure_thought_text(block_text):
                    cleaned_cmd = block_text.strip()
                    if cleaned_cmd:
                        return {'tool': 'run_command', 'parameters': {'command': cleaned_cmd}}
            return None
        tool_name = tool_name_match.group(1).strip()
    else:
        tool_name = tool_name_match.group(1).strip()

    params = {}
    
    # 2. Extract content parameter specifically if present.
    # Uses position-based extraction to correctly handle:
    #   - Literal ]]> inside CDATA
    #   - Literal </content> and </command> inside file content
    #   - Nested <![CDATA[ and same-line openers
    #   - Preserves all whitespace byte-exact (no .strip())
    content_start = block_text.find('<content>')
    if content_start != -1:
        after_tag = block_text[content_start + len('<content>'):]
        last_close = after_tag.rfind('</content>')
        if last_close != -1:
            body = after_tag[:last_close]
        else:
            outer_close = re.search(r'</(?:command|execute_tool|call_to_tool|tool_call|call|tool_tool|tool)>', after_tag, re.IGNORECASE)
            body = after_tag[:outer_close.start()] if outer_close else after_tag
        had_cdata = False
        while True:
            cdata_match = re.match(r'^\s*<!\[CDATA\[', body)
            if not cdata_match:
                break
            body = body[cdata_match.end():]
            had_cdata = True
        if body.endswith(']]>'):
            body = body[:-3]
        else:
            trailing_cdata = re.search(r'\]\]>\s*$', body)
            if trailing_cdata:
                body = body[:trailing_cdata.start()]
        if had_cdata:
            if body.startswith('\r\n'):
                body = body[2:]
            elif body.startswith('\n'):
                body = body[1:]
            _ind = re.search(r'\n[ \t]+$', body)
            if _ind:
                body = body[:_ind.start() + 1]
        params['content'] = body  # NO .strip() — preserve file content exactly

    # 3. Extract all other standard parameter tags (<path>, <start_line>, <end_line>, <command>, <cmd>, <search>, <replace>, etc.)
    # Remove the file-content region before scanning for parameters, so tag-like
    # text inside file content (e.g. a literal <path>) can never override real params.
    # (Tag strings are split so the tag scanner can't mistake this source for a content block.)
    _OPEN_C = '<con' + 'tent>'
    _CLOSE_C = '</con' + 'tent>'
    scan_text = block_text
    _cs = block_text.find(_OPEN_C)
    if _cs != -1:
        _after = block_text[_cs + len(_OPEN_C):]
        _lc = _after.rfind(_CLOSE_C)
        if _lc != -1:
            _end = _cs + len(_OPEN_C) + _lc + len(_CLOSE_C)
        else:
            _oc = re.search(r'</(?:command|execute_tool|call_to_tool|tool_call|call|tool_tool|tool)>', _after, re.IGNORECASE)
            _end = _cs + len(_OPEN_C) + (_oc.start() if _oc else len(_after))
        scan_text = block_text[:_cs] + block_text[_end:]
    all_tags = re.finditer(r'<([a-zA-Z0-9_]+)>(.*?)</\1>', scan_text, re.DOTALL)
    for m in all_tags:
        tag = m.group(1).lower()
        val = m.group(2).strip()
        if tag not in ['tool_name', 'name', 'tool_call_name', 'content']:
            if val.startswith('<![CDATA[') and val.endswith(']]>'):
                val = val[9:-3]
            else:
                val = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', val, flags=re.DOTALL)
            params[tag] = val

    # Normalize cmd / shell to command parameter
    if 'cmd' in params and 'command' not in params:
        params['command'] = params['cmd']
    elif 'shell' in params and 'command' not in params:
        params['command'] = params['shell']

    if isinstance(tool_name, str):
        tool_name = LEGACY_TOOL_NAMES.get(tool_name.strip().lower(), tool_name)

    return {'tool': tool_name, 'parameters': params}

def is_pure_thought_text(text):
    """
    Evaluates whether a text block consists exclusively of internal model thoughts,
    reasoning processes, or meta-cognitive commentary about the harness and tags.
    Returns True if no genuine user-facing communication or tools are present.
    """
    if not text or not text.strip():
        return True
    t = text.strip()
    
    # New format: a line that starts with a tag of the new format is never a pure thought.
    # (A reply made only of line tags has almost no text once tags are stripped.)
    _NEWFMT_TAGS_RE = re.compile(
        "^[ " + chr(9) + "]*" + chr(60)
        + "(say|sh|write|edit|read|ls|cd|rm|mv|grep|web|bg_start|bg_list|bg_stop|confirm)"
        + "(?::[A-Za-z0-9_]+)?(?=[ " + chr(9) + "/" + chr(62) + "])", re.MULTILINE)
    if _NEWFMT_TAGS_RE.search(t):
        return False

    # If genuine executable tool tags with tool_name are present, it is not pure thought
    if re.search(r'<(?:execute_tool|call_to_tool|tool_call|call|command)\b[^>]*>[\s\S]*?<(?:tool_name|name|tool_call_name)\b', t, re.IGNORECASE):
        return False

    # Strip XML tags to inspect raw text
    t_clean = re.sub(r'<[^>]+>', '', t).strip()
    substantive = re.sub(r'[\s\.\,\-\_\`\'\"\:\;\(\)\[\]\{\}\<\>\/\\\|]+', '', t_clean)
    if len(substantive) < 2:
        return True

    meta_patterns = [
        r"(?i)\b(?:the harness (?:might have|failed|dropped|did not)|outside these tags|inside these tags|failed to intercept|intercept the <execute_tool>|intercept the tool)\b",
        r"(?i)\b(?:user only saw|user'?s implicit demand|user'?s prompt is revealing|analyzing the user'?s|looking at the user'?s prompt)\b",
        r"(?i)\b(?:re-evaluating tool execution|deducing tool behavior|pinpointing desired response|analyzing missing tool output)\b",
        r"(?i)\b(?:my previous response|my current thinking|i need to consider|i suspect|i am considering|i'?m interpreting|i'?m focusing|i'?m noting|i'?m analyzing|i will now|i am evaluating|first, i will|let me examine|in this turn|we need to decide)\b",
        r"(?i)\b(?:expand to view model thoughts|collapse to hide model thoughts|chevron_right|more_vert|thumb_up|thumb_down)\b",
    ]

    lines = [l.strip() for l in t_clean.splitlines() if l.strip()]
    if not lines:
        return True

    thought_header_pattern = re.compile(
        r"^\*\*(?:(?:[A-Z][a-z]+(?:ing|ed)\b|Thought|Thinking|Reasoning|Reflection|Internal|Analysis|Plan|Goal|Strategy|Observation|Assessment|Evaluation|Diagnosis)\b[^\n*]*)\*\*$",
        re.IGNORECASE
    )

    thought_lines = 0
    for l in lines:
        if thought_header_pattern.match(l):
            thought_lines += 1
        elif any(re.search(pat, l) for pat in meta_patterns):
            thought_lines += 1

    return thought_lines == len(lines)

def strip_model_thoughts(text):
    """
    Strips internal thoughts, reasoning blocks, and DOM artifacts from AI output.
    Ensures that only genuine user-facing messages and tool execution tags remain.
    """
    if not text:
        return ""
    text = text.replace('\x00', '')
    
    # 1. Strip XML-style thinking/thought tags (supports Gemini thoughts, Claude antthinking, and DeepSeek <think>)
    text = re.sub(r'<(?:thought|thinking|think|reasoning|reflection|internal|antthinking|thought_process)(?:\s+[^>]*)?>[\s\S]*?</(?:thought|thinking|think|reasoning|reflection|internal|antthinking|thought_process)>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^\s*<(?:thought|thinking|think|reasoning|reflection|internal|antthinking|thought_process)(?:\s+[^>]*)?>[\s\S]*?(?=<(?:message|execute_tool|call_to_tool|tool_call|call|command|talk_to_user|msg)\b)', '', text, flags=re.IGNORECASE)

    # 2. Strip UI / DOM artifacts that might leak from AI Studio
    text = re.sub(r'(?:Expand to view model thoughts|Collapse to hide model thoughts|chevron_right|more_vert|edit\s*more_vert|thumb_up|thumb_down)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'Model \d+:\d+\s*(?:AM|PM)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\b\d+(?:\.\d+)?s\b', '', text)

    # 3. Strip standalone bold thought header lines before block splitting
    text = re.sub(
        r'^\s*\*\*(?:(?:[A-Z][a-z]+(?:ing|ed)\b|Thought|Thinking|Reasoning|Reflection|Internal|Analysis|Plan|Goal|Strategy|Observation|Assessment|Evaluation|Diagnosis)\b[^\n*]*)\*\*\s*$',
        '',
        text,
        flags=re.IGNORECASE | re.MULTILINE
    )

    # 4. Process blocks separated by double newlines to remove thought headers and reasoning
    blocks = re.split(r'\n\s*\n', text)
    cleaned_blocks = []
    
    thought_verb_pattern = re.compile(
        r'^\*\*(?:(?:[A-Z][a-z]+(?:ing|ed)\b|Thought|Thinking|Reasoning|Reflection|Internal|Analysis|Plan|Goal|Strategy|Observation|Assessment|Evaluation|Diagnosis)\b[^\n*]*)\*\*',
        re.IGNORECASE
    )
    thought_sentence_pattern = re.compile(
        r"^(?:i'm now|i am now|my current thinking|i need to consider|i suspect|i am considering|i'm interpreting|i'm focusing|i'm noting|i'm analyzing|i will now|i am evaluating|the user's prompt|looking at the user's|first, i will|let me examine|in this turn|we need to decide)",
        re.IGNORECASE
    )
    
    in_thought_section = False
    
    for block in blocks:
        b_str = block.strip()
        if not b_str:
            continue
            
        if is_pure_thought_text(b_str):
            # Check if there is an embedded genuine action tag inside this thought block
            tag_m = None
            if re.search(r'<(?:execute_tool|call_to_tool|tool_call|call|command)\b[^>]*>[\s\S]*?<(?:tool_name|name|tool_call_name)\b', b_str, re.IGNORECASE):
                tag_m = re.search(r'<(?:execute_tool|call_to_tool|tool_call|call|command)\b', b_str, re.IGNORECASE)
            elif re.search(r'<(?:message|talk_to_user|msg|explanation)\b', b_str, re.IGNORECASE):
                msg_m = re.search(r'<(?:message|talk_to_user|msg|explanation)\b[^>]*>([\s\S]*?)(?:</|$)', b_str, re.IGNORECASE)
                if msg_m and not is_pure_thought_text(msg_m.group(1)):
                    tag_m = re.search(r'<(?:message|talk_to_user|msg|explanation)\b', b_str, re.IGNORECASE)
            if tag_m:
                tag_content = b_str[tag_m.start():]
                if not is_pure_thought_text(tag_content):
                    cleaned_blocks.append(tag_content)
            continue

        # Check if block starts with a thought header
        if thought_verb_pattern.search(b_str):
            in_thought_section = True
            tag_m = None
            if re.search(r'<(?:execute_tool|call_to_tool|tool_call|call|command)\b[^>]*>[\s\S]*?<(?:tool_name|name|tool_call_name)\b', b_str, re.IGNORECASE):
                tag_m = re.search(r'<(?:execute_tool|call_to_tool|tool_call|call|command)\b', b_str, re.IGNORECASE)
            elif re.search(r'<(?:message|talk_to_user|msg|explanation)\b', b_str, re.IGNORECASE):
                msg_m = re.search(r'<(?:message|talk_to_user|msg|explanation)\b[^>]*>([\s\S]*?)(?:</|$)', b_str, re.IGNORECASE)
                if msg_m and not is_pure_thought_text(msg_m.group(1)):
                    tag_m = re.search(r'<(?:message|talk_to_user|msg|explanation)\b', b_str, re.IGNORECASE)
            if tag_m:
                tag_content = b_str[tag_m.start():]
                if not is_pure_thought_text(tag_content):
                    cleaned_blocks.append(tag_content)
                    in_thought_section = False
            continue
            
        if in_thought_section:
            first_line = b_str.splitlines()[0].strip()
            if thought_sentence_pattern.search(first_line) or thought_verb_pattern.search(first_line):
                continue
            tag_m = None
            if re.search(r'<(?:execute_tool|call_to_tool|tool_call|call|command)\b[^>]*>[\s\S]*?<(?:tool_name|name|tool_call_name)\b', b_str, re.IGNORECASE):
                tag_m = re.search(r'<(?:execute_tool|call_to_tool|tool_call|call|command)\b', b_str, re.IGNORECASE)
            elif re.search(r'<(?:message|talk_to_user|msg|explanation)\b', b_str, re.IGNORECASE):
                msg_m = re.search(r'<(?:message|talk_to_user|msg|explanation)\b[^>]*>([\s\S]*?)(?:</|$)', b_str, re.IGNORECASE)
                if msg_m and not is_pure_thought_text(msg_m.group(1)):
                    tag_m = re.search(r'<(?:message|talk_to_user|msg|explanation)\b', b_str, re.IGNORECASE)
            if tag_m:
                tag_content = b_str[tag_m.start():]
                if not is_pure_thought_text(tag_content):
                    cleaned_blocks.append(tag_content)
                    in_thought_section = False
                    continue
            in_thought_section = False
            
        first_line = b_str.splitlines()[0].strip()
        if thought_sentence_pattern.search(first_line):
            continue
            
        cleaned_blocks.append(block)
        
    result = '\n\n'.join(cleaned_blocks).strip()
    result = re.sub(r'^[`\'\"\s]*tags\.?', '', result).strip()
    return result

def _legacy_smart_parse_ai_output(raw_text, turn_marker=None):
    """
    Robust, stateful XML parser that extracts message text and tool calls.
    Immune to backticks, CDATA masking errors, HTML string collisions, and thought leakage.
    """
    if not raw_text: 
        return "PARSE_ERROR"
    
    raw_text = raw_text.replace('\x00', '')
    
    # 0. Strip DeepSeek-native DSML / provider syntax before any other processing
    raw_text = _strip_dsml(raw_text)
    
    # 1. Slice at turn marker if present
    if turn_marker and turn_marker in raw_text:
        ai_text = raw_text.split(turn_marker)[-1]
    else:
        ai_text = raw_text

    # 2. Defensive History Truncation if marker was lost
    if not turn_marker or turn_marker not in raw_text:
        last_prompt_pos = -1
        for indicator in ["=== USER REQUEST ===", "System Info:"]:
            idx = _mask_cdata(_mask_content_bodies(_mask_message_bodies(ai_text))).rfind(indicator)
            if idx > last_prompt_pos:
                last_prompt_pos = idx
        if last_prompt_pos != -1:
            ai_text = ai_text[last_prompt_pos:]

    # Hard stop at the first structural __DONE__ marker: nothing after it may run.
    # Masked copy hides message bodies, content bodies and CDATA, so a literal
    # __DONE__ inside file content or a message is never treated as the terminator.
    _masked_done = _mask_cdata(_mask_content_bodies(_mask_message_bodies(ai_text)))
    # A marker quoted inside a command body (grep pattern, heredoc, ...) is not the
    # terminator. Only a marker that directly follows a closing tag counts. If none
    # does but closing tags exist, do not cut here: the trim after the last closing
    # tag below already drops trailing text. With no closing tags, keep the old
    # first-marker behaviour.
    _sm = re.search(r'</[a-zA-Z_]+>\s*(__done__)', _masked_done, re.IGNORECASE)
    if _sm:
        ai_text = ai_text[:_sm.start(1)]
    elif not re.search(r'</[a-zA-Z_]+>', _masked_done):
        _dm = re.search(r'__done__', _masked_done, re.IGNORECASE)
        if _dm:
            ai_text = ai_text[:_dm.start()]

    # A made-up next turn (a fake "System Info:" or "Tool Results:" line right after a
    # closing tag) is never real. Cut from that line on. Uses the masked copy so
    # text inside file content, CDATA and messages is not affected.
    _mfake = _mask_cdata(_mask_content_bodies(_mask_message_bodies(ai_text)))
    _fk = re.search(r'</[a-zA-Z_]+>[ \t]*\r?\n\s*(?:System Info:|Tool Results:)', _mfake)
    if _fk:
        _cut = _fk.end() - len(re.search(r'(?:System Info:|Tool Results:)$', _fk.group(0)).group(0))
        ai_text = ai_text[:_cut]

    # Cut off any thoughts, summaries, or blather generated after closing tags or __DONE__
    closing_tags = list(re.finditer(r'</(?:message|talk_to_user|msg|explanation|execute_tool|call_to_tool|tool_call|call|command|tool_tool|tool)>', ai_text, re.IGNORECASE))
    if closing_tags:
        last_close = closing_tags[-1]
        trailing_part = ai_text[last_close.end():]
        if re.search(r'\b(?:__done__|done)\b', trailing_part, re.IGNORECASE) or len(trailing_part.strip()) > 0:
            ai_text = ai_text[:last_close.end()]
    else:
        if '__done__' in ai_text.lower():
            done_idx = ai_text.lower().rfind('__done__')
            ai_text = ai_text[:done_idx]
        elif re.search(r'\n\s*done\s*$', ai_text, re.IGNORECASE):
            ai_text = re.sub(r'\n\s*done\s*$', '', ai_text, flags=re.IGNORECASE).strip()

    tools_to_run = list()
    
    # Extract all tool execution blocks (<command>, <execute_tool>, <call_to_tool>, <tool_call>, <call>)
    tool_blocks = extract_tool_blocks(ai_text)
    for tag_name, block_text in tool_blocks:
        parsed_tool = parse_tool_block(block_text, outer_tag=tag_name)
        if parsed_tool:
            tools_to_run.append(parsed_tool)
            
    # Extract all conversational message blocks (<message>, <talk_to_user>, <msg>, <explanation>)
    message_parts = []
    pos = 0
    while pos < len(ai_text):
        om = OPEN_MSG_RE.search(ai_text, pos)
        if not om:
            break
        tag = om.group(1).lower()
        start = om.end()

        cm = STRUCTURAL_MSG_CLOSE_RE.search(ai_text, start)
        real_tool = _find_real_tool_block(ai_text, start)

        # A properly closed message that contains a complete inline command example
        # (no other message opens inside it) keeps its full text.
        inline_example = False
        if cm and cm.group(1).lower() == tag and real_tool is not None and real_tool < cm.start():
            seg = ai_text[start:cm.start()]
            if (not OPEN_MSG_RE.search(seg)
                    and re.search(r'</(?:command|execute_tool|call_to_tool|tool_call|call)\s*>',
                                  ai_text[real_tool:cm.start()], re.IGNORECASE)):
                inline_example = True

        if cm and cm.group(1).lower() == tag and (real_tool is None or cm.start() < real_tool or inline_example):
            part = ai_text[start:cm.start()]
            pos = cm.end()
        elif real_tool is not None:
            part = ai_text[start:real_tool]
            pos = real_tool
        else:
            pos = len(ai_text)
            continue

        part = html.unescape(part.strip())
        if part.startswith('<![CDATA[') and part.endswith(']]>'):
            part = part[9:-3].strip()
        else:
            part = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', part, flags=re.DOTALL)
        cleaned_part = strip_model_thoughts(part)
        if cleaned_part:
            # Strip stray inner message tag markers while preserving text
            cleaned_part = re.sub(r'</?(?:message|talk_to_user|msg|explanation)\b[^>]*>', '', cleaned_part).strip()
            if cleaned_part:
                message_parts.append(cleaned_part)

    extracted_message = "\n\n".join(message_parts).strip()
            
    if tools_to_run:
        return [{"status": "continue", "message": extracted_message, "tools": tools_to_run}]
        
    if extracted_message:
        return [{"status": "complete", "message": extracted_message, "tools": list()}]
        
    # If tags were present but produced no message/tools → PARSE_ERROR.
    # Do NOT strip tags and show raw text — that leaks internal tool structure to the user.
    # (The retry loop in main.py will prompt the model to fix its format.)
    has_any_tags = bool(re.search(r'<(?:message|talk_to_user|msg|explanation|execute_tool|call_to_tool|tool_call|call|command)\b', ai_text, re.IGNORECASE))
    if has_any_tags:
        return "PARSE_ERROR"

    # TRUE fallback: absolutely no tags anywhere — treat as plain text reply (e.g. a one-liner answer).
    cleaned_text = strip_model_thoughts(ai_text).strip()
    if cleaned_text and not is_pure_thought_text(cleaned_text):
        return [{"status": "complete", "message": cleaned_text, "tools": list()}]

    return "PARSE_ERROR"


# ---------------------------------------------------------------------------
# Cut-off detection (PROTECTED: pure, no I/O; used to refuse running a reply
# that ended in the middle of a tool call).
# ---------------------------------------------------------------------------
def _legacy_detect_truncation(raw_text):
    """Return a short reason string if the reply ends inside an unfinished tool
    block, else None. Text inside <message> bodies, CDATA, <content> bodies and
    backticks is masked first, so mentioning <command> there never triggers it."""
    if not raw_text:
        return None
    masked = _mask_cdata(_mask_content_bodies(_mask_message_bodies(_mask_inline_code_tags(raw_text))))
    open_pattern = re.compile(r'<(' + '|'.join(OPEN_TOOL_TAGS) + r')(?:\s+[^>]*)?>', re.IGNORECASE)
    tag_scan = re.compile(
        r'<(' + '|'.join(OPEN_TOOL_TAGS) + r')(?:\s+[^>]*)?>|</(' + '|'.join(CLOSE_TOOL_TAGS) + r')>',
        re.IGNORECASE
    )
    pos = 0
    while pos < len(masked):
        m = open_pattern.search(masked, pos)
        if not m:
            return None
        tag_name = m.group(1).lower()
        depth, curr, closed = 1, m.end(), False
        while curr < len(masked):
            sm = tag_scan.search(masked, curr)
            if not sm:
                break
            if sm.group(1):
                if sm.group(1).lower() == tag_name:
                    depth += 1
            elif sm.group(2) and is_matching_close(tag_name, sm.group(2).lower()):
                depth -= 1
                if depth == 0:
                    closed = True
                    pos = sm.end()
                    break
            curr = sm.end()
        if not closed:
            return "tool block opened but never closed"
    return None


# ---------------------------------------------------------------------------
# New command format (one tag per tool). The legacy functions above are kept
# only as dead reference code and are not called.
# ---------------------------------------------------------------------------
try:
    from core import cmdformat as _cmdformat
except ImportError:  # loaded by path (subagents/runner.py)
    import cmdformat as _cmdformat

LAST_PARSE_ERRORS = []


def smart_parse_ai_output(raw_text, turn_marker=None):
    """Parse a model reply in the new tag format.

    Returns a dict {"status", "message", "tools"} or the string "PARSE_ERROR".
    On PARSE_ERROR, LAST_PARSE_ERRORS holds the specific error lines.
    """
    global LAST_PARSE_ERRORS
    LAST_PARSE_ERRORS = []
    if not raw_text:
        LAST_PARSE_ERRORS = ["the reply was empty"]
        return "PARSE_ERROR"
    text = raw_text.replace('\x00', '')
    if turn_marker and turn_marker in text:
        text = text.split(turn_marker)[-1]
    res = _cmdformat.parse_reply(text)
    if res['errors']:
        LAST_PARSE_ERRORS = list(res['errors'])
        return "PARSE_ERROR"
    if res['tools']:
        return [{"status": "continue", "message": res['message'].strip(), "tools": res['tools']}]
    if res['message'].strip():
        return [{"status": "complete", "message": res['message'].strip(), "tools": []}]
    plain = text.strip()
    if plain:
        return [{"status": "complete", "message": plain, "tools": []}]
    LAST_PARSE_ERRORS = ["the reply held no say text and no tags"]
    return "PARSE_ERROR"


def detect_truncation(raw_text):
    """Cut-off detection is done by the new parser: an unclosed body tag is a parse error."""
    return None
