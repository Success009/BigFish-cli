# tools.py - The Capabilities Engine (Full Filesystem Edition)
import os
import shutil
import subprocess
import urllib.request
import urllib.parse
import json
import editor  # Delegating editing logic to the separate editor module

SCRATCHPAD_FILE = os.path.expanduser("~/gemini_local_agent/scratchpad.txt")
SEARCH_LOG_FILE = os.path.expanduser("~/gemini_local_agent/last_search_output.txt")
SKILLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")

# Injected by main.py after browser connects - enables CDP-assisted search (Tier 3)
_cdp_controller = None

def set_browser_controller(controller):
    """Called by main.py after browser connects, enabling Tier 3 CDP search."""
    global _cdp_controller
    _cdp_controller = controller


def is_protected_system_path(path: str) -> bool:
    """Return True if a file path targets protected operating system locations."""
    if not path:
        return False
    try:
        norm = os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))
    except Exception:
        norm = str(path)
    sys_prefixes = (
        '/etc', '/usr', '/boot', '/bin', '/sbin', '/lib', '/lib64',
        '/proc', '/sys', '/dev', '/run', '/var/lib', '/var/run', '/root'
    )
    for sp in sys_prefixes:
        if norm == sp or norm.startswith(sp + '/'):
            return True
    return False

def _sudo_read(path):
    """Reads content from a root-protected path using passwordless sudo."""
    proc = subprocess.run(["sudo", "-n", "cat", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise PermissionError(f"Sudo read failed: {proc.stderr.decode('utf-8', errors='replace')}")
    return proc.stdout.decode("utf-8", errors="replace")

def _sudo_write(path, content):
    """Writes content to a root-protected path using passwordless sudo."""
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        subprocess.run(["sudo", "-n", "mkdir", "-p", parent], check=True)
    proc = subprocess.run(["sudo", "-n", "tee", path], input=content.encode("utf-8"), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise PermissionError(f"Sudo write failed: {proc.stderr.decode('utf-8', errors='replace')}")

def format_size(size):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"

def web_search(query, num_results=5):
    """
    Executes a Google Search query using the Gemini 2.5 Flash API with search grounding.
    Uses the developer REST Spec payload key "googleSearchRetrieval" to trigger grounding.
    Returns only the direct factual text without printing long lists of source URLs.
    """
    api_key = os.environ.get("BIGFISH_GEMINI_API_KEY", "")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    
    # Instruct 2.5 Flash that its results are for a middleware parser pipeline, not a human reader
    system_instruction = (
        "You are a high-fidelity web search grounding assistant. Your response is acting as a "
        "middleware database for a local AI software engineering agent, not a human user. "
        "When queried, use your search tools to fetch the most up-to-date, relative, and factual data. "
        "Provide a precise, highly structured summary of the search findings. Do not include conversational filler "
        "or chat-like intro/outro text, and do not append lists of references or URLs at the end of the text."
    )
    
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": f"Search Query: {query}\n\nPlease perform web research and return the relevant results."}
                ]
            }
        ],
        "systemInstruction": {
            "parts": [
                {"text": system_instruction}
            ]
        },
        "tools": [
            {"google_search": {}}  # Modern Gemini Google Search grounding schema
        ],
        "generationConfig": {
            "temperature": 0.2
        }
    }
    
    try:
        if not api_key:
            raise ValueError("no API key set")
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            url,
            data=data,
            headers={'Content-Type': 'application/json'}
        )
        
        with urllib.request.urlopen(req, timeout=15) as response:
            response_data = json.loads(response.read().decode('utf-8'))
            
        candidates = response_data.get('candidates', [])
        if candidates:
            content_parts = candidates[0].get('content', {}).get('parts', [])
            if content_parts:
                search_text = content_parts[0].get('text', '')
                if search_text:
                    total_chars = len(search_text)
                    MAX_SEARCH_CHARS = 5000
                    if total_chars > MAX_SEARCH_CHARS:
                        os.makedirs(os.path.dirname(SEARCH_LOG_FILE), exist_ok=True)
                        with open(SEARCH_LOG_FILE, 'w', encoding='utf-8', errors='replace') as sf:
                            sf.write(search_text)
                            
                        truncated_text = search_text[:3000]
                        prefix = (
                            f"--- SEARCH RESULTS TRUNCATED DUE TO SIZE SAFETY LIMITS ---\n"
                            f"The search query produced {total_chars} characters of output.\n"
                            f"The FULL search output was automatically saved to: {SEARCH_LOG_FILE}\n"
                            f"(If you need to read the full search logs, use read_file on that path).\n\n"
                            f"--- LAST 3,000 CHARACTERS PREVIEW ---\n"
                        )
                        return f"{prefix}{truncated_text}\n... [TRUNCATED] ..."
                    return search_text
    except Exception:
        pass

    # Resilient zero-auth search fallback (prevents breakdown when API keys or quotas fail)
    try:
        import html as html_lib
        import re as re_lib
        headers = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/119.0'}
        f_url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        f_req = urllib.request.Request(f_url, headers=headers)
        with urllib.request.urlopen(f_req, timeout=10) as resp:
            content = resp.read().decode('utf-8', errors='ignore')
        snippets = re_lib.findall(r'<a class="result__snippet[^>]*>(.*?)</a>', content, re_lib.DOTALL)
        cleaned = [re_lib.sub(r'<[^>]+>', '', html_lib.unescape(s)).strip() for s in snippets[:num_results]]
        if cleaned:
            return "--- Search Findings (via Zero-Auth Fallback) ---\n" + "\n\n".join(cleaned)
    except Exception:
        pass

    # Tier 3: CDP Browser-Assisted Search - use active Chrome profile via Playwright
    # This is bot-proof, zero-API, always-current as it runs inside the user's real browser
    if _cdp_controller is not None:
        try:
            import re as re_lib
            import html as html_lib
            page = _cdp_controller.browser.new_page() if hasattr(_cdp_controller, 'browser') and _cdp_controller.browser else None
            if page:
                search_url = f"https://www.google.com/search?q={urllib.parse.quote(query)}&hl=en"
                page.goto(search_url, wait_until="domcontentloaded", timeout=12000)
                # Extract featured snippet, knowledge panel, and top result summaries
                js_extract = """() => {
                    const getText = (sel) => {
                        const el = document.querySelector(sel);
                        return el ? el.innerText.trim() : '';
                    };
                    const getAll = (sel) => Array.from(document.querySelectorAll(sel)).map(e => e.innerText.trim()).filter(Boolean);
                    const featured = getText('.hgKElc') || getText('.Z0LcW') || getText('.LGOjhe') || getText('.kp-blk .IZ6rdc');
                    const snippets = getAll('.VwiC3b');
                    const titles = getAll('h3.LC20lb');
                    let result = '';
                    if (featured) result += 'Featured: ' + featured + '\\n\\n';
                    for (let i = 0; i < Math.min(5, snippets.length); i++) {
                        if (titles[i]) result += titles[i] + '\\n';
                        if (snippets[i]) result += snippets[i] + '\\n\\n';
                    }
                    return result.trim();
                }"""
                cdp_text = page.evaluate(js_extract)
                page.close()
                if cdp_text and len(cdp_text) > 20:
                    return f"--- Search Findings (via CDP Browser) ---\n{cdp_text}"
        except Exception:
            try:
                if page and not page.is_closed():
                    page.close()
            except Exception:
                pass

    return "ERROR: No search results returned from any search tier."

def load_skill(skill_name):
    """
    Loads a dynamic skill instruction file from the skills/ directory.
    Returns the raw markdown content to be injected into the active turn context.
    Lists available skills if skill_name is 'list' or not found.
    """
    os.makedirs(SKILLS_DIR, exist_ok=True)
    
    if skill_name.lower() in ('list', '', 'help'):
        available = [f[:-3] for f in os.listdir(SKILLS_DIR) if f.endswith('.md') and f != 'README.md']
        if not available:
            return "No skills available. Create .md files in the skills/ directory."
        return "Available skills:\n" + "\n".join(f"  - {s}" for s in sorted(available))

    # Try exact then fuzzy match
    candidates = [f for f in os.listdir(SKILLS_DIR) if f.endswith('.md')]
    matched_file = None
    name_l = skill_name.lower()
    if name_l.endswith('.md'):
        name_l = name_l[:-3]
    exact_name = name_l + '.md'
    for c in candidates:
        if c.lower() == exact_name:
            matched_file = c
            break
    if not matched_file:
        for c in candidates:
            if skill_name.lower() in c.lower():
                matched_file = c
                break

    if not matched_file:
        available = [f[:-3] for f in candidates if f != 'README.md']
        return f"ERROR: Skill '{skill_name}' not found. Available: {', '.join(sorted(available)) or 'none'}"

    skill_path = os.path.join(SKILLS_DIR, matched_file)
    try:
        with open(skill_path, 'r', encoding='utf-8') as f:
            content = f.read()
        skill_display = matched_file[:-3]
        return f"=== SKILL LOADED: {skill_display} ===\n{content}\n=== END SKILL ==="
    except Exception as e:
        return f"ERROR: Could not read skill file '{matched_file}': {e}"


def read_file(path, start_line=None, end_line=None, search_term=None, surrounding_lines=3, read_all=False):
    try:
        try:
            if os.path.isfile(path):
                with open(path, 'rb') as _bf:
                    _head = _bf.read(8192)
                if b'\x00' in _head:
                    return (f"ERROR: {path} looks like a binary file ({os.path.getsize(path)} bytes). "
                            f"Not shown as text. Use run_command (xxd, file, strings) to inspect it.")
        except OSError:
            pass
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
        except PermissionError:
            raw_text = _sudo_read(path)
            lines = raw_text.splitlines(True)
            
        total_lines = len(lines)
        if total_lines == 0:
            return f"--- {path} is empty ---"

        if (not search_term and not read_all and start_line is not None
                and int(start_line) > total_lines):
            return (f"ERROR: start_line {start_line} is beyond the end of {path} "
                    f"({total_lines} lines).")

        if search_term:
            # Collect ALL matching line indices, not just the first
            match_indices = [i for i, line in enumerate(lines) if search_term in line]
            if not match_indices:
                return f"ERROR: The search term '{search_term}' was not found in {path}."
            
            # Build the output: surrounding context around each unique match
            output_ranges = []
            for match_idx in match_indices:
                s = max(0, match_idx - surrounding_lines)
                e = min(total_lines - 1, match_idx + surrounding_lines)
                output_ranges.append((s, e))
            
            # Merge overlapping ranges
            merged = []
            for s, e in sorted(output_ranges):
                if merged and s <= merged[-1][1] + 1:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], e))
                else:
                    merged.append([s, e])
            
            output_lines = []
            for seg_s, seg_e in merged:
                if output_lines:
                    output_lines.append("     | --- (gap) ---\n")
                for i in range(seg_s, seg_e + 1):
                    marker = ">>>" if search_term in lines[i] else "   "
                    output_lines.append(f"{i+1:4} {marker} {lines[i]}")
            
            content = "".join(output_lines)
            _CAP = 15000
            _note = ""
            if len(content) > _CAP:
                content = content[:_CAP]
                _note = (f"\n\n--- SEARCH OUTPUT TRUNCATED AT {_CAP} CHARS. "
                         f"{len(match_indices)} matches in total. Use a more specific search_term "
                         f"or read a line range. ---")
            return (f"--- search_term '{search_term}': {len(match_indices)} match(es) "
                    f"in {path} ---\n{content}{_note}")

        else:
            if read_all:
                s_line = 1
                e_line = total_lines
            elif start_line is not None and end_line is None:
                s_line = max(1, int(start_line))
                e_line = min(total_lines, s_line + 500)
            elif end_line is not None and start_line is None:
                e_line = min(total_lines, int(end_line))
                s_line = max(1, e_line - 500)
            elif start_line is not None and end_line is not None:
                if int(start_line) > int(end_line):
                    return f"ERROR: Invalid line range: start_line ({start_line}) > end_line ({end_line})."
                s_line = max(1, int(start_line))
                e_line = min(total_lines, int(end_line))
            else:
                s_line = 1
                e_line = min(total_lines, 500)
                if total_lines > 500:
                    warning = f"\n\n[SYSTEM WARNING: File has {total_lines} lines. You only saw lines 1-500. Use <start_line> and <end_line> to read more, or set <read_all>true</read_all> to load everything.]"

        output_lines = list()
        total_read_chars = 0
        MAX_TOTAL_READ_CHARS = 15000
        MAX_LINE_CHARS = 1000
        truncated_due_to_chars = False

        # Bypass character truncation and operation limits if reading the entire file, OR if a precise line range is specified
        bypass_char_limit = read_all or (start_line is not None and end_line is not None)

        for i in range(s_line - 1, e_line):
            raw_line = lines[i]
            
            # Enforce line-level character density limits only if not bypassing limits
            if not bypass_char_limit and len(raw_line) > MAX_LINE_CHARS:
                processed_line = raw_line[:MAX_LINE_CHARS] + " ... [Line truncated at 1,000 characters] ...\n"
            else:
                processed_line = raw_line
                
            line_to_add = f"{i+1:4} | {processed_line}"
            
            # Enforce strict total-operation character limit (15,000 characters) only if not bypassing limits
            if not bypass_char_limit and (total_read_chars + len(line_to_add) > MAX_TOTAL_READ_CHARS):
                truncated_due_to_chars = True
                allowed_len = MAX_TOTAL_READ_CHARS - total_read_chars
                if allowed_len > 0:
                    output_lines.append(line_to_add[:allowed_len])
                break
            
            output_lines.append(line_to_add)
            total_read_chars += len(line_to_add)
            
        content = "".join(output_lines)
        res = f"--- Reading lines {s_line} to {e_line} of {total_lines} in {path} ---\n{content}"
        
        if not bypass_char_limit and truncated_due_to_chars:
            res += f"\n\n--- FILE READ TRUNCATED DUE TO CHARACTER LIMITS (15,000 Chars Max) ---"
        elif 'warning' in locals():
            res += warning
            
        return res
        
    except FileNotFoundError:
        return f"ERROR: File not found at {path}"
    except Exception as e:
        return f"ERROR: Could not read file. {str(e)}"

def write_file(path, content):
    # Safety net: an empty write over a non-empty file is almost always a parse failure.
    try:
        if content == "" and os.path.isfile(path) and os.path.getsize(path) > 0:
            return (f"ERROR: Refusing to overwrite non-empty {path} with empty content. "
                    f"This usually means the content was lost in parsing. No changes made. "
                    f"To truncate on purpose, use run_command with ': > file'.")
    except Exception:
        pass
    
    if is_protected_system_path(path):
        return f"ERROR: Protected system path access denied for {path}. File tools cannot modify protected OS directories. Use run_command with sudo if system modification is required."
    try:
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            # Atomic write: temp file in the same folder, then rename over the target.
            # A crash mid-write leaves the original file untouched.
            import tempfile as _tf
            real_path = os.path.realpath(path)  # follow symlinks so the link is preserved
            tmp_fd, tmp_path = _tf.mkstemp(dir=os.path.dirname(real_path) or '.', prefix='.bf_write_')
            try:
                with os.fdopen(tmp_fd, 'w', encoding='utf-8', errors='replace', newline='') as f:
                    f.write(content)
                if os.path.exists(real_path):
                    try:
                        os.chmod(tmp_path, os.stat(real_path).st_mode & 0o7777)
                    except OSError:
                        pass
                else:
                    os.chmod(tmp_path, 0o644)
                os.replace(tmp_path, real_path)
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except PermissionError:
            # Automatic passwordless sudo elevation for root-protected files/directories
            _sudo_write(path, content)

        # Server-side write verification: read back to guarantee data integrity
        try:
            with open(path, 'r', encoding='utf-8', errors='replace', newline='') as f:
                verified_content = f.read()
            if len(verified_content) != len(content):
                return (
                    f"ERROR: Write verification failed for {path}! "
                    f"Expected {len(content)} characters, but verified {len(verified_content)} on disk. "
                    f"Content may have been truncated."
                )
        except Exception:
            pass  # Fallback if read-back has special permission limitations

        return f"Successfully wrote {len(content)} characters to {path}"
    except Exception as e:
        return f"ERROR: Could not write file. {str(e)}"

def edit_file(path, search_text=None, replace_text=None, start_line=None, end_line=None, start_string=None, end_string=None):
    """Delegates to the external editor.py module."""
    return editor.edit_file(path, search_text, replace_text, start_line, end_line, start_string, end_string)

def create_directory(path):
    if is_protected_system_path(path):
        return f"ERROR: Protected system path access denied for {path}. File tools cannot modify protected OS directories."
    try:
        try:
            os.makedirs(path, exist_ok=True)
        except PermissionError:
            subprocess.run(["sudo", "-n", "mkdir", "-p", path], check=True)
        return f"Successfully created directory: {path}"
    except Exception as e:
        return f"ERROR: Could not create directory. {str(e)}"

def _is_catastrophic_delete_target(path):
    """True for paths that must never be deleted: filesystem root, the user's home,
    the parent of home, or the CLI's own install folder."""
    try:
        norm = os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))
    except Exception:
        return True
    home = os.path.realpath(os.path.expanduser("~"))
    install_dir = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    forbidden = {os.path.sep, home, os.path.dirname(home), install_dir}
    for _sub in ("core", "agent", "web", "interface", "adapters", "tests"):
        forbidden.add(os.path.join(install_dir, _sub))
    return norm in forbidden


def delete_item(path):
    if is_protected_system_path(path):
        return f"ERROR: Protected system path access denied for {path}. File tools cannot modify protected OS directories."
    if _is_catastrophic_delete_target(path):
        return f"ERROR: Refusing to delete {path}. It is the filesystem root, your home folder, or the CLI's own folder."
    try:
        if not os.path.lexists(path):
            return f"ERROR: Item not found at {path}"
        try:
            if os.path.islink(path):
                os.unlink(path)
                return f"Successfully deleted symlink: {path}"
            if os.path.isdir(path):
                shutil.rmtree(path)
                return f"Successfully deleted directory: {path}"
            else:
                os.remove(path)
                return f"Successfully deleted file: {path}"
        except PermissionError:
            subprocess.run(["sudo", "-n", "rm", "-rf", path], check=True)
            return f"Successfully deleted {'directory' if os.path.isdir(path) else 'file'}: {path}"
    except Exception as e:
        return f"ERROR: Could not delete {path}. {str(e)}"

def rename_item(old_path, new_path):
    if is_protected_system_path(old_path) or is_protected_system_path(new_path):
        return f"ERROR: Protected system path access denied. File tools cannot modify protected OS directories."
    try:
        if not os.path.lexists(old_path):
            return f"ERROR: Item not found at {old_path}"
        if os.path.lexists(new_path) and os.path.realpath(old_path) != os.path.realpath(new_path):
            return f"ERROR: Destination already exists at {new_path}. Delete it first or choose another name."
        try:
            parent = os.path.dirname(new_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            os.rename(old_path, new_path)
        except PermissionError:
            parent = os.path.dirname(new_path)
            if parent:
                subprocess.run(["sudo", "-n", "mkdir", "-p", parent], check=True)
            subprocess.run(["sudo", "-n", "mv", old_path, new_path], check=True)
        return f"Successfully renamed {old_path} to {new_path}"
    except Exception as e:
        return f"ERROR: Could not rename. {str(e)}"

def list_directory(path):
    try:
        if not os.path.exists(path):
            return f"ERROR: Directory not found at {path}"
            
        entries = os.listdir(path)
        if not entries: 
            return "The directory is empty."
            
        output = list()
        for entry in sorted(entries):
            full_path = os.path.join(path, entry)
            if os.path.isdir(full_path):
                output.append(f"[DIR]  {entry}/")
            elif os.path.isfile(full_path):
                size_bytes = os.path.getsize(full_path)
                size_str = format_size(size_bytes)
                # Fast line count: read raw bytes and count newlines - no full decode pass
                try:
                    if size_bytes > 10 * 1024 * 1024:  # Skip line count for files > 10MB
                        output.append(f"[FILE] {entry} - ({size_str})")
                        continue
                    with open(full_path, 'rb') as f:
                        data = f.read()
                    line_count = data.count(b'\n')
                    # If file doesn't end with newline, count the last line too
                    if data and not data.endswith(b'\n'):
                        line_count += 1
                    output.append(f"[FILE] {entry} - {line_count} lines ({size_str})")
                except Exception:
                    output.append(f"[FILE] {entry} - ({size_str})")
            else:
                output.append(f"[OTHER] {entry}")
                
        return "\n".join(output)
    except Exception as e:
        return f"ERROR: Could not list directory. {str(e)}"


def grep_search(directory, search_text):
    import time as _time
    try:
        if not directory or not os.path.exists(directory):
            return f"ERROR: Path not found: {directory}"
        if not search_text or not str(search_text).strip():
            return "ERROR: search_text is empty."
        search_text = str(search_text)

        start_ts = _time.time()
        TIME_BUDGET = 15.0
        MAX_MATCHES = 500
        MAX_FILE_BYTES = 2 * 1024 * 1024
        MAX_LINE_CHARS = 500

        exclude_dirs = {
            '.git', '.hg', '.svn', 'node_modules', '.gradle', 'build', 'bin',
            'venv', 'env', '.venv', 'target', '.idea', '.vscode',
            '__pycache__', '.cache', '.pytest_cache', '.mypy_cache', '.tox',
            'dist', 'out', 'site-packages', '.next', '.nuxt', 'coverage',
            '.Trash', 'lost+found', '.npm', '.yarn', '.pnpm-store'
        }
        binary_exts = {
            '.pyc', '.pyo', '.so', '.o', '.a', '.dll', '.dylib', '.exe', '.class', '.jar',
            '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.webp', '.svg',
            '.mp3', '.mp4', '.mkv', '.avi', '.mov', '.wav', '.flac', '.ogg', '.webm',
            '.zip', '.gz', '.bz2', '.xz', '.tar', '.7z', '.rar',
            '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
            '.woff', '.woff2', '.ttf', '.otf', '.eot',
            '.sqlite', '.db', '.lock', '.bin', '.dat'
        }

        results = []
        state = {'timed_out': False, 'skipped_large': 0}

        def _scan_file(filepath):
            if state['timed_out']:
                return
            try:
                sz = os.path.getsize(filepath)
            except OSError:
                return
            if sz > MAX_FILE_BYTES:
                state['skipped_large'] += 1
                return
            try:
                with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                    for i, line in enumerate(f, 1):
                        if search_text in line:
                            clean_line = line.strip()
                            if len(clean_line) > MAX_LINE_CHARS:
                                clean_line = clean_line[:MAX_LINE_CHARS] + " ... [Line too long, truncated] ..."
                            results.append(f"{filepath}:{i}:{clean_line}")
                            if len(results) >= MAX_MATCHES:
                                return
            except Exception:
                return

        if os.path.isfile(directory):
            if os.path.splitext(directory)[1].lower() in binary_exts:
                return f"SKIPPED: '{directory}' looks like a binary file."
            _scan_file(directory)
        else:
            for root, dirs, files in os.walk(directory):
                if len(results) >= MAX_MATCHES:
                    break
                dirs[:] = [d for d in dirs if d not in exclude_dirs and not d.startswith('.')]
                for file in files:
                    if len(results) >= MAX_MATCHES:
                        break
                    if (_time.time() - start_ts) > TIME_BUDGET:
                        state['timed_out'] = True
                        break
                    if os.path.splitext(file)[1].lower() in binary_exts:
                        continue
                    _scan_file(os.path.join(root, file))
                if state['timed_out']:
                    break

        hit_cap = len(results) >= MAX_MATCHES

        if not results:
            note = f" (search stopped after {TIME_BUDGET:.0f}s budget)" if state['timed_out'] else ""
            return f"No matches found for '{search_text}' in {directory}{note}."

        full_output = "\n".join(results)
        total_chars = len(full_output)

        status_lines = []
        if state['timed_out']:
            status_lines.append(f"[NOTE: search stopped early after {TIME_BUDGET:.0f}s budget; results may be incomplete.]")
        if hit_cap:
            status_lines.append(f"[NOTE: results capped at {MAX_MATCHES} matches; narrow the search for full results.]")
        if state['skipped_large']:
            status_lines.append(f"[NOTE: skipped {state['skipped_large']} file(s) larger than {MAX_FILE_BYTES // (1024*1024)} MB.]")

        MAX_GREP_CHARS = 15000
        if total_chars > MAX_GREP_CHARS:
            try:
                os.makedirs(os.path.dirname(SEARCH_LOG_FILE), exist_ok=True)
                with open(SEARCH_LOG_FILE, 'w', encoding='utf-8', errors='replace') as sf:
                    sf.write(full_output)
            except Exception:
                pass
            prefix = (
                "--- GREP SEARCH RESULTS TRUNCATED DUE TO SIZE SAFETY LIMITS ---\n"
                f"The search matched {len(results)} line(s), {total_chars} characters.\n"
                f"The FULL search output was saved to: {SEARCH_LOG_FILE}\n"
                "Use 'read_file' on that path for the rest.\n\n"
                "--- FIRST 12,000 CHARACTERS ---\n"
            )
            if status_lines:
                prefix = "\n".join(status_lines) + "\n" + prefix
            return prefix + full_output[:12000] + "\n\n... [TRUNCATED] ..."

        if status_lines:
            return "\n".join(status_lines) + "\n" + full_output
        return full_output

    except Exception as e:
        return f"ERROR: An error occurred during grep. {str(e)}"

def write_to_scratchpad(content, mode='a'):
    try:
        write_mode = 'a' if mode in ('a', 'append') else 'w'
        with open(SCRATCHPAD_FILE, write_mode, encoding='utf-8', errors='replace') as f:
            f.write(content + "\n")
        return f"Successfully saved {len(content)} characters to scratchpad."
    except Exception as e:
        return f"ERROR: Could not write to scratchpad. {str(e)}"
